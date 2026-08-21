"""입구 라우터 — 질문 하나를 받아 어떻게 처리할지 정한다.

Patient Simulator 실측 30건(`case_id` 접두사가 곧 출처 데이터소스)을 보면 질문이
MCP 21개 도구 전 영역에 고르게 퍼져 있다. 그래서 "무엇으로 검색할지"를 먼저 정해야
하고, 그 판정이 이 파일이다.

판정은 넷이다.
  domain   — 어느 MCP 도구군으로 갈지 (검증 가능: case_id 접두사가 정답 라벨)
  urgency  — 응급 / 조건부 / 일상. 응급이면 되묻지 않고 즉시 의뢰한다
  context  — 결정적 맥락이 빠졌는가. 빠졌으면 하나만 되묻는다
  persona  — 일반인 / 실무자 / 임상의. 답변 눈높이가 갈린다

응급만 규칙을 먼저 태운다. 놓치면 위험한 쪽이라 recall 을 우선하고,
LLM 이 아니라고 해도 규칙이 잡으면 응급으로 올린다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any

log = logging.getLogger("router")

# ── 도메인 → MCP 도구 서브셋 ──────────────────────────────────────────
# 21개를 통째로 주면 모델이 헤맨다. 질문 유형별로 줄여서 준다.
# 인접 도메인끼리 도구를 일부러 겹쳐 둔다. 실측에서 오분류가 남는 자리가
#   약물 3형제(mfds ↔ adr ↔ hira_drug_price)와 청구 3형제(hira_updates ↔ kcd ↔ hira_drug_price)
# 인데, 이 둘은 사람이 봐도 경계가 흐리다(gold 라벨 자체가 갈리는 사례가 있다).
# 프롬프트를 더 조여 100%를 노리는 것보다, 틀려도 필요한 도구가 손에 있게 하는 편이 견고하다.
DOMAIN_TOOLS: dict[str, list[str]] = {
    "pubmed": ["rag_vector_query", "rag_get_data_source_detail"],
    "mfds": [
        "openapi_mfds_get_drug_indication",
        "openapi_mfds_check_drug_permission",
        "openapi_mfds_find_drugs_by_ingredient",
        "adr_retrieve_drug_info",  # 상호작용·경고를 같이 봐야 하는 질문이 섞인다
    ],
    "adr": [
        "adr_retrieve_drug_info",
        "openapi_mfds_get_drug_indication",  # 한국어 약명 → ingredient_eng 변환에 필요
        "rag_sql_query",  # FAERS
    ],
    "hira_drug_price": [
        "openapi_hira_get_drug_price",
        "openapi_mfds_check_drug_permission",
        "hira_updates_search",  # 급여목록 등재와 급여 인정기준은 다른 곳에 있다
    ],
    "hira_updates": [
        "hira_updates_search",
        "index_get_relevant_nodes",
        "index_get_page_content",
        "openapi_hira_disease_check_code",
        "openapi_hira_get_drug_price",
    ],
    "korean_law": ["openapi_law_search", "openapi_law_list_articles", "openapi_law_get_article"],
    "guideline_index": [
        "index_get_relevant_nodes",
        "index_get_page_content",
        "index_keyword_search",
    ],
    "kcd": [
        "kcd_search_codes",
        "kcd_get_name",
        "openapi_hira_disease_check_code",
        "hira_updates_search",  # "이 코드로 청구되나" 는 고시까지 봐야 답이 된다
    ],
    # generic 은 L2 자체 지식으로 답한다. 검색이 오히려 방해가 된다.
    "generic": [],
}
DOMAINS = list(DOMAIN_TOOLS)

# ── 응급 규칙 (recall 우선) ────────────────────────────────────────────
# 단독으로 응급인 것.
EMERGENCY_SOLO = [
    r"목이?\s*뻣뻣", r"경부\s*강직", r"의식[이을]?\s*(잃|없|흐)", r"쓰러졌",
    r"숨[이을]?\s*(안|못)\s*[쉬쉼]", r"호흡\s*곤란", r"말[이]?\s*어눌", r"한쪽[이]?\s*마비",
    r"피가\s*(계속|멈추지)", r"출혈[이]?\s*(계속|멎지|멈추지)", r"경련", r"발작",
    r"자살", r"극단적\s*선택", r"청색증", r"실신",
]
# 다른 신호와 겹칠 때 응급인 것.
EMERGENCY_COMBO = [
    (r"가슴[이]?\s*(아프|답답|조여)|흉통", r"식은땀|숨[이]?\s*[차찬]|턱|왼팔|어깨"),
    (r"머리가?\s*(너무|심하게|갑자기)?\s*아프|두통", r"열|목이?\s*뻣뻣|구토|시야"),
    (r"열[이]?\s*(나|심)", r"목이?\s*뻣뻣|의식|경련"),
    (r"복통|배가?\s*아프", r"토혈|검은\s*변|혈변"),
]


# 날짜가 박힌 질문은 현행 규정과 대조해야 한다 (openapi_law_get_article 은 현행만 준다).
# LLM 판정이 놓치는 걸 봐서 규칙으로 같이 잡는다.
DATE_PATTERNS = [
    r"\d{4}\s*년\s*\d{1,2}\s*월",
    r"\d{4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}",
    r"진료일[자은는]?\s*\d",
    r"오늘도\s*(그대로|여전히)",
    r"지금[도은는]?\s*(그대로|여전히|어떻게)",
    r"현재[도은는]?\s*(유효|적용|그대로)",
    r"최근\s*(고시|개정|변경)",
    r"작년|재작년|올해\s*\d{1,2}\s*월",
]


def rule_date_sensitive(text: str) -> bool:
    return any(re.search(p, text) for p in DATE_PATTERNS)


def rule_urgency(text: str) -> str | None:
    """규칙이 응급이라고 판단하면 'emergency', 아니면 None(=LLM 에 맡긴다)."""
    for pat in EMERGENCY_SOLO:
        if re.search(pat, text):
            return "emergency"
    for a, b in EMERGENCY_COMBO:
        if re.search(a, text) and re.search(b, text):
            return "emergency"
    return None


# vLLM 의 structured output. 이걸 걸면 모델이 스키마 밖으로 못 나간다.
# 실측: 안 걸면 "domain":"pubmed_abstract" 같은 스키마 밖 값이나 엉뚱한 키가 섞여 나오고
# 응답이 길어져 잘리기까지 한다. `guided_json` 은 이 서버에서 무시된다 — json_schema 를 쓸 것.
ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "enum": DOMAINS},
        "urgency": {"type": "string", "enum": ["emergency", "conditional", "routine"]},
        "context": {"type": "string", "enum": ["sufficient", "missing_critical"]},
        "ask_back": {"type": "string"},
        "provisional": {"type": "string"},
        "persona": {"type": "string", "enum": ["layperson", "practitioner", "clinician"]},
        "lang": {"type": "string", "enum": ["ko", "en"]},
        "date_sensitive": {"type": "boolean"},
        "search_query": {"type": "string"},
    },
    "required": [
        "domain", "urgency", "context", "ask_back", "provisional",
        "persona", "lang", "date_sensitive", "search_query",
    ],
    "additionalProperties": False,
}

ROUTE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "route", "schema": ROUTE_SCHEMA},
}


@dataclass
class Route:
    domain: str = "generic"
    urgency: str = "routine"  # emergency | conditional | routine
    context: str = "sufficient"  # sufficient | missing_critical
    persona: str = "layperson"  # layperson | practitioner | clinician
    lang: str = "ko"
    ask_back: str = ""  # context=missing_critical 일 때 되물을 질문 하나
    # 되묻기 전에 먼저 주는 조건부 안내. 되묻는 턴이 빈 턴이 되지 않게 한다.
    # 실측: 되묻기 경로에서 76자짜리 "상황에 따라 다릅니다"만 나간 턴이 있었다.
    # 프론티어 모델과 블라인드로 비교당하는 자리에서 이런 턴은 그대로 진다.
    provisional: str = ""
    date_sensitive: bool = False  # 날짜가 박힌 질문이면 effective_date 대조가 필요하다
    # 멀티턴 지시대명사를 푼 self-contained 검색 질의. 라우터가 여기서 만들어 두면
    # generation 단계에서 "모델에게 도구를 줘서 질의를 받아오는" 왕복 한 번이 통째로 없어진다.
    search_query: str = ""
    tools: list[str] = field(default_factory=list)
    source: str = "llm"  # llm | rules | fallback

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


CLASSIFY_PROMPT = """You classify a Korean health question so a downstream system can route it.

