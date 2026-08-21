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
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from budget import Deadline
from generation import generate
from mcp_client import MCPClient
from router import DOMAIN_TOOLS, Route, rule_date_sensitive

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
PUBLIC_MODEL = os.environ.get("HARNESS_MODEL_NAME", "medai")

# 서버가 max_tokens 2048 을 넘기면 400 (`output_limit_exceeded`) 을 던진다. 이건 상한이다.
SERVER_MAX_TOKENS = 2048
MAX_TOKENS = min(int(os.environ.get("FM_MAX_TOKENS", "2048")), SERVER_MAX_TOKENS)
DIRECT_MAX_TOKENS = min(int(os.environ.get("FM_DIRECT_MAX_TOKENS", "1100")), SERVER_MAX_TOKENS)
MCP_FINAL_MAX_TOKENS = min(int(os.environ.get("FM_MCP_FINAL_MAX_TOKENS", "900")), SERVER_MAX_TOKENS)
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

# L2 follows task instructions in the latest user turn more reliably than a
# separate system message. Keep this deliberately narrow: it prevents a generic
# referral from replacing an otherwise answerable medical response.
ANSWER_INSTRUCTION = (
    "Answer directly and concisely. Do not give a referral-only answer."
)

_EVIDENCE_RE = re.compile(
    r"(근거|출처|인용|논문|연구|가이드라인|지침|공식|문헌|reference|citation|"
    r"evidence|guideline|study|paper|trial|peer[- ]reviewed|systematic\s+review|"
    r"meta[- ]analysis|official\s+(?:source|label|document|guidance))",
    re.I,
)
_CLAIM_CHECK_RE = re.compile(
    r"(진짜|사실|맞아|검증|입증|효과\s*있|몇\s*%|퍼센트|확률|위험도|증가한다던데|"
    r"is\s+it\s+true|is\s+there\s+(?:research|evidence)|proven|what\s+percent|"
    r"does\s+.+\s+(?:increase|decrease|raise|lower)\s+(?:the\s+)?(?:risk|odds|rate))",
    re.I,
)
_POLICY_RE = re.compile(
    r"(급여|비급여|보험|심평원|HIRA|고시|수가|청구|산정|약가|상한금액|본인부담|"
    r"\breimbursement\b|\bcoverage\b|\bbilling\b|\bbillable\b|\bfee\b|\bprice\b|"
    r"\bcovered\b|not\s+covered|\bcopay\b|out[- ]of[- ]pocket)",
    re.I,
)
_LAW_RE = re.compile(
    r"(법령|법률|조문|시행령|시행규칙|고시\s*제|law|statute|regulation|legal\s+duty|article)",
    re.I,
)
_KCD_RE = re.compile(
    r"(KCD|ICD|질병코드|상병코드|진단코드|disease\s*code|diagnosis\s*code|"
    r"diagnostic\s*code|classification\s*code)",
    re.I,
)
_MED_DETAIL_RE = re.compile(
    r"(허가|적응증|금기|병용|상호작용|이상반응|부작용|용량|투여|임신\s*중|수유\s*중|"
    r"label|indication|contraindicat(?:ion|ed)|interaction|adverse|side\s*effect|dose|dosage|"
    r"pregnancy|lactation|breastfeeding|take\s+.+\s+with|safe\s+with|combine|combined\s+with|"
    r"use\s+.+\s+together)",
    re.I,
)
_MED_CAUSALITY_RE = re.compile(
    r"(부작용|이상반응|때문|탓|원인|caus|adverse|side\s*effect)", re.I
)
_SPECIFIC_DRUG_RE = re.compile(
    r"(아세트아미노펜|타이레놀|이부프로펜|부루펜|아스피린|와파린|메트포르민|"
    r"암로디핀|로사르탄|리시노프릴|세툭시맙|오메프라졸|아토르바스타틴|"
    r"acetaminophen|paracetamol|ibuprofen|aspirin|warfarin|metformin|amlodipine|"
    r"losartan|lisinopril|cetuximab|omeprazole|atorvastatin|"
    r"[가-힣]{2,}(?:맙|닙|틴|신|핀|탄|롤|졸|딘|펜|센|민|린|론|손|탁|실|스타틴))",
    re.I,
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
        "driver up — model=%s max_tokens=%d direct_tokens=%d mcp_final_tokens=%d thinking=%s budget=%.0fs fm_conc=%d mcp_conc=%d",
        FM_MODEL,
        MAX_TOKENS,
        DIRECT_MAX_TOKENS,
        MCP_FINAL_MAX_TOKENS,
        ENABLE_THINKING,
        REQUEST_BUDGET_S,
        FM_CONCURRENCY,
        MCP_CONCURRENCY,
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
    models = [
        {"id": PUBLIC_MODEL, "object": "model", "created": 0, "owned_by": "team"},
    ]
    if FM_MODEL != PUBLIC_MODEL:
        models.append({"id": FM_MODEL, "object": "model", "created": 0, "owned_by": "lunit"})
    return {"object": "list", "data": models}


# 일시적인 것들. 실측으로 502(nginx)를 봤다 — 재시도 없이 두면 그 문항이 통째로 0점이다.
RETRY_STATUS = {429, 500, 502, 503, 504}


def _messages_with_answer_instruction(messages: list[dict]) -> list[dict]:
    """마지막 user 턴에만 좁은 답변 지시를 붙인다."""
    forwarded = [dict(message) for message in messages]
    for message in reversed(forwarded):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = f"{content}\n\n[{ANSWER_INSTRUCTION}]"
        elif isinstance(content, list):
            message["content"] = [
                *content,
                {"type": "text", "text": f"[{ANSWER_INSTRUCTION}]"},
            ]
        break
    return forwarded


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_text(message.get("content"))
    return ""


def _recent_user_text(messages: list[dict], turns: int = 3) -> str:
    user_turns = [
        _content_text(message.get("content"))
        for message in messages
        if message.get("role") == "user"
    ]
    return "\n".join(text for text in user_turns[-turns:] if text)


def _language_for(text: str) -> str:
    return "ko" if re.search(r"[가-힣]", text) else "en"


def _mcp_route(messages: list[dict]) -> Route | None:
    """확실한 외부 근거 질문만 MCP로 보낸다. 애매하면 L2 direct가 기본이다."""
    text = _recent_user_text(messages)
    if not text.strip():
        return None

    domain = ""
    if _KCD_RE.search(text):
        domain = "kcd"
    elif _LAW_RE.search(text):
        domain = "korean_law"
    elif _POLICY_RE.search(text):
        domain = "hira_drug_price" if re.search(r"(약가|상한금액|price)", text, re.I) else "hira_updates"
    elif _MED_DETAIL_RE.search(text) and _SPECIFIC_DRUG_RE.search(text):
        domain = "adr" if _MED_CAUSALITY_RE.search(text) else "mfds"
    elif re.search(r"(가이드라인|지침|권고|guideline)", text, re.I):
        domain = "guideline_index"
    elif _CLAIM_CHECK_RE.search(text) or _EVIDENCE_RE.search(text):
        domain = "pubmed"

    if not domain:
        return None

    tools = list(DOMAIN_TOOLS.get(domain) or [])
    if not tools:
        return None
    date_sensitive = rule_date_sensitive(text)
    if date_sensitive and domain not in ("korean_law", "guideline_index") and "hira_updates_search" not in tools:
        tools.append("hira_updates_search")
    return Route(
        domain=domain,
        urgency="routine",
        context="sufficient",
        persona="layperson",
        lang=_language_for(_last_user_text(messages) or text),
        ask_back="",
        date_sensitive=date_sensitive,
        search_query=text,
        tools=tools,
        source="rules",
    )


def _choice(data: dict[str, Any]) -> dict[str, Any]:
    return data["choices"][0]


def _choice_content(data: dict[str, Any]) -> str:
    return (_choice(data)["message"].get("content") or "").strip()


def _last_resort_answer(messages: list[dict]) -> str:
    last_user = _last_user_text(messages)
    if any("가" <= char <= "힣" for char in last_user):
        return (
            "현재 답변 생성이 원활하지 않아 질문에 맞춘 충분한 답을 드리지 못했습니다. "
            "증상이 심하거나 빠르게 악화하거나, 호흡곤란·의식저하·심한 흉통·마비·대량 출혈 같은 "
            "응급 신호가 있으면 119 또는 응급실 도움을 받으세요."
        )
    return (
        "I could not generate a reliable answer for this request. If symptoms are severe, rapidly "
        "worsening, or include trouble breathing, confusion, severe chest pain, weakness on one side, "
        "or heavy bleeding, seek emergency care now."
    )


async def answer_with_optional_mcp(messages: list[dict], dl: Deadline) -> str:
    route = _mcp_route(messages)
    if route is None:
        return await generate_reply(messages, dl)

    if dl.expired(reserve=ANSWER_RESERVE_S):
        log.info("MCP gate hit but time budget is too low — using direct L2")
        return await generate_reply(messages, dl)

    try:
        log.info("MCP gate hit — domain=%s tools=%s", route.domain, ",".join(route.tools))
        content, result = await generate(
            _messages_with_answer_instruction(messages),
            route,
            call_fm,
            MCP,
            MCP_FINAL_MAX_TOKENS,
            budget=RETRIEVAL_BUDGET,
            deadline=dl,
            reserve=ANSWER_RESERVE_S,
        )
        if content:
            return content
        log.warning(
            "MCP path returned empty content — using direct L2 (domain=%s status=%s)",
            route.domain,
            getattr(result, "status", None),
        )
    except Exception as e:  # noqa: BLE001 — MCP 경로 실패가 최종 빈 답변이 되면 안 된다.
        log.warning("MCP path failed — using direct L2 (%s: %s)", type(e).__name__, str(e)[:200])

    return await generate_reply(messages, dl)


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


async def generate_reply(messages: list[dict], dl: Deadline) -> str:
    """Use a completed thinking response, otherwise fall back to the 43-point path."""
    forwarded = _messages_with_answer_instruction(messages)
    try:
        data = await call_fm(
            forwarded,
            DIRECT_MAX_TOKENS,
            {"chat_template_kwargs": {"enable_thinking": True}},
        )
    except Exception as e:  # noqa: BLE001 - 실패한 thinking 호출보다 완성 답변이 중요하다.
        log.warning(
            "thinking 호출 실패 — 기존 경로로 폴백 (%s elapsed=%.1fs)",
            type(e).__name__,
            dl.elapsed,
        )
        data = await call_fm(
            forwarded,
            DIRECT_MAX_TOKENS,
            {"chat_template_kwargs": {"enable_thinking": False}},
        )
        content = _choice_content(data)
        if not content:
            log.error("L2 raw 응답의 content가 비었다 — elapsed=%.1fs", dl.elapsed)
        return content

    choice = _choice(data)
    content = _choice_content(data)
    if choice.get("finish_reason") == "length" or not content:
        log.info(
            "thinking 응답 손상 — 기존 경로로 폴백 (finish=%s content=%d elapsed=%.1fs)",
            choice.get("finish_reason"),
            len(content),
            dl.elapsed,
        )
        data = await call_fm(
            forwarded,
            DIRECT_MAX_TOKENS,
            {"chat_template_kwargs": {"enable_thinking": False}},
        )
        content = _choice_content(data)
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
            answer_with_optional_mcp(messages, dl), timeout=REQUEST_BUDGET_S + 20
        )
    except asyncio.TimeoutError:
        # 여기서 빈 문자열을 흘리면 그 문항은 0점이다. 도구도 라우팅도 없이
        # 한 번만 더, 짧게 답을 받아 본다. 늦은 답이 없는 답보다 낫다.
        log.error("요청 시간 초과 — 직답으로 되살린다 (elapsed=%.1fs)", dl.elapsed)
        try:
            data = await asyncio.wait_for(
                call_fm(
                    _messages_with_answer_instruction(messages),
                    DIRECT_MAX_TOKENS,
                    {"chat_template_kwargs": {"enable_thinking": False}},
                ),
                timeout=60,
            )
            content = _choice_content(data)
        except Exception:
            log.exception("직답 폴백도 실패")
            content = _last_resort_answer(messages)
    except Exception:
        # 평가 하네스에 5xx 를 돌려주면 대화 전체가 깨질 수 있다.
        # 그래서 형식은 지키되, 실패는 로그에 남겨 사후에 반드시 보이게 한다.
        log.exception("생성 실패")
        content = _last_resort_answer(messages)

    if not content:
        content = _last_resort_answer(messages)

    log.info("응답 %d자 / %.1fs", len(content), dl.elapsed)
    prompt_chars = sum(len(_content_text(message.get("content"))) for message in messages)
    completion_chars = len(content)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model") or PUBLIC_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": max(1, int(prompt_chars * 0.7)),
            "completion_tokens": max(1, int(completion_chars * 0.7)),
            "total_tokens": max(2, int((prompt_chars + completion_chars) * 0.7)),
        },
    }
