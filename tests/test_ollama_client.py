"""Ollama client tests."""
from ollama_client import OllamaClient


def test_default_client_configuration(monkeypatch):
    for name in (
        "SETUP_MANAGER_OLLAMA_URL",
        "SETUP_MANAGER_OLLAMA_MODEL",
        "SETUP_MANAGER_OLLAMA_ALLOWED_MODELS",
        "SETUP_MANAGER_OLLAMA_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    client = OllamaClient()
    assert client.base_url == "http://127.0.0.1:11434"
    assert client.model == "qwen2.5-coder:7b"
    assert client.timeout == 300.0


def test_the_default_model_is_on_the_allowed_list():
    """The default model must be usable, or every chat request is rejected."""
    client = OllamaClient()
    assert client.model in client.allowed_models


def test_a_model_outside_the_policy_is_rejected():
    from ollama_client import OllamaError

    try:
        OllamaClient(model="some-other-model")
    except OllamaError:
        return
    raise AssertionError("a model outside the allow-list should be rejected")