Return ONLY a JSON object, no prose, with these keys:

"domain": which knowledge source answers it.
  Work through the eight specific sources FIRST. Pick "generic" ONLY if none of them fit —
  "generic" is the last resort, not the default.

  - "pubmed": the person is checking whether a CLAIM is supported by research. Cues: heard it on
    YouTube/news/from someone, "진짜야?", "연구된 거야?", "겁주는 거야?", asks about a risk
    association or a screening/predictive marker between two conditions. Even when the topic is
    ordinary medicine, if the question is "is this claim real / is there a study", it is "pubmed".
  - "mfds": may I TAKE this drug — leftover/borrowed medicine, splitting a tablet, is this product
    approved, what is it approved for, dosage. The person is deciding about taking it.
  - "adr": the person is ALREADY EXPERIENCING symptoms and asks whether a drug caused them.
    Cues: symptom started after taking/changing a drug, stopped when off it, returned when retaken.
  - "hira_drug_price": money for a drug — 약값, 약국에서 얼마, 급여/비급여 여부, 상한금액.
  - "hira_updates": billing and reimbursement RULES for procedures/services — 청구 가능한지,
    산정 방법, 수가, 급여기준, 고시 변경. Typically asked by staff about a claim.
  - "korean_law": wants the ARTICLE NUMBER or verbatim text of a statute/regulation, or whether a
    legal reporting duty exists.
  - "guideline_index": clinical practice guideline recommendation for managing a patient —
    evidence level, grade of recommendation, what the guideline advises.
  - "kcd": disease classification codes — code lookup, code meaning, whether a code can be billed.
  - "generic": none of the above; answerable from general medical knowledge alone.

  Disambiguation:
  - "can I take this drug" → mfds. "is this symptom from the drug" → adr. "how much does it cost /
    is it covered" → hira_drug_price.
  - A claim-checking question is "pubmed" even if it sounds like general medicine.
  - A question about billing a PROCEDURE is "hira_updates"; about a CODE itself, "kcd".

