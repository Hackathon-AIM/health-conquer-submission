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

# ── 우리 층: 약물 안전 게이트 ───────────────────────────────────
# safety 는 응급(119)만 본다. "와파린+아스피린", "음주 후 진통제" 같은
# 약물 상호작용은 아무도 안 본다. 그리고 실제 질문은 "이거 먹어도 되나요"보다
# "뭘 먹으면 되나요"가 많아서, 검사해야 할 대상은 입력이 아니라 **모델의 답변**이다.
# 결정론·동기·무통신이라 지연이 사실상 0이고, 실패해도 원문을 그대로 돌려준다.
try:
    from drug_safety import guard as _drug_guard
except Exception as _e:                                    # 사전이 없어도 서버는 뜬다
    logging.getLogger("driver").error("약물 안전 게이트 비활성: %r", _e)
    def _drug_guard(_convo: str, answer: str) -> str:      # type: ignore[misc]
        return answer

# 응급 안내가 이미 첫머리에 있으면 경고를 덧대지 않는다 (중복 = 감점).
_EMERGENCY_LEAD = re.compile(r"119|응급실|즉시 (?:병원|진료|의료)")

# ── 대화 이력 상한 ────────────────────────────────────────────
# generation 은 이력을 그대로 넘긴다(convo.extend(messages)). 거기에 검색 근거까지
# 얹히므로 긴 멀티턴에서 input_limit_exceeded 가 날 수 있고, 그러면 그 턴은 통째로
# 0점이다. 이전 하네스에서 실제로 겪었다(tool_result 12,000자 × 8건 = 96,000자).
# 개선이 아니라 보험이다. 그래서 상한은 넉넉하게 잡는다.
HISTORY_MAX_CHARS = int(os.environ.get("HISTORY_MAX_CHARS", "24000"))
HISTORY_MAX_TURNS = int(os.environ.get("HISTORY_MAX_TURNS", "16"))


def trim_history(messages: list[dict]) -> list[dict]:
    """뒤에서부터 예산 안에 들어오는 만큼만 남긴다. 최근 턴이 가장 중요하다."""
    if len(messages) <= 2:
        return messages
    kept: list[dict] = []
    total = 0
    for m in reversed(messages[-HISTORY_MAX_TURNS:]):
        n = len(str(m.get("content") or ""))
        # 마지막 두 개(직전 답변 + 지금 질문)는 예산과 무관하게 지킨다.
        if kept and total + n > HISTORY_MAX_CHARS and len(kept) >= 2:
            break
        kept.append(m)
        total += n
    kept.reverse()
    if len(kept) < len(messages):
        log.info("이력 축소: %d턴 → %d턴 (%d자)", len(messages), len(kept), total)
    return kept


# ── 응급 안전망 ───────────────────────────────────────────────
# 응급 판정인데 답변이 응급 안내로 시작하지 않으면, 그때만 앞에 붙인다.
# 평소엔 모델이 알아서 넣는다(실측). 그래서 "항상"이 아니라 "놓쳤을 때만"이다.
# 항상 붙이면 중복이 되고 중복은 의사소통 감점이다.
_EMERGENCY_LEAD_IN = (
    "**지금 119에 연락하거나 가까운 응급실로 가세요.** "
    "말씀하신 증상은 즉시 진료가 필요할 수 있습니다. "
    "혼자 운전하지 마시고 주변 사람에게 도움을 요청하세요."
)
# 근거 0건인데 붙은 인용번호는 환각이다. 프롬프트로만 막던 것을 코드로 막는다.
# [1] 뿐 아니라 [1,2] · [1, 2] 형태도 잡는다 — 실측에서 이 형태가 나왔다.
_CITE_MARK = re.compile(r"\s*\[\s*\d+(?:\s*[,·]\s*\d+)*\s*\]")


# 실측: 답변이 "Assistant" 한 줄로 시작해서 나왔다. 채팅 템플릿 누수다.
# 사람이 읽으면 바로 눈에 띄고 의사소통(21.2%)에서 값을 잃는다.
_ROLE_LEAK = re.compile(r"^\s*(?:Assistant|assistant|어시스턴트)\s*[:：]?\s*\n+")


def strip_role_leak(content: str) -> str:
    return _ROLE_LEAK.sub("", content, count=1)


