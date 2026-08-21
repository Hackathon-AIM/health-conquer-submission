"""제출용 멀티턴 대화 드라이버 — OpenAI 호환 서버.

평가자(Evaluator)가 각 대화 턴을 POST /v1/chat/completions 로 보내고,
이 서버가 다음 assistant 응답을 돌려준다.

기본은 **기준선(baseline)** 이다 — 받은 대화를 L2 에 그대로 넘기고 그 출력만 돌려준다.
2단계 하네스(라우터 → retrieval → generation)는 `BASELINE=0` 으로 켠다.
"""

import asyncio
import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI

from generation import generate
from mcp_client import MCPClient
from router import classify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("driver")

FM_URL = os.environ.get("LUNIT_FM_API_URL", "https://model.hackathon.lunit.io").rstrip("/")
FM_KEY = os.environ.get("LUNIT_FM_API_KEY", "")
FM_MODEL = os.environ.get("LUNIT_FM_MODEL", "Lunit/L2-preview")

# 서버가 max_tokens 2048 을 넘기면 400 (`output_limit_exceeded`) 을 던진다. 이건 상한이다.
SERVER_MAX_TOKENS = 2048
MAX_TOKENS = min(int(os.environ.get("FM_MAX_TOKENS", "2048")), SERVER_MAX_TOKENS)
TIMEOUT = float(os.environ.get("FM_TIMEOUT", "180"))
FM_RETRIES = int(os.environ.get("FM_RETRIES", "3"))
FM_BACKOFF = float(os.environ.get("FM_BACKOFF", "1.5"))

# retrieval 단계에서 허용할 MCP 도구 호출 수. 대시보드 팁이 "제한하라"고 명시한다.
RETRIEVAL_BUDGET = int(os.environ.get("RETRIEVAL_BUDGET", "6"))
EMERGENCY_BUDGET = int(os.environ.get("EMERGENCY_BUDGET", "2"))

MCP = MCPClient()

# L2 는 사고과정을 별도 `reasoning` 필드로 뱉는데, 그게 2048 예산을 통째로 먹는다.
# 실측(같은 질문):
#   thinking on  → reasoning 2492자 + content 881자, finish=length  (잘림)
#   thinking off → reasoning 0자    + content 576자, finish=stop     (완결, 395토큰)
# 상한이 2048 로 묶여 있는 한, thinking 을 켜면 긴 답변은 구조적으로 완결될 수 없다.
# 품질 A/B 는 따로 하되 기본값은 off 로 둔다.
ENABLE_THINKING = os.environ.get("FM_THINKING", "0") == "1"

# 기준선 모드 — 라우터·retrieval·generation 하네스를 전부 건너뛰고 L2 로 그대로 넘긴다.
# 하네스가 실제로 얼마나 보태는지 재는 대조군이고, 하네스가 깨졌을 때 되돌아올 바닥이다.
# 하네스를 다시 태우려면 BASELINE=0.
BASELINE = os.environ.get("BASELINE", "1") == "1"

app = FastAPI(title="AIM conversation driver")


@app.on_event("startup")
async def announce() -> None:
    # 키가 없어도 컨테이너는 뜬다 — "수동 작업 없이 시작" 요구사항 때문.
    # 대신 여기서 크게 남겨서 평가 로그만 봐도 원인을 알 수 있게 한다.
    if not FM_KEY:
        log.error("LUNIT_FM_API_KEY 가 비어 있다. 모든 생성 요청이 실패한다.")
    log.info(
        "driver up — model=%s url=%s max_tokens=%d thinking=%s mode=%s",
        FM_MODEL,
        FM_URL,
        MAX_TOKENS,
        ENABLE_THINKING,
        "baseline" if BASELINE else "harness",
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "model": FM_MODEL, "key_present": bool(FM_KEY)}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": FM_MODEL, "object": "model", "owned_by": "lunit"}]}


# 일시적인 것들. 실측으로 502(nginx)를 봤다 — 재시도 없이 두면 그 문항이 통째로 0점이다.
RETRY_STATUS = {429, 500, 502, 503, 504}