"urgency": "emergency" (needs care now), "conditional" (urgent only if certain red flags are
  present), or "routine".

"context": "missing_critical" if a decisive fact is absent AND one question would materially change
  the answer — for example a drug/dose decision that depends on the person's age, other conditions,
  other medications, or why they want it. Use "sufficient" when a conditional answer covering the
  main branches is genuinely just as good, and always when it is an emergency.

"ask_back": if context is "missing_critical", the single highest-value question to ask, in
  Korean, one sentence. It MUST be an actual question ending in "?" — not a statement about what
  depends on what, and not a list of things to check. Otherwise "".

"provisional": if context is "missing_critical", a short conditional answer to give BEFORE asking,
  in Korean, 2-4 sentences. Cover the main branches ("A라면 …, B라면 …") and name the safest
  default with concrete specifics (drug/dose/step) where it is safe to do so. Never leave the
  person with nothing — a turn that only asks a question and gives no usable guidance is a failed
  turn. Otherwise "".

"persona": "layperson" (casual speech, slang, typos, personal worry), "practitioner" (billing,
  claims, pharmacy or administrative work), or "clinician" (uses clinical terminology, asks about
  guidelines/evidence grade for managing a patient).

"lang": "ko" or "en" — the language to answer in.

"date_sensitive": true if the question is pinned to a specific date or asks whether a rule is
  still current.

"search_query": a single self-contained query to look the answer up with. Resolve every pronoun
  and ellipsis from the conversation ("그 약" → the actual drug name). Be specific and descriptive
  rather than a few keywords. For the guideline/HIRA corpora an English query works well even
  when the user wrote Korean; for Korean law and Korean drug/billing lookups keep Korean terms.
  Empty string only when domain is "generic".

