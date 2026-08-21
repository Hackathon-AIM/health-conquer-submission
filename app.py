"""제출용 멀티턴 대화 드라이버 — OpenAI 호환 서버.

평가자(Evaluator)가 각 대화 턴을 POST /v1/chat/completions 로 보내고,
이 서버가 다음 assistant 응답을 돌려준다.

요청 하나의 흐름:
  라우터가 도메인·응급도·맥락 충분성·페르소나를 정하고,
  그 판정에 따라 되묻기 / 직답 / 검색-후-답변으로 갈린다.
"""

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from budget import Deadline
from generation import generate
from mcp_client import MCPClient
from router import classify

# ── 우리 층 ─────────────────────────────────────────────────
# drug_safety 는 stdlib 만 쓴다. 그래도 import 를 감싸는 이유: 이 파일이 어떤 이유로든
# 못 읽히면 서버가 통째로 못 뜨고, 그건 전 문항 0점이다. 실제로 그렇게 네 번 죽었다.
try:
    from drug_safety import guard as _drug_guard
except Exception as _e:                                 # pragma: no cover
    logging.getLogger("driver").error("약물 안전 게이트 비활성: %r", _e)

    def _drug_guard(_convo, answer):
        return answer

# 응급 안내가 이미 첫머리에 있으면 덧대지 않는다 (중복 = 감점).
_EMERGENCY_LEAD = re.compile(r"119|응급실|즉시 (?:병원|진료|의료)")
# [1] · [1,2] · [1, 2] 를 모두 잡는다 — 실측에서 다 나왔다.
_CITE_MARK = re.compile(r"\s*\[\s*\d+(?:\s*[,·]\s*\d+)*\s*\]")

# ── 근거가 필요한 질문만 MCP 를 태운다 ─────────────────────────
# 기준선(하네스 없이 L2 그대로)이 38.34 를 받았고, 하네스를 항상 태운 판은 29.13 이었다.
# 그러니 하네스를 전면 복원하는 건 도박이다. 대신 **L2 가 알 수 없는 것**에만 태운다:
# 한국 법령 조문·급여기준·고시·약가·질병코드·진료지침. 이건 모델 안에 없고 문서에만 있다.
# 나머지 질문은 38.34 와 완전히 같은 경로로 간다.
_NEEDS_DOCS = re.compile(
    r"제\s*\d+\s*조|시행령|시행규칙|법령|법\s*상|고시|급여|비급여|본인부담|상한제|"
    r"수가|청구|산정|약가|약값|상한금액|KCD|상병\s*코드|질병\s*코드|허가사항|"
    r"효능효과|용법|가이드라인|진료지침|권고\s*(기준|사항|등급)|근거\s*수준"
)
# 인덱스 코퍼스는 둘뿐이고 어느 쪽인지는 도메인이 이미 안다.
# 실측: "가이드라인상 혈압 목표" 질문에서 모델이 corpus_tag="hira"(급여기준)를 골라
# 5회를 다 쓰고 관련 없는 페이지 하나만 열었다. 모델 재량에 맡길 이유가 없다.
_CORPUS_HINT = {"guideline_index": "guideline", "hira_updates": "hira"}
_DOC_DOMAINS = {"guideline_index", "korean_law", "hira_updates", "kcd",
                "hira_drug_price", "mfds"}

# 인덱스 코퍼스는 "찾기 → 열기"가 최소 2단계다. cite_uid 는 본문을 열어야만 나오므로
# 3회로는 못 연다(실측: calls=3 items=0). 시간은 run_retrieval 이 매 스텝 deadline 을
# 보고 스스로 끊으므로 예산을 늘려도 REQUEST_BUDGET_S 를 넘지 않는다.
# ⚠️ 상수로 둔다 — os.environ.get(x, "5") 는 x 가 빈 문자열이면 int("") 로 터지고,
#    그건 모듈 import 실패이자 컨테이너 기동 실패다.
DOC_RETRIEVAL_BUDGET = 5

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
        "temperature": 0.3,
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


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _strip_bad_citations(content: str, n_items: int) -> str:
    """근거 개수를 넘는 인용번호를 지운다.

    없는 근거를 가리키는 인용은 인용이 없는 것보다 나쁘다 — 읽는 사람이
    "확인된 사실"로 받아들이기 때문이다. 번호가 전부 유효하면 손대지 않는다.
    실측: 근거 1건인데 [1,2] 를 달았고, 그 1건은 답변 내용과 무관한 문서였다.
    """
    def fix(m):
        nums = [int(x) for x in re.findall(r"\d+", m.group(0))]
        good = [x for x in nums if 1 <= x <= n_items]
        if len(good) == len(nums):
            return m.group(0)
        if not good:
            return ""
        lead = m.group(0)[:len(m.group(0)) - len(m.group(0).lstrip())]
        return lead + "[" + ",".join(str(x) for x in good) + "]"
    return _CITE_MARK.sub(fix, content)


