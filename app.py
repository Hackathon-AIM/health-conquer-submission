"""제출용 멀티턴 대화 드라이버 — OpenAI 호환 서버.

평가자(Evaluator)가 각 대화 턴을 POST /v1/chat/completions 로 보내고,
이 서버가 다음 assistant 응답을 돌려준다.

요청 하나의 흐름:
  1. 초안 — PIPELINE 이 경로를 고른다
       raw     : 대화를 그대로 L2 에 넘기고 thinking 으로 한 번에 받는다 (기본)
       harness : 라우팅 → MCP 근거 검색 → 근거를 얹어 생성
  2. 검증 — review 가 내보내기 전에 "질문에 답했는가"와 "이상한 데가 없는가"를 본다.
       규칙에 걸린 것만 L2 에게 되묻는다. 정상 답변에는 추가 호출이 없다.

단계마다 남은 시간을 보고 스스로 줄인다. 답을 못 내는 것이 가장 나쁘므로
어느 단계가 실패해도 그때까지 만든 답으로 내려간다.
"""

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from budget import MIN_CALL_S, Deadline, answer_timeout, call_cap
from generation import generate
from mcp_client import MCPClient
from review import REVIEW_MODE, review
from router import classify

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

# ── 출력 예산 ────────────────────────────────────────────────
#
# 컨텍스트 창은 병목이 아니다. 실측: max_model_len=131072 이고 프롬프트
# 60,016 토큰도 200 이 온다. conquer_val 프롬프트는 중앙 500자·최대 4,216자라
# 창의 0.4% 도 안 쓴다. 상한은 max_tokens 32768 (32769 는 400) 과, 그와 별개인
# 메시지 바이트 한도(아주 큰 단일 메시지에서 message_too_large)뿐이다.
#
# 진짜 희소 자원은 **max_tokens 안에서 thinking 과 답변이 나눠 쓰는 몫**이다.
# 같은 12문항을 max_tokens 별로 재보면:
#   2048  잘림 1/12 · thinking 비중 48% · 답변 중앙 3343자 · 지연 중앙 31.3s
#   4096  잘림 0/12 · thinking 비중 46% · 답변 중앙 3673자 · 지연 중앙 21.4s (최대 70.0s)
#   6144  잘림 0/12 · thinking 비중 50% · 답변 중앙 2764자 · 지연 중앙 22.7s (최대 39.6s)
#   8192  잘림 0/12 · thinking 비중 49% · 답변 중앙 2777자 · 지연 중앙 24.4s
# 2048 만 잘리고, 그 위로는 지연이 사실상 평평하다. 6144 를 쓰는 이유는
# 리더보드 설정(conquer_val.yaml)이 client max_tokens 를 6144 로 두고 있고,
# 그 파일이 "4096 에서는 2% 가 예산을 전부 thinking 에 쓰고 빈 content 를
# 돌려줬다" 고 적고 있기 때문이다. 빈 답은 확정 0점이다.
SERVER_MAX_TOKENS = 32768
MAX_TOKENS = min(int(os.environ.get("FM_MAX_TOKENS", "6144")), SERVER_MAX_TOKENS)
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

# ── 초안 경로 선택 ────────────────────────────────────────────
#
#   raw     : L2 에게 대화를 그대로 넘기고 thinking 으로 한 번에 답을 받는다.
#             팀 실측에서 가장 높은 점수가 나온 경로다(50.03). 도구를 쓰지 않는다.
#   harness : 라우팅 → MCP 근거 검색 → 근거를 얹어 생성. 근거·인용이 필요한
#             질문에서 값을 내지만, 요청당 상류 호출이 늘어 지연 위험이 크다.
#             과거 이 경로의 계보가 38.34 였고, 느려서 0.00 을 받은 적도 있다.
#
# 기본을 raw 로 두는 이유는 하나다 — 측정된 최고점이 거기 있다.
# harness 는 CoEval 로 재고 나서 켜는 것이 맞다.
PIPELINE = os.environ.get("PIPELINE", "raw").strip().lower()

