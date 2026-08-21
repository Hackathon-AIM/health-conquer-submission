"""제출용 멀티턴 대화 드라이버 — OpenAI 호환 서버.

평가자(Evaluator)가 각 대화 턴을 POST /v1/chat/completions 로 보내고,
이 서버가 다음 assistant 응답을 돌려준다.

지금은 L2 로 그대로 넘기는 최소 관통 버전이다.
이 파일의 `generate_reply()` 안이 retrieval/generation 2단계 하네스로 바뀔 자리다.
"""

import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("driver")

FM_URL = os.environ.get("LUNIT_FM_API_URL", "https://model.hackathon.lunit.io").rstrip("/")
FM_KEY = os.environ.get("LUNIT_FM_API_KEY", "")
FM_MODEL = os.environ.get("LUNIT_FM_MODEL", "Lunit/L2-preview")

# 서버가 max_tokens 2048 을 넘기면 400 (`output_limit_exceeded`) 을 던진다. 이건 상한이다.
SERVER_MAX_TOKENS = 2048
MAX_TOKENS = min(int(os.environ.get("FM_MAX_TOKENS", "2048")), SERVER_MAX_TOKENS)
TIMEOUT = float(os.environ.get("FM_TIMEOUT", "180"))

# L2 는 사고과정을 별도 `reasoning` 필드로 뱉는데, 그게 2048 예산을 통째로 먹는다.
# 실측(같은 질문):
#   thinking on  → reasoning 2492자 + content 881자, finish=length  (잘림)
#   thinking off → reasoning 0자    + content 576자, finish=stop     (완결, 395토큰)
# 상한이 2048 로 묶여 있는 한, thinking 을 켜면 긴 답변은 구조적으로 완결될 수 없다.
# 품질 A/B 는 따로 하되 기본값은 off 로 둔다.
ENABLE_THINKING = os.environ.get("FM_THINKING", "0") == "1"

app = FastAPI(title="AIM conversation driver")


@app.on_event("startup")
async def announce() -> None:
    # 키가 없어도 컨테이너는 뜬다 — "수동 작업 없이 시작" 요구사항 때문.
    # 대신 여기서 크게 남겨서 평가 로그만 봐도 원인을 알 수 있게 한다.
    if not FM_KEY:
        log.error("LUNIT_FM_API_KEY 가 비어 있다. 모든 생성 요청이 실패한다.")
    log.info(
        "driver up — model=%s url=%s max_tokens=%d thinking=%s",
        FM_MODEL,
        FM_URL,
        MAX_TOKENS,
        ENABLE_THINKING,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "model": FM_MODEL, "key_present": bool(FM_KEY)}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": FM_MODEL, "object": "model", "owned_by": "lunit"}]}


async def call_fm(messages: list[dict], max_tokens: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": FM_MODEL,
        "messages": messages,
        "max_tokens": min(max_tokens, SERVER_MAX_TOKENS),
        "chat_template_kwargs": {"enable_thinking": ENABLE_THINKING},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            f"{FM_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {FM_KEY}"},
            json=payload,
        )
    if r.status_code != 200:
        # 본문에 원인이 들어 있다 (예: {"error":{"code":"output_limit_exceeded"}}).
        # 상태코드만 남기면 당일 새벽에 원인을 못 찾는다.
        log.error("FM %d: %s", r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()


async def generate_reply(messages: list[dict]) -> str:
    """대화 맥락을 받아 다음 assistant 발화를 만든다.

    TODO: 여기가 2단계 하네스로 바뀐다.
      1) retrieval  — MCP tools + finalize_retrieval 만 주고 cite_uid 수집
      2) generation — retrieve_relevant_content 하나만 주고 최종 답변 생성
    또한 L2 는 single-turn 최적화라, 멀티턴 히스토리는 여기서
    query rewriting / context summarization 으로 눌러줘야 한다.
    """
    data = await call_fm(messages, MAX_TOKENS)
    choice = data["choices"][0]
    content = (choice["message"].get("content") or "").strip()

    # content 가 비는 건 대개 reasoning 이 예산을 다 먹고 잘린 경우다.
    # max_tokens 를 더 올릴 수는 없으므로(2048 이 상한), 한 번만 다시 시도한다.
    # 조용히 빈 문자열을 흘리면 그 문항은 통째로 0점이 된다.
    if not content:
        log.warning("빈 content — finish_reason=%s, 재시도", choice.get("finish_reason"))
        data = await call_fm(messages, MAX_TOKENS)
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()

    if not content:
        log.error("재시도 후에도 빈 content — finish_reason=%s", choice.get("finish_reason"))
    return content


@app.post("/v1/chat/completions")
async def chat_completions(body: dict) -> dict[str, Any]:
    messages = body.get("messages") or []
    try:
        content = await generate_reply(messages)
    except Exception:
        # 평가 하네스에 5xx 를 돌려주면 대화 전체가 깨질 수 있다.
        # 그래서 형식은 지키되, 실패는 로그에 남겨 사후에 반드시 보이게 한다.
        log.exception("생성 실패")
        content = ""

    return {
        "object": "chat.completion",
        "model": FM_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