# ── 출처 표기 ────────────────────────────────────────────────
# 프론티어 상은 임상의가 우리 답과 프론티어 모델 답을 **나란히 놓고 블라인드로** 읽는다.
# 5B 모델이 문장력·지식 폭으로 프론티어를 이길 수는 없다. 이길 수 있는 자리는 하나뿐이다 —
# **한국 문서에 실제로 근거한 답**(급여기준·고시·법령 조문·허가사항). 프론티어는 그걸 못 본다.
#
# 그런데 지금은 본문에 [1] 만 떠 있고 그게 무엇인지 화면에 없다. 읽는 사람 입장에서
# 출처 없는 [1] 은 근거가 아니라 오히려 지어낸 것처럼 보인다. 우위를 만들어 놓고 안 보여주는 셈.
# 실제로 인용된 번호만, 제목/URL 이 있는 것만 짧게 붙인다 (완전성은 채점 4%뿐이다).
def render_sources(content: str, items: list) -> str:
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
        lines.append(f"[{n}] {label or url}" + (f" — {url}" if label and url else ""))
    if not lines:
        return content
    return content.rstrip() + "\n\n---\n**참고한 자료**\n" + "\n".join(lines)


def strip_bad_citations(content: str, n_items: int) -> str:
    """근거 개수를 넘는 인용번호를 지운다.

    없는 근거를 가리키는 인용은 인용이 없는 것보다 나쁘다 — 읽는 사람이
    "확인된 사실"로 받아들이기 때문이다. 정확성이 채점 43%다.
    번호가 전부 유효하면 그대로 둔다(진짜 인용까지 지우면 손해).
    """
    def fix(m: re.Match) -> str:
        nums = [int(x) for x in re.findall(r"\d+", m.group(0))]
        good = [x for x in nums if 1 <= x <= n_items]
        if len(good) == len(nums):
            return m.group(0)
        if not good:
            return ""
        lead = m.group(0)[:len(m.group(0)) - len(m.group(0).lstrip())]
        return f"{lead}[{','.join(map(str, good))}]"
    return _CITE_MARK.sub(fix, content)

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

# 인덱스 코퍼스(가이드라인·고시·법령)는 **찾기 → 열기**가 최소 2단계라
# 3회로는 본문을 못 연다. 열지 못한 것은 cite_uid 가 없어 인용 자체가 불가능하다.
#   실측: guideline_index / korean_law 질문이 calls=3 을 다 쓰고 items=0 으로 끝났다.
#         반면 단발 조회(mfds)는 calls=1 로 sufficient 를 받았다.
# 시간은 run_retrieval 이 매 스텝 deadline 을 보고 스스로 끊으므로, 예산을 늘려도
# REQUEST_BUDGET_S 를 넘기지 않는다. 늘려서 잃는 건 없고 못 열면 통째로 잃는다.
DEEP_DOMAINS = {"guideline_index", "korean_law", "hira_updates", "kcd"}
# 인덱스 코퍼스는 두 개다(임상 가이드라인 / 심평원 고시). 도메인이 곧 코퍼스다.
CORPUS_HINT = {"guideline_index": "guideline", "hira_updates": "hira"}
DEEP_BUDGET = int(os.environ.get("DEEP_RETRIEVAL_BUDGET", "5"))

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

# temperature 를 안 보내면 서버 기본값이 붙는다. 실측: 같은 질문("타이레놀 1회 최대 용량")에
# 세 번 물어 650~1,000mg / 500mg / 1,000mg 로 세 번 다 다르게 답했다. 허가사항은 하나인데
# 매번 다른 수치를 말하는 건 정확성(채점 43%)에서 그대로 깎이는 자리다.
# 0 이 아니라 0.3 인 이유: 완전 결정론은 표현이 뻣뻣해져 의사소통(21.2%)에서 손해를 볼 수 있고,
# 팀이 같은 값으로 트라이얼을 돌리고 있어 결과를 비교할 수 있다.
FM_TEMPERATURE = float(os.environ.get("FM_TEMPERATURE", "0.3"))

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
        "driver up — model=%s max_tokens=%d temp=%.2f thinking=%s budget=%.0fs "
        "fm_conc=%d mcp_conc=%d",
        FM_MODEL, MAX_TOKENS, FM_TEMPERATURE, ENABLE_THINKING, REQUEST_BUDGET_S,
        FM_CONCURRENCY, MCP_CONCURRENCY,
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
        "temperature": FM_TEMPERATURE,
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