# ── 시간 예산 가드 ────────────────────────────────────────────
# 예산을 40초로 정해 놓고도 그 예산이 지켜지지 않던 이유가 여기 있었다.
# 호출 하나의 timeout(FM 120s · MCP 60s)이 요청 전체 예산보다 크고, 거기에
# 재시도 3회가 곱해진다. 최악은 FM 한 번이 120×3+백오프 ≈ 365초다.
# 예산은 "언제 멈출지" 만 정했고 "얼마나 기다릴지" 는 아무도 정하지 않았다.
#
# 가드를 켜면 모든 상류 호출의 timeout 을 남은 시간에서 깎아 만든다. 재시도까지
# 합쳐서 그 상한을 넘지 않는다. 0 으로 두면 예전 동작 그대로다(A/B 용).
DEADLINE_GUARD = os.environ.get("DEADLINE_GUARD", "1") == "1"

# 단계별 호출 하나의 상한. 남은 시간이 이보다 적으면 남은 시간 쪽이 이긴다.
CLASSIFY_CAP_S = float(os.environ.get("CLASSIFY_CAP_S", "12"))
FM_CALL_CAP_S = float(os.environ.get("FM_CALL_CAP_S", "25"))

# 최종 답변 호출만은 남은 시간으로 깎지 않는다. 예산은 답을 지키려고 있는 것이라
# 그걸로 답변을 깎으면 앞 단계가 흘린 시간만큼 답이 굶는다. 실측: 동시 30건에서
# 답변 호출을 remaining 으로 깎았더니 6/30 이 빈 답(=0점)이 됐다.
ANSWER_CAP_S = float(os.environ.get("ANSWER_CAP_S", "45"))
ANSWER_FLOOR_S = float(os.environ.get("ANSWER_FLOOR_S", "25"))

# ── thinking 을 켤 시간이 있는가 ──────────────────────────────
#
# 실측: 같은 12문항에서 thinking 을 켜면 지연 중앙 21.4s(최대 70.0s), 끄면
# 5.3s(최대 17.1s) 다. 4배다. 답변 길이는 3673자 대 1749자로 두 배 차이라
# 켜는 쪽이 기본적으로 유리하지만, **남은 시간이 없을 때는 얘기가 다르다.**
# 20초 남은 상태에서 thinking 을 켜면 대개 아무것도 못 받고, 그 문항은
# 짧은 답이 아니라 빈 답이 된다.
#
# 그래서 남은 시간이 이 값보다 적으면 처음부터 thinking 없이 간다.
# 사후 폴백(잘리면 다시 부르기)과 다르다 — 그쪽은 이미 시간을 다 쓴 뒤다.
THINKING_MIN_S = float(os.environ.get("THINKING_MIN_S", "26"))

