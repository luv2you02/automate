from app.config import load_settings


def test_env_overrides_and_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WIDE_MAX_TON", "26.5")
    monkeypatch.setenv("CHAT_MODEL", "")
    s = load_settings()
    assert s.data_dir == tmp_path
    assert s.rules.wide_max_ton == 26.5
    assert s.rules.normal_max_ton == 27
    assert s.chat_model is None
    assert s.embedding_dim == 1536


def test_api_key_not_exposed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    s = load_settings()
    assert "sk-test" not in repr(s)
    assert "sk-test" not in str(s.public_dict())
    assert s.public_dict()["api_key_configured"] is True
