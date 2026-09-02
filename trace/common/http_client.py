"""带重试/退避/限流的 HTTP 客户端与来源错误分类。

所有真实 Collector 必须使用本模块，不得自行吞异常返回空数组。
必须区分：
    网络错误 / 来源限流 / 来源结构变化 / 无新数据 /
    解析失败 / 鉴权失败 / 来源暂时不可用
"""

from __future__ import annotations

import logging
import random
import time

import httpx

logger = logging.getLogger(__name__)


# ---- 来源特定错误类型 ----
class SourceError(Exception):
    """来源错误基类。"""
    category = "unknown"

    def __init__(self, message: str = "", http_status: int | None = None):
        super().__init__(message)
        # 真实 HTTP 状态码（429/403/500… 必须显式可见，不得与"无新数据"混淆）
        self.http_status = http_status


class NetworkError(SourceError):
    category = "network_error"


class RateLimitedError(SourceError):
    category = "rate_limited"


class SourceStructureError(SourceError):
    category = "structure_changed"


class ParseError(SourceError):
    category = "parse_failed"


class AuthError(SourceError):
    category = "auth_failed"


class SourceUnavailableError(SourceError):
    category = "source_unavailable"


class HttpClient:
    """统一 HTTP 客户端：timeout / retry / 指数退避 / 429 退让 / 错误分类。"""

    def __init__(self, source_id: str, *, timeout: float = 30.0,
                 max_retries: int = 3, backoff_base: float = 1.5,
                 rate_limit_qps: float = 1.0, headers: dict | None = None):
        self.source_id = source_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._min_interval = 1.0 / max(rate_limit_qps, 0.01)
        self._last_request_at = 0.0
        self._client = httpx.Client(timeout=timeout, headers=headers or {},
                                    follow_redirects=True)

    # ------------------------------------------------------------------
    def _rate_limit_wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """发起请求，失败时按指数退避重试；最终失败抛出具体的 SourceError。"""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._rate_limit_wait()
            try:
                self._last_request_at = time.monotonic()
                resp = self._client.request(method, url, **kwargs)
                if resp.status_code == 429:
                    raise RateLimitedError(f"429 from {url}", http_status=429)
                if resp.status_code in (401, 403):
                    raise AuthError(f"{resp.status_code} from {url}",
                                    http_status=resp.status_code)
                if resp.status_code >= 500:
                    raise SourceUnavailableError(f"{resp.status_code} from {url}",
                                                 http_status=resp.status_code)
                if resp.status_code >= 400:
                    raise SourceUnavailableError(f"{resp.status_code} from {url}",
                                                 http_status=resp.status_code)
                return resp
            except RateLimitedError as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break               # 末次尝试不再空等退避
                wait = self._backoff(attempt) * 2
                logger.warning("[%s] rate limited, backoff %.1fs", self.source_id, wait)
                time.sleep(wait)
            except (AuthError, SourceStructureError, ParseError):
                raise  # 不重试：重试无意义
            except SourceError as exc:
                # 404 等普通 4xx（非鉴权/限流）：重试无意义，直接暴露
                if 400 <= (getattr(exc, "http_status", None) or 0) < 500:
                    raise
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                wait = self._backoff(attempt)
                logger.warning("[%s] %s, retry %d/%d in %.1fs",
                               self.source_id, exc.category, attempt + 1,
                               self.max_retries, wait)
                time.sleep(wait)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise NetworkError(f"{type(exc).__name__}: {exc}") from exc
                wait = self._backoff(attempt)
                logger.warning("[%s] network error %s, retry %d/%d in %.1fs",
                               self.source_id, type(exc).__name__, attempt + 1,
                               self.max_retries, wait)
                time.sleep(wait)
        # 保留具体错误类型（来源限流/不可用等），不得统一吞成泛化 SourceError
        if isinstance(last_exc, SourceError):
            raise last_exc
        raise SourceError(f"failed after retries: {last_exc}") from last_exc

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def _backoff(self, attempt: int) -> float:
        # 抖动随退避基数缩放：测试用小基数（如 0.001）时抖动可忽略，
        # 生产默认 1.5 时抖动 ~0.45s 防惊群
        return (self.backoff_base * (2 ** attempt)
                + random.uniform(0, min(0.5, self.backoff_base * 0.3)))

    def close(self) -> None:
        self._client.close()
