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
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from langdetect import DetectorFactory, LangDetectException, detect

from budget import Deadline
from generation import generate
from mcp_client import MCPClient
from router import classify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("driver")

# langdetect는 짧은 문장에서 샘플링을 사용한다. 평가 재현성을 위해 고정한다.
DetectorFactory.seed = 0

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


_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_ENGLISH_HINT_RE = re.compile(
    r"\b(?:i|my|we|our|what|when|where|why|how|can|could|should|would|do|does|did|"
    r"is|are|was|were|have|has|had|with|without|take|taking|continue|stop|male|"
    r"female|patient|pain|fever|medicine|medication|doctor|versus|vs)\b",
    re.IGNORECASE,
)


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
    return ""


def _language(text: str) -> str:
    """`ko`, `en`, 또는 번역이 필요한 다른 ISO 언어 코드를 돌려준다."""
    if _HANGUL_RE.search(text):
        return "ko"
    if not text:
        return "en"
    # langdetect는 "45yo male eGFR 38, continue ACEi?" 같은 짧은 임상 영어를
    # 스페인어로 오판한다. 흔한 영어 문법·임상 토큰이 있으면 먼저 보호한다.
    if _ENGLISH_HINT_RE.search(text):
        return "en"
    try:
        return detect(text)
    except LangDetectException:
        # 짧은 약명·수치만 있는 후속 턴은 앞선 대화가 이미 맥락을 제공한다.
        return "en"


def _clean_content(content: str) -> tuple[str, bool]:
    """노출된 사고 토큰을 제거하고, 제거 여부도 반환한다."""
    original = content
    if "</think>" in content.lower():
        # 정상적인 `<think>reasoning</think>answer` 형태면 답변 부분만 보존한다.
        content = re.split(r"</think>", content, flags=re.IGNORECASE)[-1]
    content = _THINK_BLOCK_RE.sub("", content)
    content = re.sub(r"</?think>", "", content, flags=re.IGNORECASE).strip()
    return content, content != original.strip()


async def _translate_latest_to_english(messages: list[dict], text: str) -> list[dict]:
    """비영어 최신 사용자 턴만 영어로 정규화한다. 외부 API를 사용하지 않는다."""
    prompt = [
        {
            "role": "system",
            "content": (
                "Translate the user's medical question into natural, precise English. "
                "Preserve drug names, measurements, symptoms, negations, and uncertainty. "
                "Return only the translation, with no explanation."
            ),
        },
        {
            "role": "user",
            "content": (
                "Translate the medical question between <source> tags into English. "
                "Output only the English translation.\n\n"
                f"<source>{text}</source>"
            ),
        },
    ]
    data = await call_fm(prompt, 700)
    translated, _ = _clean_content(
        (data["choices"][0]["message"].get("content") or "").strip()
    )
    if not translated or _language(translated) != "en":
        # 잘못된 현지어 답변을 다음 생성으로 넘기면 언어 붕괴가 연쇄된다.
        # 원문과 영어 명령을 함께 둔 단일 턴으로 한 번 더 강하게 교정한다.
        log.warning("번역 결과가 영어가 아니어서 재시도한다")
        retry_prompt = [{
            "role": "user",
            "content": (
                "Write ONLY an English translation of this text. Do not answer it and "
                f"do not use its language: <source>{text}</source>"
            ),
        }]
        retry = await call_fm(retry_prompt, 700)
        translated, _ = _clean_content(
            (retry["choices"][0]["message"].get("content") or "").strip()
        )
    if not translated:
        log.warning("번역 결과가 비어 원문을 유지한다")
        translated = text

    normalized = [dict(message) for message in messages]
    for index in range(len(normalized) - 1, -1, -1):
        if normalized[index].get("role") == "user":
            normalized[index]["content"] = translated
            break
    normalized.insert(0, {
        "role": "system",
        "content": (
            "Answer the latest medical question in clear English. Use the conversation "
            "context, but do not answer in the source language."
        ),
    })
    return normalized


def _suspicious(content: str, expected: str, elapsed: float, leaked_think: bool) -> bool:
    if not content or leaked_think or len(content) > 3500:
        return True
    # 느리면서 긴 응답은 실측상 반복문·코드 조각으로 붕괴한 경우가 많았다.
    if elapsed > 30 and len(content) > 2500:
        return True
    actual = _language(content)
    if expected == "ko":
        return not bool(_HANGUL_RE.search(content))
    return actual not in {"en"}


def _retry_messages(messages: list[dict], expected: str) -> list[dict]:
    language = "Korean" if expected == "ko" else "English"
    return [{
        "role": "system",
        "content": (
            f"Answer in clear {language}. Give a direct, medically careful answer under "
            "700 words. Do not reveal reasoning or use <think> tags. Do not emit code, "
            "repetitive punctuation, or text in another language."
        ),
    }, *messages]


async def generate_reply(messages: list[dict], dl: Deadline) -> str:
    """영어·한국어는 직결하고, 그 외 언어는 영어로 정규화해 답한다."""
    source_lang = _language(_last_user_text(messages))
    expected = source_lang if source_lang in {"ko", "en"} else "en"
    prepared = messages
    if source_lang not in {"ko", "en"}:
        log.info("비지원 출력 언어 %s — 영어로 정규화", source_lang)
        prepared = await _translate_latest_to_english(messages, _last_user_text(messages))

    started = time.monotonic()
    data = await call_fm(prepared, MAX_TOKENS)
    raw = (data["choices"][0]["message"].get("content") or "").strip()
    content, leaked_think = _clean_content(raw)
    elapsed = time.monotonic() - started

    if _suspicious(content, expected, elapsed, leaked_think):
        log.warning(
            "출력 가드 재시도 — source=%s expected=%s chars=%d elapsed=%.1fs think=%s",
            source_lang, expected, len(content), elapsed, leaked_think,
        )
        retry = await call_fm(_retry_messages(prepared, expected), 1400)
        retried = (retry["choices"][0]["message"].get("content") or "").strip()
        content, _ = _clean_content(retried)

    # 재시도도 비정상 장문이면 완전한 문장 경계에서 잘라 무한 출력이 채점기로
    # 넘어가는 것을 막는다. 정상 응답은 이 경로에 들어오지 않는다.
    if len(content) > 3500:
        cut = max(content.rfind(". ", 0, 3500), content.rfind("。", 0, 3500))
        content = content[: cut + 1 if cut > 1800 else 3500].rstrip()

    if not content:
        log.error("L2 응답의 content가 비었다 — elapsed=%.1fs", dl.elapsed)
    return content


@app.post("/v1/chat/completions")
async def chat_completions(body: dict) -> dict[str, Any]:
    messages = body.get("messages") or []
    dl = Deadline.start(REQUEST_BUDGET_S)
    try:
        # 번역 경로는 FM 호출이 하나 더 필요하므로 별도 여유를 준다. 영어·한국어
        # 대다수 문항의 제한은 기존과 동일하게 유지한다.
        source_lang = _language(_last_user_text(messages))
        request_timeout = REQUEST_BUDGET_S + (75 if source_lang not in {"ko", "en"} else 20)
        content = await asyncio.wait_for(
            generate_reply(messages, dl), timeout=request_timeout
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
