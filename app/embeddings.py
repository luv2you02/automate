"""임베딩 클라이언트.

- OpenAIEmbeddings: 실제 API 호출 (배치, 지수 백오프 3회).
- FakeEmbeddings: EMBEDDING_FAKE=1 일 때. 텍스트 해시로 결정적 벡터를 만들며 API를 호출하지 않는다.
  테스트와 Playwright 검토에서 비용 없이 쓰기 위한 것.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Protocol, Sequence

import numpy as np

from .config import Settings

log = logging.getLogger(__name__)


class Embedder(Protocol):
    dim: int
    calls: int  # API(또는 가짜) 호출 횟수 — "중복은 API 호출 안 함" 검증용

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


class FakeEmbeddings:
    def __init__(self, dim: int):
        self.dim = dim
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        self.calls += 1
        out = np.empty((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            seed = int.from_bytes(hashlib.sha256(t.encode("utf-8")).digest()[:8], "little")
            v = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
            out[i] = v / np.linalg.norm(v)
        return out


class EmbeddingError(RuntimeError):
    pass


class OpenAIEmbeddings:
    BATCH = 256
    RETRIES = 3

    def __init__(self, api_key: str, model: str, dim: int):
        if not api_key:
            raise EmbeddingError("OPENAI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        from openai import OpenAI  # 가짜 모드에서는 import 하지 않음

        self._client = OpenAI(api_key=api_key)
        self.model = model
        self.dim = dim
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        chunks = []
        for start in range(0, len(texts), self.BATCH):
            chunks.append(self._embed_batch(list(texts[start : start + self.BATCH])))
        return np.vstack(chunks)

    def _embed_batch(self, batch: list[str]) -> np.ndarray:
        last_err: Exception | None = None
        for attempt in range(self.RETRIES):
            try:
                self.calls += 1
                resp = self._client.embeddings.create(model=self.model, input=batch)
                return np.asarray([d.embedding for d in resp.data], dtype=np.float32)
            except Exception as e:  # 네트워크/레이트리밋 등
                last_err = e
                wait = 2**attempt
                log.warning("임베딩 실패 (%d/%d), %ds 후 재시도: %s", attempt + 1, self.RETRIES, wait, type(e).__name__)
                time.sleep(wait)
        raise EmbeddingError(f"임베딩 {self.RETRIES}회 실패: {last_err}") from last_err


def get_embedder(settings: Settings) -> Embedder:
    if settings.embedding_fake:
        return FakeEmbeddings(settings.embedding_dim)
    return OpenAIEmbeddings(settings.openai_api_key, settings.embedding_model, settings.embedding_dim)
