"""FAISS 벡터 저장소 (PRD F5).

- IndexIDMap(IndexFlatIP) + L2 정규화 → 내적 = 코사인 유사도.
- ID는 products.id 와 동일하게 써서 갱신/삭제가 가능하다.
- 저장/로드는 faiss.serialize_index 로 바이트를 만들어 Python이 직접 파일을 쓴다.
  (Windows에서 faiss.write_index 에 한글 경로를 넘기면 실패하므로 경로를 C++에 넘기지 않는다.)
- 저장은 임시 파일에 쓴 뒤 os.replace 로 교체 → 중간에 꺼져도 인덱스가 깨지지 않는다.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterable, Sequence

import faiss
import numpy as np


class VectorStore:
    def __init__(self, path: Path, dim: int):
        self.path = Path(path)
        self.dim = dim
        self._lock = threading.RLock()
        self.index = self._new_index()

    def _new_index(self) -> faiss.IndexIDMap:
        return faiss.IndexIDMap(faiss.IndexFlatIP(self.dim))

    # ---------- 영속화 ----------
    def load(self) -> "VectorStore":
        """파일이 있으면 읽고, 없으면 빈 인덱스를 만들어 바로 저장한다."""
        with self._lock:
            if self.path.exists():
                buf = np.frombuffer(self.path.read_bytes(), dtype=np.uint8)
                index = faiss.deserialize_index(buf)
                if index.d != self.dim:
                    raise ValueError(
                        f"FAISS 인덱스 차원({index.d})이 설정된 임베딩 차원({self.dim})과 다릅니다. "
                        "임베딩 모델을 바꿨다면 인덱스를 다시 만들어야 합니다."
                    )
                self.index = index
            else:
                self.index = self._new_index()
                self.save()
        return self

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = faiss.serialize_index(self.index)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_bytes(data.tobytes())
            os.replace(tmp, self.path)

    # ---------- 조작 ----------
    @property
    def size(self) -> int:
        return int(self.index.ntotal)

    def _prepare(self, vectors: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if arr.shape[1] != self.dim:
            raise ValueError(f"벡터 차원 {arr.shape[1]} != {self.dim}")
        arr = np.ascontiguousarray(arr)
        faiss.normalize_L2(arr)
        return arr

    def upsert(self, ids: Iterable[int], vectors) -> None:
        """같은 ID가 있으면 지우고 다시 넣는다."""
        id_arr = np.asarray(list(ids), dtype=np.int64)
        if id_arr.size == 0:
            return
        vecs = self._prepare(vectors)
        if len(id_arr) != len(vecs):
            raise ValueError("ids와 vectors 개수가 다릅니다.")
        with self._lock:
            self.index.remove_ids(id_arr)
            self.index.add_with_ids(vecs, id_arr)

    def remove(self, ids: Iterable[int]) -> int:
        id_arr = np.asarray(list(ids), dtype=np.int64)
        if id_arr.size == 0:
            return 0
        with self._lock:
            return int(self.index.remove_ids(id_arr))

    def search(self, vector, k: int = 10, min_score: float | None = None) -> list[tuple[int, float]]:
        """(id, 코사인 유사도) 목록, 유사도 내림차순."""
        with self._lock:
            if self.size == 0:
                return []
            q = self._prepare(vector)
            scores, ids = self.index.search(q, min(k, self.size))
        out = []
        for i, s in zip(ids[0], scores[0]):
            if i == -1:
                continue
            if min_score is not None and s < min_score:
                continue
            out.append((int(i), float(s)))
        return out
