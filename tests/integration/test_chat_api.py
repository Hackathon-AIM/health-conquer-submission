from fastapi.testclient import TestClient

from medibot.api.server import app


def test_chat_endpoint_returns_answer_and_trace_id() -> None:
    client = TestClient(app)
    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "콜레스테롤이 뭐야?"}]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["trace_id"]
    assert data["answer"]
    assert data["triage_class"] == "NON_EMERGENT"


def test_chat_endpoint_keeps_two_turn_session_state() -> None:
    client = TestClient(app)
    first = client.post(
        "/chat",
        json={
            "session_id": "api-session",
            "messages": [{"role": "user", "content": "고혈압이 있어요"}],
        },
    )
    assert first.status_code == 200
    second = client.post(
        "/chat",
        json={
            "session_id": "api-session",
            "messages": [
                {"role": "user", "content": "고혈압이 있어요"},
                {"role": "assistant", "content": first.json()["answer"]},
                {"role": "user", "content": "이부프로펜 먹어도 돼?"},
            ],
        },
    )
    assert second.status_code == 200
    assert second.json()["answer"]


def test_openai_models_endpoint_returns_model_list() -> None:
    client = TestClient(app)
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert data["data"][0]["id"]


def test_openai_chat_completions_endpoint_returns_choice() -> None:
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "medibot-fallback",
            "messages": [{"role": "user", "content": "콜레스테롤이 뭐야?"}],
            "user": "openai-api-session",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"]
    assert data["medibot"]["trace_id"]