async def call_fm(
    messages: list[dict], max_tokens: int, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """FM 한 번 호출. `extra` 로 response_format 같은 vLLM 파라미터를 얹는다.

    일시적 실패(502·타임아웃 등)는 지수 백오프로 되돌려 시도한다.
    영구적 실패(400 output_limit_exceeded 등)는 즉시 올린다 — 재시도해도 같다.
    """
    payload: dict[str, Any] = {
        "model": FM_MODEL,
        "messages": messages,
        "max_tokens": min(max_tokens, SERVER_MAX_TOKENS),
        "chat_template_kwargs": {"enable_thinking": ENABLE_THINKING},
        **(extra or {}),
    }
    last: Exception | None = None
    for attempt in range(FM_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{FM_URL}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {FM_KEY}"},
                    json=payload,
                )
            if r.status_code == 200:
                return r.json()

            # 본문에 원인이 들어 있다 (예: {"error":{"code":"output_limit_exceeded"}}).
            # 상태코드만 남기면 당일 새벽에 원인을 못 찾는다.
            log.error("FM %d (%d/%d): %s", r.status_code, attempt + 1, FM_RETRIES, r.text[:300])
            if r.status_code not in RETRY_STATUS:
                r.raise_for_status()
            last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("FM 통신 실패 (%d/%d): %s", attempt + 1, FM_RETRIES, type(e).__name__)
            last = e

        if attempt < FM_RETRIES - 1:
            await asyncio.sleep(FM_BACKOFF * (2**attempt))

    assert last is not None
    raise last


async def run_baseline(messages: list[dict]) -> str:
    """기준선 — 받은 대화를 손대지 않고 L2 에 그대로 넘긴다.

    라우터도 retrieval 도 없다. 하네스가 붙기 전 점수가 여기서 나온다.
    """
    data = await call_fm(messages, MAX_TOKENS)
    return (data["choices"][0]["message"].get("content") or "").strip()


async def run_harness(messages: list[dict]) -> str:
    """2단계 하네스 — 라우팅 후 되묻기 / 직답 / 검색-후-답변으로 갈린다.

    입구에서 라우터가 도메인·응급도·맥락 충분성·페르소나를 정한다.

    TODO: L2 는 single-turn 최적화라 멀티턴 히스토리를 self-contained 질의로
    눌러주는 단계가 아직 없다.
    """
    route = await classify(messages, call_fm)
    log.info(
        "route: domain=%s urgency=%s context=%s persona=%s date=%s src=%s tools=%d",
        route.domain, route.urgency, route.context, route.persona,
        route.date_sensitive, route.source, len(route.tools),
    )

    # 결정적 맥락이 빠졌고 응급도 아니면, 답을 지어내지 말고 하나만 되묻는다.
    # 09 문서 §4 — 맥락인지는 Consensus 두 번째로 큰 축(24.7%)이고 프론티어가 무너지는 곳이다.
    if route.context == "missing_critical" and route.ask_back:
        return route.ask_back

    # 응급이면 검색을 짧게 끊는다. 실측에서 예산 6회를 다 쓰고 31초가 걸렸는데,
    # 정작 근거는 "약물 부작용 자료에서 확인되지 않음"이라 답에 보탬이 없었다.
    # 응급에서 값을 내는 건 근거 인용이 아니라 즉시 의뢰다.
    budget = EMERGENCY_BUDGET if route.urgency == "emergency" else RETRIEVAL_BUDGET
    content, retr = await generate(messages, route, call_fm, MCP, MAX_TOKENS, budget)
    return content


async def generate_reply(messages: list[dict]) -> str:
    """대화 맥락을 받아 다음 assistant 발화를 만든다.

    기본은 기준선(그대로 넘기기)이다. BASELINE=0 이면 2단계 하네스를 태운다.
    """
    content = await (run_baseline(messages) if BASELINE else run_harness(messages))

    # content 가 비는 건 대개 reasoning 이 예산을 다 먹고 잘린 경우다.
    # max_tokens 를 더 올릴 수는 없으므로(2048 이 상한), 한 번만 다시 시도한다.
    # 조용히 빈 문자열을 흘리면 그 문항은 통째로 0점이 된다.
    if not content:
        log.warning("빈 content — 재시도")
        content = await run_baseline(messages)

    if not content:
        log.error("재시도 후에도 빈 content")
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
