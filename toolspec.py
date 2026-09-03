"""MCP 도구별 질의 계약 — 어떤 언어·어떤 형태로 인자를 넣어야 하는가.

왜 이 파일이 필요한가
  라우터는 질의를 **하나**만 만들어 왔다. 그런데 한 도메인의 도구 묶음 안에 언어가
  섞여 있다. 예를 들어 domain="adr" 의 서브셋은 이렇다.

      adr_retrieve_drug_info            drug_name 은 **영문** brand/INN ('Tylenol')
      openapi_mfds_get_drug_indication  drug_name 은 **한글** 제품명 ('키트루다')
      rag_sql_query                     FAERS·DailyMed 는 **영문** 데이터다

  질의가 하나뿐이면 이 셋 중 최소 하나는 반드시 틀린 언어로 들어간다. 틀린 언어로
  들어간 호출은 대개 0건을 돌려주는데, **비용은 성공한 호출과 똑같이 든다.**
  도구 호출 예산과 시간 예산을 그렇게 태우면 답을 쓸 시간이 사라지고, 그 문항은
  답이 나빠서가 아니라 도달하지 못해서 0점이 된다.

  그래서 (1) 라우터가 한/영 두 벌을 같은 호출에서 만들고,
       (2) 도구를 부르기 직전에 이 표를 보고 인자를 맞추고,
       (3) 맞출 수 없으면 **부르지 않는다** — 예산을 아끼는 쪽이 이득이다.

실측 근거 (2026-08-21, 라이브 MCP)
  index_get_relevant_nodes · hira      한국어 질의가 주제에 맞는 노드를 물어온다.
                                       영어 질의는 점수가 더 높게 나오는데도
                                       "Preface" 같은 일반 문단을 집어온다.
  index_keyword_search     · hira      한국어 score 8 vs 영어 score 2. 원문이 한글이다.
  index_get_relevant_nodes · guideline 영어 0.735 vs 한국어 0.552.
                                       코퍼스가 EAU·NCCN 영문 가이드라인이다.
  index_list_documents                 summary 가 영문으로 생성돼 있어 영어가 맞는다.

도구 스키마는 라이브 tools/list 에서 받아 확인했다 — 인자 이름과 설명은 추측이 아니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_HANGUL_RE = re.compile(r"[가-힣]")

# ── 코퍼스·컬렉션·DB 의 언어 ──────────────────────────────────
CORPUS_LANG = {"hira": "ko", "guideline": "en"}
COLLECTION_LANG = {"pubmed_abstracts": "en", "hira_faq": "ko"}
DB_LANG = {"faers_12q4_25q4": "en", "dailymed_26_08": "en", "kcd": "any"}

# index_list_documents 만 예외다 — 문서 본문이 아니라 **영문 summary** 에 매칭한다.
SUMMARY_MATCHED = {"index_list_documents"}


@dataclass(frozen=True)
class QuerySpec:
    """도구 하나의 질의 인자 계약."""

    field: str  # 질의가 들어가는 인자 이름
    lang: str  # ko | en | any | corpus  (corpus = corpus_tag/collection_name/db_name 을 따른다)
    form: str  # phrase | keywords | drug | ingredient
    note: str = ""


# 질의를 받는 도구만 적는다. 여기 없는 도구(코드·식별자 인자)는 언어와 무관하다.
TOOL_QUERY: dict[str, QuerySpec] = {
    # ── 약: 언어가 정면으로 갈리는 자리 ──────────────────────
    "adr_retrieve_drug_info": QuerySpec(
        "drug_name", "en", "drug", "DailyMed. 스키마가 'English brand or generic (INN)' 이라고 못박는다"
    ),
    "openapi_mfds_check_drug_permission": QuerySpec(
        "drug_name", "ko", "drug", "식약처 허가 제품명은 한글이다 ('옵디보주')"
    ),
    "openapi_mfds_get_drug_indication": QuerySpec(
        "drug_name", "ko", "drug", "식약처 허가 제품명은 한글이다 ('키트루다')"
    ),
    "openapi_mfds_find_drugs_by_ingredient": QuerySpec(
        "ingredient", "en", "ingredient",
        "성분명은 영문 INN 선호 + **대소문자를 가린다**. 'medroxyprogesterone' 은 안 걸리고 "
        "'Medroxyprogesterone' 은 걸린다",
    ),
    "openapi_hira_get_drug_price": QuerySpec(
        "drug_name", "ko", "drug", "HIRA 약가의 품명은 한글이다"
    ),
    # ── 한국 제도·법령: 전부 한글 코퍼스 ────────────────────
    "openapi_law_search": QuerySpec("query", "ko", "phrase", "법령명 부분일치"),
    "openapi_law_list_articles": QuerySpec("contains", "ko", "keywords", "조문 제목 필터"),
    "hira_updates_search": QuerySpec("query", "ko", "phrase", "고시 제목·본문 문구"),
    # ── 코퍼스가 언어를 정하는 것들 ─────────────────────────
    "index_get_relevant_nodes": QuerySpec("query", "corpus", "phrase", "길고 서술적인 질의가 낫다(스키마 명시)"),
    "index_keyword_search": QuerySpec("query", "corpus", "keywords", "원문 정확 일치. 콤마로 복수 키워드"),
    "index_list_documents": QuerySpec("query", "corpus", "phrase", "문서 summary 가 영문이라 영어가 맞는다"),
    "rag_vector_query": QuerySpec("query", "corpus", "phrase", "collection_name 이 언어를 정한다"),
    # ── 양쪽 다 되는 것 ─────────────────────────────────────
    "kcd_search_codes": QuerySpec("name", "any", "phrase", "lang 인자가 auto/kor/eng/both 를 받는다"),
}

# 스키마가 "기본값에 맡기지 말라"고 말하거나, 우리가 명시하는 편이 나은 인자들.
STATIC_DEFAULTS: dict[str, dict[str, Any]] = {
    # 한국어 사용자에게는 한글 병명이 필요하고, 교차 확인에는 영문도 있어야 한다.
    "kcd_get_name": {"lang": "both"},
    "kcd_search_codes": {"lang": "auto"},
}


def lang_of(text: str) -> str:
    """한글이 하나라도 있으면 한국어로 본다. 약명·코드가 섞인 문장이 대부분이라 이 정도가 맞다."""
    return "ko" if _HANGUL_RE.search(text or "") else "en"


def corpus_lang(tool: str, args: dict) -> str:
    """corpus_tag / collection_name / db_name 에서 그 코퍼스의 언어를 읽는다."""
    if tool in SUMMARY_MATCHED:
        # 본문이 아니라 영문 summary 에 매칭한다.
        return "en"
    tag = args.get("corpus_tag")
    if isinstance(tag, str) and tag in CORPUS_LANG:
        return CORPUS_LANG[tag]
    col = args.get("collection_name")
    if isinstance(col, str) and col in COLLECTION_LANG:
        return COLLECTION_LANG[col]
    db = args.get("db_name")
    if isinstance(db, str) and db in DB_LANG:
        return DB_LANG[db]
    return "any"


def to_keywords(phrase: str, limit: int = 3) -> str:
    """서술형 질의를 정확 일치용 키워드로 줄인다.

    index_keyword_search 는 raw page text 에 **정확히** 일치하는 것을 찾는다.
    문장을 통째로 던지면 반드시 0건이다. 조사가 붙은 토큰도 원문과 어긋나므로
    긴 명사만 남기고 콤마로 잇는다.
    """
    phrase = (phrase or "").strip()
    if not phrase:
        return ""
    if "," in phrase and len(phrase) < 60:
        return phrase  # 이미 키워드 목록이다
    words = re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z\-]{2,}", phrase)
    # 조사·어미가 붙어 원문과 안 맞는 토큰을 피하려고 긴 것부터 고른다.
    words = sorted(set(words), key=lambda w: -len(w))[:limit]
    return ", ".join(words)


def looks_like_a_sentence(text: str) -> bool:
    """정확 일치 도구에 넣기엔 문장인가.

    짧은 구는 그대로 두는 편이 낫다 — 원문에 그대로 등장할 후보이기 때문이다.
    쪼개는 것은 문장일 때만 이득이다.
    """
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t or "？" in t:
        return True
    if re.search(r"(하나요|되나요|인가요|입니까|알려줘|어떻게|무엇|왜)", t):
        return True
    return len(t) > 25


def _pick(ctx: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = (ctx.get(k) or "").strip()
        if v:
            return v
    return ""


def normalize_args(tool: str, args: dict, ctx: dict) -> tuple[dict, str | None]:
    """도구를 부르기 직전에 인자를 계약에 맞춘다.

    `ctx` 는 라우터가 한 번의 분류 호출에서 같이 만들어 둔 질의 변형들이다:
      query_ko / query_en / keywords_ko / keywords_en / drug_ko / drug_en

    돌려주는 것: (고친 인자, 건너뛸 사유 또는 None)
    사유가 있으면 **부르지 않는다.** 틀린 언어로 부르면 0건이 오는데 비용은 같다.
    """
    args = dict(args or {})

    for k, v in STATIC_DEFAULTS.get(tool, {}).items():
        args.setdefault(k, v)

    spec = TOOL_QUERY.get(tool)
    if spec is None:
        return args, None  # 코드·식별자만 받는 도구

    want = spec.lang
    if want == "corpus":
        want = corpus_lang(tool, args)

    current = args.get(spec.field)
    current = current.strip() if isinstance(current, str) else ""

    # 1) 언어를 맞춘다.
    if want in ("ko", "en"):
        if spec.form == "drug":
            pool = ("drug_ko", "query_ko") if want == "ko" else ("drug_en", "query_en")
        elif spec.form == "ingredient":
            pool = ("ingredient_en", "drug_en", "query_en")
        elif spec.form == "keywords":
            pool = ("keywords_ko", "query_ko") if want == "ko" else ("keywords_en", "query_en")
        else:
            pool = ("query_ko",) if want == "ko" else ("query_en",)

        if not current or lang_of(current) != want:
            replacement = _pick(ctx, pool)
            if not replacement:
                return args, f"{tool} 은 {want} 인자가 필요한데 그 언어의 질의가 없다"
            args[spec.field] = replacement
            current = replacement

    if not current:
        # any 언어인데도 값이 비어 있으면 라우터 질의를 그대로 쓴다.
        current = _pick(ctx, ("query_ko", "query_en"))
        if not current:
            return args, f"{tool} 에 넣을 질의가 없다"
        args[spec.field] = current

    # 2) 형태를 맞춘다.
    if spec.form == "keywords" and looks_like_a_sentence(args[spec.field]):
        # 이미 짧은 구를 넣었으면 건드리지 않는다. "위험도 보정" 은 원문에 그대로
        # 있는 정확 일치 후보인데, 이걸 "위험도, 보정" 으로 쪼개면 다른 질의가 된다.
        args[spec.field] = to_keywords(args[spec.field])
        if not args[spec.field]:
            return args, f"{tool} 에 넣을 키워드를 못 만들었다"
    elif spec.form == "ingredient":
        # 스키마가 case-sensitive 라고 명시한다. 소문자로 던지면 안 걸린다.
        args[spec.field] = " ".join(w[:1].upper() + w[1:] for w in args[spec.field].split())

    return args, None


def call_key(tool: str, args: dict) -> str:
    """같은 요청 안에서 같은 호출을 두 번 하지 않기 위한 키."""
    items = sorted((k, str(v)) for k, v in (args or {}).items())
    return tool + "|" + "&".join(f"{k}={v}" for k, v in items)