# 어떤 이유로든 본 경로가 실패했을 때의 구조 호출. 이 호출의 목적은 품질이
# 아니라 0점 회피다.
#
# ★ 구조 호출은 **짧은 답**을 요구해야 한다. 실측에서 초안이 40초에 끊긴 뒤
#   구조 호출까지 4096토큰을 요구했다가 30초 안에 못 받아 빈 답이 나왔다.
#   4096토큰은 생성만으로도 수십 초다 — 이미 시간이 없어서 여기 온 건데 같은
#   길이를 다시 요구하는 것은 앞의 실패를 반복하겠다는 뜻이다.
#   짧은 답은 감점이지만 빈 답은 0점이다.
RESCUE_CAP_S = float(os.environ.get("RESCUE_CAP_S", "30"))
RESCUE_MAX_TOKENS = int(os.environ.get("RESCUE_MAX_TOKENS", "1024"))

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
        "driver up — model=%s max_tokens=%d pipeline=%s review=%s budget=%.0fs "
        "fm_conc=%d mcp_conc=%d guard=%s",
        FM_MODEL, MAX_TOKENS, PIPELINE, REVIEW_MODE, REQUEST_BUDGET_S,
        FM_CONCURRENCY, MCP_CONCURRENCY, DEADLINE_GUARD,
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
    messages: list[dict],
    max_tokens: int,
    extra: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """FM 한 번 호출. `extra` 로 response_format 같은 vLLM 파라미터를 얹는다.

    일시적 실패(502·타임아웃 등)는 지수 백오프로 되돌려 시도한다.
    영구적 실패(400 output_limit_exceeded 등)는 즉시 올린다 — 재시도해도 같다.

    `timeout` 은 **재시도와 백오프까지 포함한** 이 호출 전체의 상한이다.
    주지 않으면 예전처럼 시도마다 FM_TIMEOUT 을 쓴다 — 그 경우 최악이
    FM_TIMEOUT×FM_RETRIES 라 요청 예산을 통째로 넘긴다. 시간이 걸린 경로에서는
    반드시 넘겨라. 상한을 소진하면 마지막 예외를, 예외가 없었으면 TimeoutError 를 올린다.
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
    # timeout 을 안 주면 예전과 같은 최악(시도마다 TIMEOUT)을 그대로 쓴다.
    total = timeout if (timeout and timeout > 0) else TIMEOUT * FM_RETRIES
    started = time.monotonic()

    def _left() -> float:
        return total - (time.monotonic() - started)

    last: Exception | None = None
    for attempt in range(FM_RETRIES):
        if _left() <= 0:
            log.warning("FM 시간 예산 소진 — %d/%d 시도에서 중단", attempt + 1, FM_RETRIES)
            break
        try:
            async with _fm_sem:
                # 세마포어 대기도 예산을 먹는다. 잡고 나서 다시 재어야 정확하다.
                left = _left()
                if left <= 0:
                    break
                r = await _client.post(
                    f"{FM_URL}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {FM_KEY}"},
                    json=payload,
                    timeout=min(TIMEOUT, left),
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
            backoff = FM_BACKOFF * (2**attempt)
            # 백오프를 기다리고 나면 부를 시간이 없다면, 기다리는 것 자체가 손해다.
            if _left() <= backoff + MIN_CALL_S:
                log.warning("FM 재시도 포기 — 남은 예산 %.1fs", _left())
                break
            await asyncio.sleep(backoff)

    if last is None:
        raise TimeoutError(f"FM 호출이 시간 예산 {total:.0f}s 안에 시작되지 못했다")
    raise last


def _timed(call, timeout: float | None):
    """call_fm 을 timeout 에 묶어서 돌려준다.

    router.classify 처럼 call_fm 의 시그니처를 모르는 하위 모듈에 시간 예산을
    주입하는 유일한 방법이다. 하위 모듈은 자기가 시간에 묶였다는 걸 모른 채 돈다.
    """
    if timeout is None:
        return call

    async def _call(messages, max_tokens, extra=None, **kw):
        kw.setdefault("timeout", timeout)
        return await call(messages, max_tokens, extra, **kw)

    return _call


async def _direct_answer(
    messages: list[dict], dl: Deadline, max_tokens: int | None = None
) -> str:
    """라우팅도 검색도 없이 답만 받는다. 시간이 없을 때의 마지막 경로다.

    `max_tokens` 를 줄여 부르면 생성 시간 자체가 줄어든다. 시간이 없어서 여기
    왔으므로 길이를 낮추는 것이 이 경로가 성공할 확률을 직접 올린다.
    thinking 도 끈다 — 사고 토큰이 예산의 대부분을 먹는다.
    """
    t = answer_timeout(dl, ANSWER_CAP_S, ANSWER_FLOOR_S) if DEADLINE_GUARD else None
    data = await call_fm(
        _with_answer_instruction(messages),
        max_tokens or MAX_TOKENS,
        {"chat_template_kwargs": {"enable_thinking": False}},
        timeout=t,
    )
    return (data["choices"][0]["message"].get("content") or "").strip()


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
    return ""


def _lang_of(text: str) -> str:
    """답변 언어는 질문 언어를 따른다. 불일치는 의사소통 축에서 그대로 감점이다."""
    return "ko" if re.search(r"[가-힣]", text) else "en"


def _with_answer_instruction(messages: list[dict]) -> list[dict]:
    """최신 사용자 턴에만 답변 행동 지시를 붙인다. 히스토리는 건드리지 않는다."""
    forwarded = [dict(message) for message in messages]
    if forwarded and forwarded[-1].get("role") == "user":
        content = forwarded[-1].get("content")
        if isinstance(content, str):
            forwarded[-1]["content"] = f"{content}\n\n[{ANSWER_INSTRUCTION}]"
    return forwarded


async def _draft_raw(messages: list[dict], dl: Deadline) -> tuple[str, str, int, str]:
    """SOTA 경로 — 대화를 그대로 L2 에 넘기고 thinking 으로 답을 받는다.

    thinking 응답이 max_tokens 에 걸려 잘리면 thinking 없이 한 번 더 받는다.
    돌려주는 것: (초안, 답변 언어, 근거 개수=0, 근거 텍스트="")
    """
    forwarded = _with_answer_instruction(messages)
    cap = answer_timeout(dl, ANSWER_CAP_S, ANSWER_FLOOR_S) if DEADLINE_GUARD else None

    # 남은 시간이 thinking 을 감당 못 하면 처음부터 켜지 않는다.
    if DEADLINE_GUARD and dl.remaining() < THINKING_MIN_S:
        log.info(
            "thinking 생략 — 남은 %.1fs < %.0fs (짧아도 답이 있는 편이 낫다)",
            dl.remaining(), THINKING_MIN_S,
        )
        data = await call_fm(
            forwarded,
            MAX_TOKENS,
            {"chat_template_kwargs": {"enable_thinking": False}},
            timeout=cap,
        )
        content = (data["choices"][0]["message"].get("content") or "").strip()
        return content, _lang_of(_last_user_text(messages)), 0, ""

    try:
        data = await call_fm(
            forwarded,
            MAX_TOKENS,
            {"chat_template_kwargs": {"enable_thinking": True}},
            timeout=cap,
        )
    except Exception as e:  # noqa: BLE001
        # thinking 호출이 시간에 걸렸다. 예외를 올리면 요청 전체가 구조 경로로
        # 떨어지는데 그쪽은 시간이 더 없다. 같은 자리에서 thinking 을 끄고 짧게
        # 다시 받는 편이 훨씬 산다.
        log.warning(
            "thinking 호출 실패(%s) — thinking 없이 짧게 다시 받는다 (elapsed=%.1fs)",
            type(e).__name__,
            dl.elapsed,
        )
        return (
            await _direct_answer(messages, dl, RESCUE_MAX_TOKENS),
            _lang_of(_last_user_text(messages)),
            0,
            "",
        )
    choice = data["choices"][0]
    content = (choice["message"].get("content") or "").strip()
    if choice.get("finish_reason") == "length" or not content:
        log.info(
            "thinking 응답 손상 — 기존 경로로 폴백 (finish=%s content=%d elapsed=%.1fs)",
            choice.get("finish_reason"), len(content), dl.elapsed,
        )
        cap = answer_timeout(dl, ANSWER_CAP_S, ANSWER_FLOOR_S) if DEADLINE_GUARD else None
        data = await call_fm(
            forwarded, MAX_TOKENS, {"chat_template_kwargs": {"enable_thinking": False}}, timeout=cap
        )
        content = (data["choices"][0]["message"].get("content") or "").strip()
    return content, _lang_of(_last_user_text(messages)), 0, ""


async def _draft_harness(messages: list[dict], dl: Deadline) -> tuple[str, str, int, str]:
    """하네스 경로 — 라우팅 → MCP 근거 검색 → 근거를 얹어 생성.

    돌려주는 것: (초안, 답변 언어, 근거 개수, 근거 텍스트)
    """
    keep = ANSWER_RESERVE_S + VERIFY_RESERVE_S
    if DEADLINE_GUARD and dl.expired(reserve=keep):
        # 답 쓸 시간만 남았다. 라우팅을 포기하고 답을 낸다 — 빈 답이 가장 나쁘다.
        log.info("분류 생략 — 남은 %.1fs 로 직답", dl.remaining())
        content = await _direct_answer(messages, dl)
        return content, _lang_of(_last_user_text(messages)), 0, ""

    route = await classify(
        messages,
        _timed(call_fm, call_cap(dl, CLASSIFY_CAP_S, reserve=keep) if DEADLINE_GUARD else None),
    )
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
        deadline=dl if DEADLINE_GUARD else None,
        reserve=keep,
        answer_cap=ANSWER_CAP_S,
        answer_floor=ANSWER_FLOOR_S,
    )
    n_ev = len(retr.items) if retr else 0
    return content, route.lang, n_ev, (retr.as_prompt() if retr else "")


async def generate_reply(messages: list[dict], dl: Deadline) -> str:
    """초안을 만들고, 내보내기 전에 출력 검증층을 한 번 통과시킨다.

    초안 경로는 PIPELINE 이 정하고, 검증층은 두 경로가 같은 것을 쓴다.
    검증층은 대부분의 요청에서 FM 을 부르지 않는다 — 규칙에 걸린 것만 되묻는다.
    """
    draft_fn = _draft_harness if PIPELINE == "harness" else _draft_raw
    content, lang, n_evidence, evidence = await draft_fn(messages, dl)
    if not content:
        log.error("초안이 비었다 (pipeline=%s) — elapsed=%.1fs", PIPELINE, dl.elapsed)
        return content

    if REVIEW_MODE == "off":
        return content

    before = len(content)
    reviewed, notes = await review(
        content,
        _last_user_text(messages),
        call_fm,
        lang=lang,
        deadline=dl if DEADLINE_GUARD else None,
        n_evidence=n_evidence,
        evidence=evidence,
    )
    if notes:
        log.info(
            "검증 %d→%d자 · 근거 %d건 · 신호 %d건: %s",
            before, len(reviewed), n_evidence, len(notes), "; ".join(notes)[:300],
        )
    if not reviewed:
        # 검증층은 절대 빈 답을 만들면 안 된다. 왔다면 버그이므로 초안을 지킨다.
        log.error("검증이 빈 답을 냈다 — 초안 유지 (elapsed=%.1fs)", dl.elapsed)
        return content
    return reviewed


@app.post("/v1/chat/completions")
async def chat_completions(body: dict) -> dict[str, Any]:
    messages = body.get("messages") or []
    dl = Deadline.start(REQUEST_BUDGET_S)
    try:
        # 단계마다 남은 시간을 보며 스스로 줄이지만, 그래도 넘기면 여기서 끊는다.
        content = await asyncio.wait_for(
            generate_reply(messages, dl), timeout=REQUEST_BUDGET_S + 20
        )
    except Exception as e:  # noqa: BLE001
        # 실패 종류로 갈라서는 안 된다. 예전에는 asyncio.TimeoutError 만 되살리고
        # 나머지는 빈 문자열로 내려보냈는데, httpx.ReadTimeout 은 TimeoutError 가
        # 아니라서 그 문이 닫혀 있었다 — 실측에서 6/30 이 이 문으로 빠졌다.
        # 어떤 이유로 실패했든 답은 내야 한다. 빈 답은 확정 0점이다.
        log.warning("본 경로 실패(%s) — 직답으로 되살린다 (elapsed=%.1fs)",
                    type(e).__name__, dl.elapsed)
        try:
            content = await asyncio.wait_for(
                _direct_answer(messages, dl, RESCUE_MAX_TOKENS), timeout=RESCUE_CAP_S + 10
            )
        except Exception:
            # 평가 하네스에 5xx 를 돌려주면 대화 전체가 깨질 수 있다.
            # 그래서 형식은 지키되, 실패는 로그에 남겨 사후에 반드시 보이게 한다.
            log.exception("직답 폴백도 실패")
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
