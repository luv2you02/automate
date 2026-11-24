import numpy as np
import pytest

from app.embeddings import FakeEmbeddings
from app.vector_store import VectorStore

DIM = 1536


@pytest.fixture
def emb():
    return FakeEmbeddings(DIM)


def test_load_creates_file(settings):
    vs = VectorStore(settings.faiss_path, DIM).load()
    assert settings.faiss_path.exists()  # 한글 경로에서도 생성
    assert vs.size == 0
    assert vs.search(np.ones(DIM)) == []


def test_upsert_search_remove(settings, emb):
    vs = VectorStore(settings.faiss_path, DIM).load()
    texts = ["제품번호 FE02294303 고객사 신우", "제품번호 FD48282003 고객사 명강스틸", "제품번호 FC98279601 고객사 대왕플랜트"]
    vs.upsert([1, 2, 3], emb.embed(texts))
    assert vs.size == 3

    hits = vs.search(emb.embed([texts[1]])[0], k=3)
    assert hits[0][0] == 2
    assert hits[0][1] == pytest.approx(1.0, abs=1e-5)

    # 같은 ID 재삽입 → 개수 유지, 새 벡터로 교체
    vs.upsert([2], emb.embed(["바뀐 내용"]))
    assert vs.size == 3
    assert vs.search(emb.embed(["바뀐 내용"])[0], k=1)[0][0] == 2

    assert vs.remove([1]) == 1
    assert vs.size == 2
    assert all(i != 1 for i, _ in vs.search(emb.embed([texts[0]])[0], k=5))


def test_persist_roundtrip(settings, emb):
    vs = VectorStore(settings.faiss_path, DIM).load()
    vs.upsert([10, 20], emb.embed(["a", "b"]))
    vs.save()

    vs2 = VectorStore(settings.faiss_path, DIM).load()
    assert vs2.size == 2
    assert vs2.search(emb.embed(["b"])[0], k=1)[0][0] == 20


def test_min_score_filter(settings, emb):
    vs = VectorStore(settings.faiss_path, DIM).load()
    vs.upsert([1, 2], emb.embed(["a", "b"]))
    hits = vs.search(emb.embed(["a"])[0], k=2, min_score=0.9)
    assert [i for i, _ in hits] == [1]


def test_dim_mismatch(settings):
    VectorStore(settings.faiss_path, DIM).load()
    with pytest.raises(ValueError):
        VectorStore(settings.faiss_path, 8).load()
    vs = VectorStore(settings.faiss_path, DIM).load()
    with pytest.raises(ValueError):
        vs.upsert([1], np.ones((1, 8)))


def test_fake_embeddings_deterministic(emb):
    a = emb.embed(["같은 문장"])
    b = FakeEmbeddings(DIM).embed(["같은 문장"])
    assert np.allclose(a, b)
    assert a.shape == (1, DIM)
    assert np.linalg.norm(a[0]) == pytest.approx(1.0, abs=1e-5)
    assert emb.embed([]).shape == (0, DIM)
