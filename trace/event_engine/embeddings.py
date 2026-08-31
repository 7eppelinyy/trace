"""Embedding 提供者：默认 sentence-transformers 跨语言模型。

离线/无模型环境自动降级为字符 n-gram hash 伪向量，
保证 Level 3 聚类逻辑始终可运行（精度降低但流程完整）。
"""

from __future__ import annotations

import hashlib
import logging
import math

logger = logging.getLogger(__name__)


class Embedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class HashEmbedder(Embedder):
    """字符 3-gram hash 伪向量：跨语言语义能力弱，仅作降级方案。"""

    def __init__(self, dim: int = 256):
        self.dim = dim

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
        self._model = SentenceTransformer(model_name)

    def encode(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(texts or [""], normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


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