async def generate_reply(messages: list[dict], dl: Deadline) -> str:
    route = await classify(messages, call_fm)
    log.info(
        "route: domain=%s urgency=%s context=%s persona=%s date=%s src=%s tools=%d",
        route.domain, route.urgency, route.context, route.persona,
        route.date_sensitive, route.source, len(route.tools),
    )

    # 결정적 맥락이 빠졌고 응급도 아니면, 답을 지어내지 말고 하나만 되묻는다.
    # 09 문서 §4 — 맥락인지는 Consensus 두 번째로 큰 축(24.7%)이고 프론티어가 무너지는 곳이다.
    #
    # 다만 **묻기만 하고 끝내지는 않는다.** 실측: 이 경로에서 76자짜리
    # "어떤 두통약을 고를지는 상황에 따라 완전히 달라집니다."만 나간 턴이 있었다.
    # 답도 아니고 질문도 아닌 턴이고, 프론티어 모델과 블라인드로 붙는 자리에서 그대로 진다.
    # 라우터가 같은 호출에서 조건부 안내(provisional)를 같이 만들어 두므로 FM 호출은 늘지 않는다.
    if route.context == "missing_critical" and route.ask_back:
        if route.provisional:
            return f"{route.provisional}\n\n{route.ask_back}"
        return route.ask_back

    # 응급이면 검색을 짧게 끊는다. 실측에서 예산 6회를 다 쓰고 31초가 걸렸는데,
    # 정작 근거는 "약물 부작용 자료에서 확인되지 않음"이라 답에 보탬이 없었다.
    # 응급에서 값을 내는 건 근거 인용이 아니라 즉시 의뢰다.
    # 실측: "가이드라인상 혈압 목표" 질문에서 모델이 corpus_tag="hira"(급여기준)를
    # 골라 5회를 다 쓰고 관련 없는 페이지 하나만 열었다. 어느 코퍼스인지는 도메인이
    # 이미 알고 있으므로 모델 재량에 맡길 이유가 없다.
    hint = CORPUS_HINT.get(route.domain)
    if hint and route.search_query:
        route.search_query = (
            f'{route.search_query}\n(Use corpus_tag="{hint}" for index tools '
            f'— this question belongs to the {hint} corpus.)'
        )

    if route.urgency == "emergency":
        budget = EMERGENCY_BUDGET
    elif route.domain in DEEP_DOMAINS:
        budget = DEEP_BUDGET
    else:
        budget = RETRIEVAL_BUDGET

    # 라우터에서 이미 시간을 많이 썼으면 검색을 통째로 건너뛴다. 근거 있는 답보다
    # 답이 있는 것이 먼저다 — 빈 응답은 채점에서 0점이고, 실측으로 그걸 봤다.
    if dl.expired(reserve=ANSWER_RESERVE_S):
        log.warning("시간이 모자라 검색을 건너뛴다 (남은 %.0fs)", dl.remaining())
        route.tools = []

    content, result = await generate(
        messages, route, call_fm, MCP, MAX_TOKENS, budget, dl, ANSWER_RESERVE_S
    )

    # 검색이 왜 빈손인지는 호출 이력을 봐야 안다. status/items 만으로는
    # "못 찾은 것"과 "찾았는데 본문을 안 연 것"이 구분되지 않는다.
    for step in (getattr(result, "trace", None) or []):
        log.info("  retrieval· %s", step)

    # content 가 비는 건 대개 reasoning 이 예산을 다 먹고 잘린 경우다.
    # max_tokens 를 더 올릴 수는 없으므로(2048 이 상한), 도구 없이 한 번 더 시도한다.
    if not content:
        log.warning("빈 content — 도구 없이 재시도 (elapsed %.0fs)", dl.elapsed)
        data = await call_fm(messages, MAX_TOKENS)
        content = (data["choices"][0]["message"].get("content") or "").strip()

    if not content:
        log.error("빈 content 로 응답한다 — elapsed=%.1fs", dl.elapsed)

    # 응급인데 응급 안내로 시작하지 않으면 그때만 앞에 붙인다.
    if content and route.urgency == "emergency" and not _EMERGENCY_LEAD.search(content[:200]):
        log.info("응급 판정인데 답변 앞에 안내가 없다 — 안전망 문구 삽입")
        content = _EMERGENCY_LEAD_IN + "\n\n" + content

    if content:
        cleaned = strip_role_leak(content)
        if cleaned != content:
            log.info("역할 라벨 누수 제거")
            content = cleaned

    # 근거 개수를 넘는 인용번호는 지어낸 것이다.
    if content and _CITE_MARK.search(content):
        n_items = len(result.items) if result else 0
        fixed = strip_bad_citations(content, n_items)
        if fixed != content:
            log.info("근거 %d건을 넘는 인용번호를 제거했다", n_items)
            content = fixed

    # 살아남은 인용번호에 대해서만 출처를 붙인다.
    if content and result and result.items:
        with_src = render_sources(content, result.items)
        if with_src != content:
            log.info("출처 표기 삽입")
            content = with_src
    return content


@app.post("/v1/chat/completions")
async def chat_completions(body: dict) -> dict[str, Any]:
    messages = trim_history(body.get("messages") or [])
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
                f"{m.get('role', '')}: {m.get('content', '')}"
                for m in messages if isinstance(m, dict)
            )
            content = _drug_guard(convo, content)
    except Exception:
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