def _render_sources(content: str, items: list) -> str:
    """살아남은 인용번호에만 출처를 붙인다.

    프론티어 모델과 블라인드로 비교당하는 자리에서 우리가 이길 수 있는 건
    **한국 문서에 실제로 근거한 답**뿐이다. 그런데 본문에 [1] 만 있고 그게
    무엇인지 화면에 없으면, 읽는 사람에겐 근거가 아니라 지어낸 것처럼 보인다.
    """
    if not content or not items:
        return content
    used = sorted({n for m in _CITE_MARK.finditer(content)
                   for n in (int(x) for x in re.findall(r"\d+", m.group(0)))
                   if 1 <= n <= len(items)})
    lines = []
    for n in used:
        ev = items[n - 1]
        label = (getattr(ev, "title", "") or "").strip()
        url = (getattr(ev, "url", "") or "").strip()
        if not label and not url:
            continue
        lines.append("[%d] %s%s" % (n, label or url, (" — " + url) if label and url else ""))
    if not lines:
        return content
    return content.rstrip() + "\n\n---\n**참고한 자료**\n" + "\n".join(lines)


async def _grounded_reply(messages: list[dict], dl: Deadline) -> str:
    """MCP 로 한국 문서를 열어 근거를 붙여 답한다.

    이 경로에서 무엇이 실패하든 예외를 올린다 — 호출부가 기준선으로 되돌린다.
    """
    route = await classify(messages, call_fm)
    log.info("route: domain=%s urgency=%s persona=%s tools=%d",
             route.domain, route.urgency, route.persona, len(route.tools))

    # 문서 도메인이 아니면 굳이 검색하지 않는다. 기준선이 더 낫다.
    if route.domain not in _DOC_DOMAINS or not route.tools:
        raise RuntimeError("문서 도메인이 아니다: %s" % route.domain)

    hint = _CORPUS_HINT.get(route.domain)
    if hint and route.search_query:
        route.search_query = (
            route.search_query
            + '\n(Use corpus_tag="%s" for index tools — this question belongs to '
              'the %s corpus.)' % (hint, hint)
        )

    if dl.expired(reserve=ANSWER_RESERVE_S):
        raise RuntimeError("검색할 시간이 없다")

    content, result = await generate(
        messages, route, call_fm, MCP, MAX_TOKENS,
        DOC_RETRIEVAL_BUDGET, dl, ANSWER_RESERVE_S,
    )
    for step in (getattr(result, "trace", None) or []):
        log.info("  retrieval· %s", step)
    if not content:
        raise RuntimeError("근거 경로가 빈 답을 냈다")

    items = list(getattr(result, "items", None) or [])
    fixed = _strip_bad_citations(content, len(items))
    if fixed != content:
        log.info("근거 %d건을 넘는 인용번호를 제거했다", len(items))
        content = fixed
    return _render_sources(content, items)


async def generate_reply(messages: list[dict], dl: Deadline) -> str:
    """기본은 기준선(L2 그대로). 문서가 있어야 답할 수 있는 질문만 MCP 를 태운다."""
    text = _last_user(messages)
    if text and _NEEDS_DOCS.search(text):
        try:
            return await _grounded_reply(messages, dl)
        except Exception as e:      # noqa: BLE001 — 근거 경로는 부가 기능이다
            log.warning("근거 경로 미사용/실패 → 기준선으로 (%s)", str(e)[:120])

    data = await call_fm(messages, MAX_TOKENS)
    content = (data["choices"][0]["message"].get("content") or "").strip()
    if not content:
        log.error("L2 raw 응답의 content가 비었다 — elapsed=%.1fs", dl.elapsed)
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

    # 약물 안전 게이트 — 답변에 등장한 약을 대화 맥락(음주·임신·복용 중 약)과 대조한다.
    # 응급 답변에는 얹지 않는다: 첫 문단이 119 안내여야 하는데 그 앞을 뺏으면 손해다.
    try:
        if content and not _EMERGENCY_LEAD.search(content[:200]):
            convo = "\n".join(
                "%s: %s" % (m.get("role", ""), m.get("content", ""))
                for m in messages if isinstance(m, dict)
            )
            content = _drug_guard(convo, content)
    except Exception:                       # noqa: BLE001 — 답변을 잃는 것이 최악이다
        log.exception("약물 안전 게이트 실패 — 원문 유지")

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
