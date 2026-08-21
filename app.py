"""제출용 멀티턴 대화 드라이버 — OpenAI 호환 서버.

평가자(Evaluator)가 각 대화 턴을 POST /v1/chat/completions 로 보내고,
이 서버가 다음 assistant 응답을 돌려준다.

요청 하나의 흐름:
  1. 라우터가 도메인·응급도·페르소나를 정하고 도메인에 맞는 MCP 도구만 고른다
  2. retrieval 이 그 도구로 근거를 모아 cite_uid 를 확정한다
  3. generation 이 근거를 컨텍스트에 얹어 답을 쓴다
  4. verify 가 내보내기 전에 답을 한 번 더 검사하고, 필요하면 고친다

단계마다 남은 시간을 보고 스스로 줄인다. 답을 못 내는 것이 가장 나쁘므로
어느 단계가 실패해도 그때까지 만든 답으로 내려간다.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from budget import Deadline
from generation import generate
from mcp_client import MCPClient
from router import classify
from verify import verify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("driver")

# 평가 환경이 키를 주입해 주는지 확인되지 않았다. 주입되지 않으면 FM 호출이 전부
# 실패해 답이 통째로 비고, 그 채점은 0점이다 — 실측으로 Done 인데 score 0.00 을 봤다.
# 그래서 폴백 키를 들고 간다. 환경변수가 있으면 언제나 그쪽이 이긴다.
#
# ⚠️ 이 키는 저장소 히스토리에 남는다. 대회가 끝나면 대시보드 /api-keys 에서
#    'submission-eval' 키를 폐기할 것.
_FALLBACK_KEY = "lunit_dFthkHMh2_gB2aVIo_mi5jznWpHoXbU2a2Od4hlVtf4"

FM_URL = os.environ.get("LUNIT_FM_API_URL", "https://model.hackathon.lunit.io").rstrip("/")
FM_KEY = os.environ.get("LUNIT_FM_API_KEY", "").strip() or _FALLBACK_KEY
FM_MODEL = os.environ.get("LUNIT_FM_MODEL", "Lunit/L2-preview")

# 서버가 max_tokens 2048 을 넘기면 400 (`output_limit_exceeded`) 을 던진다. 이건 상한이다.
SERVER_MAX_TOKENS = 2048
MAX_TOKENS = min(int(os.environ.get("FM_MAX_TOKENS", "2048")), SERVER_MAX_TOKENS)
TIMEOUT = float(os.environ.get("FM_TIMEOUT", "120"))
FM_RETRIES = int(os.environ.get("FM_RETRIES", "3"))
FM_BACKOFF = float(os.environ.get("FM_BACKOFF", "1.5"))

# retrieval 단계에서 허용할 MCP 도구 호출 수. 대시보드 팁이 "제한하라"고 명시한다.
RETRIEVAL_BUDGET = int(os.environ.get("RETRIEVAL_BUDGET", "3"))
EMERGENCY_BUDGET = int(os.environ.get("EMERGENCY_BUDGET", "2"))

# 요청 하나에 쓸 수 있는 총 시간. RESERVE 는 최종 답변 생성 몫으로 떼어 둔다.
#
# 처음엔 75초로 뒀는데, 그 값으로 돌린 trial 이 0.00 을 받았다. 같은 회차에서
# make-easy 가 40.39 를 받았으니 채점 자체는 정상이고 우리가 느린 쪽이다.
# 답을 늦게 주는 것과 안 주는 것이 채점에서 같다면, 짧게 끊고 답을 내는 편이 낫다.
REQUEST_BUDGET_S = float(os.environ.get("REQUEST_BUDGET_S", "40"))
ANSWER_RESERVE_S = float(os.environ.get("ANSWER_RESERVE_S", "18"))

# 출력 검증 몫. 검색은 답변 몫과 이 몫을 둘 다 남기고 멈춰야 한다.
# 남은 시간이 이보다 적으면 리뷰 호출을 건너뛰고 규칙 검사만 돌린다.
VERIFY_RESERVE_S = float(os.environ.get("VERIFY_RESERVE_S", "8"))

# 우리 스스로를 밀어내지 않도록 상류 호출을 조인다. 요청 하나가 FM 을 최대 9회,
# MCP 를 6회까지 부르기 때문에 동시 요청이 몰리면 상류가 먼저 무너진다.
FM_CONCURRENCY = int(os.environ.get("FM_CONCURRENCY", "24"))
MCP_CONCURRENCY = int(os.environ.get("MCP_CONCURRENCY", "12"))

# L2 는 사고과정을 별도 `reasoning` 필드로 뱉는데, 그게 2048 예산을 통째로 먹는다.
# 실측(같은 질문):
#   thinking on  → reasoning 2492자 + content 881자, finish=length  (잘림)
#   thinking off → reasoning 0자    + content 576자, finish=stop     (완결, 395토큰)
# 상한이 2048 로 묶여 있는 한, thinking 을 켜면 긴 답변은 구조적으로 완결될 수 없다.
ENABLE_THINKING = os.environ.get("FM_THINKING", "0") == "1"

# L2 follows task instructions in the latest user turn more reliably than a
# separate system message. Keep this deliberately narrow: it prevents a generic
# referral from replacing an otherwise answerable medical response.
ANSWER_INSTRUCTION = (
    "Do not substitute 'consult a professional' for an answer; answer as far as you can."
)

_fm_sem = asyncio.Semaphore(FM_CONCURRENCY)
_client: httpx.AsyncClient | None = None
# MCP 도 같은 팀 키를 쓴다. 모듈 로드 시점의 환경변수를 각자 읽게 두면
# 폴백이 한쪽에만 걸리므로 여기서 명시적으로 넘긴다.
MCP = MCPClient(key=FM_KEY, concurrency=MCP_CONCURRENCY)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # httpx 클라이언트를 요청마다 새로 만들면 연결이 재사용되지 않고, 장시간 대량
    # 요청에서 소켓이 쌓인다. 하나를 띄워두고 공유한다.
    global _client
    _client = httpx.AsyncClient(
        timeout=TIMEOUT,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )
    # 키가 없어도 컨테이너는 뜬다 — "수동 작업 없이 시작" 요구사항 때문.
    # 대신 여기서 크게 남겨서 평가 로그만 봐도 원인을 알 수 있게 한다.
    if not FM_KEY:
        log.error("LUNIT_FM_API_KEY 가 비어 있다. 모든 생성 요청이 실패한다.")
    log.info(
        "driver up — model=%s max_tokens=%d thinking=%s budget=%.0fs fm_conc=%d mcp_conc=%d",
        FM_MODEL, MAX_TOKENS, ENABLE_THINKING, REQUEST_BUDGET_S, FM_CONCURRENCY, MCP_CONCURRENCY,
    )
    try:
        yield
    finally:
        await _client.aclose()
        await MCP.aclose()


app = FastAPI(title="AIM conversation driver", lifespan=lifespan)


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
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": ENABLE_THINKING},
        **(extra or {}),
    }
    assert _client is not None, "lifespan 이 클라이언트를 만들기 전에 호출됐다"
    last: Exception | None = None
    for attempt in range(FM_RETRIES):
        try:
            async with _fm_sem:
                r = await _client.post(
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


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
    return ""


def _with_answer_instruction(messages: list[dict]) -> list[dict]:
    """최신 사용자 턴에만 답변 행동 지시를 붙인다. 히스토리는 건드리지 않는다."""
    forwarded = [dict(message) for message in messages]
    if forwarded and forwarded[-1].get("role") == "user":
        content = forwarded[-1].get("content")
        if isinstance(content, str):
            forwarded[-1]["content"] = f"{content}\n\n[{ANSWER_INSTRUCTION}]"
    return forwarded


async def generate_reply(messages: list[dict], dl: Deadline) -> str:
    """라우팅 → MCP 근거 검색 → 생성 → 출력 검증 순으로 다음 발화를 만든다."""
    route = await classify(messages, call_fm)
    log.info(
        "route: domain=%s urgency=%s persona=%s lang=%s date=%s src=%s tools=%d",
        route.domain, route.urgency, route.persona, route.lang,
        route.date_sensitive, route.source, len(route.tools),
    )

    # 응급이면 검색을 짧게 끊는다. 응급에서 값을 내는 건 근거 인용이 아니라 즉시 의뢰다.
    budget = EMERGENCY_BUDGET if route.urgency == "emergency" else RETRIEVAL_BUDGET

    # 검색은 답을 쓸 시간과 검증할 시간을 둘 다 남기고 멈춰야 한다.
    content, retr = await generate(
        _with_answer_instruction(messages),
        route,
        call_fm,
        MCP,
        MAX_TOKENS,
        budget,
        deadline=dl,
        reserve=ANSWER_RESERVE_S + VERIFY_RESERVE_S,
    )
    if not content:
        log.error("generation 이 빈 content 를 냈다 — elapsed=%.1fs", dl.elapsed)
        return content

    content, issues = await verify(
        content,
        _last_user_text(messages),
        retr.as_prompt() if retr else "",
        len(retr.items) if retr else 0,
        call_fm,
        MAX_TOKENS,
        lang=route.lang,
        deadline=dl,
        reserve=VERIFY_RESERVE_S,
    )
    if issues:
        log.info("검증 지적 %d건: %s", len(issues), "; ".join(issues)[:300])
    if not content:
        log.error("검증 후 content 가 비었다 — elapsed=%.1fs", dl.elapsed)
    return content


@app.post("/v1/chat/completions")
async def chat_completions(body: dict) -> dict[str, Any]:
    messages = body.get("messages") or []
    dl = Deadline.start(REQUEST_BUDGET_S)
    try:
        # 단계마다 남은 시간을 보며 스스로 줄이지만, 그래도 넘기면 여기서 끊는다.
        content = await asyncio.wait_for(
            generate_reply(messages, dl), timeout=REQUEST_BUDGET_S + 20
        )
    except asyncio.TimeoutError:
        # 여기서 빈 문자열을 흘리면 그 문항은 0점이다. 도구도 라우팅도 없이
        # 한 번만 더, 짧게 답을 받아 본다. 늦은 답이 없는 답보다 낫다.
        log.error("요청 시간 초과 — 직답으로 되살린다 (elapsed=%.1fs)", dl.elapsed)
        try:
            data = await asyncio.wait_for(call_fm(messages, MAX_TOKENS), timeout=60)
            content = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception:
            log.exception("직답 폴백도 실패")
            content = ""
    except Exception:
        # 평가 하네스에 5xx 를 돌려주면 대화 전체가 깨질 수 있다.
        # 그래서 형식은 지키되, 실패는 로그에 남겨 사후에 반드시 보이게 한다.
        log.exception("생성 실패")
        content = ""

    log.info("응답 %d자 / %.1fs", len(content), dl.elapsed)
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
