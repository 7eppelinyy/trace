"""Embedding 提供者：默认 sentence-transformers 跨语言模型。

离线/无模型环境自动降级为字符 n-gram hash 伪向量，
保证 Level 3 聚类逻辑始终可运行（精度降低但流程完整）。
"""

from __future__ import annotations

import hashlib
import logging
import math

logger = logging.getLogger(__name__)

try:  # 可选加速：缺失时自动回退纯 Python，任何环境可运行
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


class Embedder:
    model_name: str = "unknown"
    dim: int = 0
    is_degraded: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class HashEmbedder(Embedder):
    """字符 3-gram hash 伪向量：跨语言语义能力弱，仅作降级方案。"""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.model_name = f"hash-ngram-{dim}"
        self.is_degraded = True

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            t = (t or "").lower()
            grams = [t[i:i + 3] for i in range(max(0, len(t) - 2))] or [t]
            for g in grams:
                idx = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16) % self.dim
                vec[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        self.model_name = model_name
        self.is_degraded = False
        self._model = SentenceTransformer(model_name)
        try:
            self.dim = int(self._model.get_sentence_embedding_dimension())
        except Exception:
            self.dim = 384

    def encode(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(texts or [""], normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def pairwise_cosines(q: list[float], vecs: list[list[float]]) -> list[float]:
    """q 与每个 vec 的余弦。

    numpy 可用时一次矩阵乘法算完（聚类热路径：每条新 item × 近 72h 全部
    事件 × 2 向量），缺失或向量维度不齐时回退逐对纯 Python 计算。
    """
    n = len(vecs)
    if not q or n == 0:
        return [0.0] * n
    if _np is not None:
        try:
            qv = _np.asarray(q, dtype=float)
            qn = float(_np.linalg.norm(qv))
            idx: list[int] = []
            rows: list[list[float]] = []
            for i, v in enumerate(vecs):
                if v and len(v) == len(q):
                    idx.append(i)
                    rows.append(v)
            out = [0.0] * n
            if rows:
                m = _np.asarray(rows, dtype=float)
                norms = _np.linalg.norm(m, axis=1)
                denom = norms * qn
                safe = _np.where(denom > 0, denom, 1.0)
                sims = _np.clip((m @ qv) / safe, 0.0, 1.0)
                for i, s in zip(idx, sims):
                    out[i] = float(s)
            return out
        except Exception as exc:  # 数值/维度异常：回退逐对计算
            logger.debug("pairwise_cosines numpy path failed, fallback: %s", exc)
    return [cosine(q, v) for v in vecs]


def build_embedder(config) -> Embedder:
    """按 settings.yaml 中 embedding.provider 构建；失败时降级。"""
    provider = config.get("embedding.provider", "sentence-transformers")
    if provider == "hash":
        return HashEmbedder(int(config.get("embedding.hash_dim", 256)))
    model_name = config.get("embedding.model", "paraphrase-multilingual-MiniLM-L12-v2")
    try:
        return SentenceTransformerEmbedder(model_name)
    except Exception as exc:  # pragma: no cover - 依赖环境
        logger.warning("sentence-transformers unavailable (%s), fallback to hash embedder", exc)
        return HashEmbedder(int(config.get("embedding.hash_dim", 256)))
