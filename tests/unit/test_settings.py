from medibot.config.settings import Settings


def test_lunit_fm_environment_aliases(monkeypatch):
    monkeypatch.setenv("MEDIBOT_MODEL_API_BASE", "")
    monkeypatch.setenv("MEDIBOT_MODEL_API_KEY", "")
    monkeypatch.setenv("MEDIBOT_MODEL_NAME", "")
    monkeypatch.setenv("LUNIT_FM_API_URL", "https://model.hackathon.lunit.io")
    monkeypatch.setenv("LUNIT_FM_API_KEY", "lunit_test")
    monkeypatch.setenv("LUNIT_FM_MODEL", "Lunit/L2-preview")

    settings = Settings()

    assert settings.model_api_base == "https://model.hackathon.lunit.io/v1"
    assert settings.model_api_key == "lunit_test"
    assert settings.model_name == "Lunit/L2-preview"