Conversation (most recent user turn last):
{question}"""


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def transcript(messages: list[dict], turns: int = 5, per_turn: int = 700) -> str:
    """최근 대화를 라우터에 보여줄 형태로. 지시대명사를 풀려면 앞 턴이 필요하다."""
    tail = [m for m in messages if m.get("role") in ("user", "assistant")][-turns:]
    lines = []
    for m in tail:
        who = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {str(m.get('content') or '')[:per_turn]}")
    return "\n".join(lines)


def build_route(raw: dict[str, Any] | None, question: str, source: str) -> Route:
    raw = raw or {}
    r = Route(source=source)

    d = str(raw.get("domain") or "").strip()
    r.domain = d if d in DOMAIN_TOOLS else "generic"

    u = str(raw.get("urgency") or "").strip()
    r.urgency = u if u in ("emergency", "conditional", "routine") else "routine"

    c = str(raw.get("context") or "").strip()
    r.context = c if c in ("sufficient", "missing_critical") else "sufficient"

    p = str(raw.get("persona") or "").strip()
    r.persona = p if p in ("layperson", "practitioner", "clinician") else "layperson"

    r.lang = "en" if str(raw.get("lang") or "").strip() == "en" else "ko"
    r.ask_back = str(raw.get("ask_back") or "").strip()
    r.provisional = str(raw.get("provisional") or "").strip()
    # 라우터가 질의를 못 만들었으면 마지막 사용자 발화를 그대로 쓴다. 지시대명사가
    # 남아 있을 수 있지만, 검색을 통째로 건너뛰는 것보다는 낫다.
    r.search_query = str(raw.get("search_query") or "").strip() or question
    # 규칙이 잡으면 올린다. LLM 이 놓치는 쪽이 실측에서 확인됐다.
    r.date_sensitive = bool(raw.get("date_sensitive")) or rule_date_sensitive(question)

    # 규칙이 응급이라고 하면 올린다. 내리지는 않는다 — 놓치는 쪽이 훨씬 비싸다.
    if rule_urgency(question) == "emergency":
        r.urgency = "emergency"

    # 응급이면 되묻지 않는다. 09 문서 §4 의 "응급:후속질문자제" 기준이 여기 걸린다.
    if r.urgency == "emergency":
        r.context = "sufficient"
        r.ask_back = ""
        r.provisional = ""

    r.tools = list(DOMAIN_TOOLS[r.domain])

    # 날짜가 박힌 질문은 "지금도 그런가"를 묻는 것이다. 개정 이력을 볼 수 있어야
    # effective_date 와 대조해 "그때 적용된 규칙이 아니다"를 말할 수 있다.
    if r.date_sensitive and "hira_updates_search" not in r.tools:
        if r.domain in ("korean_law", "guideline_index", "generic"):
            pass  # 법령은 자체 도구가, guideline 은 개정판 자체가 따로 있다
        else:
            r.tools.append("hira_updates_search")

    return r


async def classify(messages: list[dict], call_fm) -> Route:
    """call_fm(messages, max_tokens, extra) -> OpenAI 응답 dict 를 주입받아 분류한다.

    주입식으로 둔 건 app.py 의 FM 호출과 중복 구현을 피하고, 테스트에서
    가짜 호출기로 갈아끼우기 위해서다.
    """
    question = last_user_text(messages)
    if not question:
        return Route(source="fallback", tools=[])

    try:
        data = await call_fm(
            [{"role": "user", "content": CLASSIFY_PROMPT.format(question=transcript(messages))}],
            800,
            {"response_format": ROUTE_RESPONSE_FORMAT},
        )
        content = data["choices"][0]["message"].get("content") or ""
        parsed = _extract_json(content)
        if parsed is None:
            log.warning("분류 JSON 파싱 실패: %r", content[:300])
    except Exception as e:  # noqa: BLE001 — 분류 실패로 전체가 죽으면 안 된다
        # 조용히 삼키면 라우터가 통째로 fallback 으로 도는 걸 눈치채지 못한다.
        log.warning("분류 호출 실패: %s: %s", type(e).__name__, str(e)[:300])
        parsed = None

    if parsed is None:
        # 분류가 실패해도 응급 규칙만은 살려서 내려보낸다.
        r = Route(source="fallback")
        if rule_urgency(question) == "emergency":
            r.urgency = "emergency"
        r.tools = []
        return r

    return build_route(parsed, question, source="llm")
