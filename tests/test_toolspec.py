"""MCP 도구 언어 계약 회귀 테스트 — 네트워크 없이 돈다.

    python tests/test_toolspec.py

지키려는 것
  1. 도구마다 **그 코퍼스의 언어**로 인자가 들어간다. 한 도메인 서브셋 안에서도
     영문(DailyMed)과 한글(식약처)이 섞여 있으므로 질의 하나로는 안 된다.
  2. 맞출 수 없으면 **부르지 않는다.** 틀린 언어는 0건이 오는데 비용은 같고,
     그 비용이 곧 timeout 이고 timeout 이 곧 0점이다.
  3. 같은 호출을 두 번 하지 않는다.

계약의 근거는 라이브 MCP 에서 받은 tools/list 스키마와, 같은 질문을 한/영으로
던져 본 실측이다 (hira: 한국어 keyword score 8 vs 영어 2 / guideline: 영어 0.735 vs
한국어 0.552).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import toolspec as T  # noqa: E402
from retrieval import run_retrieval  # noqa: E402

CTX = {
    "query_ko": "타이레놀을 복용한 뒤 발진이 생겼는데 약 때문일 수 있나요",
    "query_en": "can acetaminophen cause a skin rash after starting the drug",
    "keywords_ko": "약물이상반응, 발진",
    "keywords_en": "adverse reaction, rash",
    "drug_ko": "타이레놀",
    "drug_en": "acetaminophen",
}


def norm(tool, args, ctx=None):
    return T.normalize_args(tool, dict(args), ctx if ctx is not None else CTX)


# ── 약 이름: 언어가 정면으로 갈리는 자리 ──────────────────────
def test_dailymed_gets_english_drug_name() -> None:
    """스키마가 'English brand or generic (INN)' 이라고 못박는다."""
    args, skip = norm("adr_retrieve_drug_info", {"drug_name": "타이레놀"})
    assert skip is None
    assert args["drug_name"] == "acetaminophen", args


def test_mfds_gets_korean_drug_name() -> None:
    """식약처 허가 제품명은 한글이다."""
    for tool in ("openapi_mfds_check_drug_permission", "openapi_mfds_get_drug_indication"):
        args, skip = norm(tool, {"drug_name": "acetaminophen"})
        assert skip is None
        assert args["drug_name"] == "타이레놀", (tool, args)


def test_hira_drug_price_gets_korean() -> None:
    args, skip = norm("openapi_hira_get_drug_price", {"drug_name": "Tylenol"})
    assert skip is None and args["drug_name"] == "타이레놀"


def test_ingredient_is_title_cased() -> None:
    """스키마가 case-sensitive 라고 명시한다 — 소문자로 던지면 안 걸린다."""
    args, skip = norm(
        "openapi_mfds_find_drugs_by_ingredient",
        {"ingredient": "medroxyprogesterone acetate"},
        {"query_en": "x", "drug_en": "medroxyprogesterone acetate"},
    )
    assert skip is None
    assert args["ingredient"] == "Medroxyprogesterone Acetate", args


def test_one_domain_needs_both_languages() -> None:
    """domain=adr 서브셋 안에서 영문 도구와 한글 도구가 갈린다.

    이게 이 작업 전체의 이유다. 질의가 하나면 둘 중 하나는 반드시 틀린다.
    """
    en, _ = norm("adr_retrieve_drug_info", {"drug_name": "타이레놀"})
    ko, _ = norm("openapi_mfds_get_drug_indication", {"drug_name": "타이레놀"})
    assert T.lang_of(en["drug_name"]) == "en"
    assert T.lang_of(ko["drug_name"]) == "ko"


# ── 코퍼스가 언어를 정한다 ────────────────────────────────────
def test_hira_corpus_takes_korean() -> None:
    args, skip = norm("index_get_relevant_nodes", {"corpus_tag": "hira", "query": "english text"})
    assert skip is None and T.lang_of(args["query"]) == "ko", args


def test_guideline_corpus_takes_english() -> None:
    """코퍼스가 EAU·NCCN 영문 가이드라인이다. 실측 0.735 vs 0.552."""
    args, skip = norm("index_get_relevant_nodes", {"corpus_tag": "guideline", "query": "한글 질의"})
    assert skip is None and T.lang_of(args["query"]) == "en", args


def test_vector_collection_decides_language() -> None:
    pub, _ = norm("rag_vector_query", {"collection_name": "pubmed_abstracts", "query": "한글"})
    faq, _ = norm("rag_vector_query", {"collection_name": "hira_faq", "query": "english"})
    assert T.lang_of(pub["query"]) == "en", pub
    assert T.lang_of(faq["query"]) == "ko", faq


def test_list_documents_matches_english_summaries() -> None:
    """문서 본문은 한글이어도 summary 가 영문으로 생성돼 있다 — 실측 확인."""
    args, _ = norm("index_list_documents", {"corpus_tag": "hira", "query": "한글 질의"})
    assert T.lang_of(args["query"]) == "en", args


def test_korean_law_stays_korean() -> None:
    args, skip = norm("openapi_law_search", {"query": "national health insurance act"})
    assert skip is None and T.lang_of(args["query"]) == "ko"


# ── 형태: 정확 일치 도구는 문장이 아니라 키워드 ───────────────
def test_keyword_search_gets_keywords_not_a_sentence() -> None:
    """index_keyword_search 는 raw page text 에 정확히 일치하는 것을 찾는다.

    문장을 통째로 던지면 반드시 0건이다.
    """
    args, skip = norm("index_keyword_search", {"corpus_tag": "hira", "query": CTX["query_ko"]})
    assert skip is None
    q = args["query"]
    assert "," in q and len(q) < 40, q
    assert "인가요" not in q, q  # 어미가 붙은 토큰은 원문과 안 맞는다


def test_keyword_search_on_guideline_uses_english_keywords() -> None:
    args, _ = norm("index_keyword_search", {"corpus_tag": "guideline", "query": "한글 문장입니다"})
    assert T.lang_of(args["query"]) == "en", args


def test_short_phrase_is_not_split() -> None:
    """짧은 구는 원문에 그대로 있을 후보다. 쪼개면 다른 질의가 된다.

    모델이 "위험도 보정" 을 넣었는데 우리가 "위험도, 보정" 으로 바꾸면
    정확 일치 대상이 달라진다 — 도우려다 망치는 자리다.
    """
    args, skip = norm("index_keyword_search", {"corpus_tag": "hira", "query": "위험도 보정"})
    assert skip is None
    assert args["query"] == "위험도 보정", args


def test_sentence_is_split_into_keywords() -> None:
    args, _ = norm("index_keyword_search", {"corpus_tag": "hira", "query": "위험도 보정은 어떻게 하나요"})
    assert "," in args["query"], args


def test_to_keywords_keeps_an_existing_list() -> None:
    assert T.to_keywords("약물이상반응, 발진") == "약물이상반응, 발진"


# ── 맞출 수 없으면 부르지 않는다 ──────────────────────────────
def test_skips_when_the_required_language_is_missing() -> None:
    """영문 변형이 없는데 DailyMed 를 부르면 0건이 온다 — 예산만 태운다."""
    thin = {"query_ko": "타이레놀 부작용", "query_en": "", "drug_ko": "타이레놀", "drug_en": ""}
    args, skip = norm("adr_retrieve_drug_info", {"drug_name": "타이레놀"}, thin)
    assert skip is not None, "부르면 안 되는데 통과시켰다"


def test_code_tools_are_untouched() -> None:
    args, skip = norm("openapi_hira_disease_check_code", {"code": "C50.9"})
    assert skip is None and args == {"code": "C50.9"}


def test_kcd_defaults_are_set_explicitly() -> None:
    """kcd 는 lang 인자를 받는다. 한국어 사용자에게는 한글 병명이 필요하다."""
    args, _ = norm("kcd_get_name", {"code": "A00.0"})
    assert args["lang"] == "both", args


# ── 예산: 언어 불일치와 중복이 예산을 태우지 않는다 ───────────
class FakeMCP:
    def __init__(self):
        self.calls = []

    async def list_tools(self, timeout=None):
        return [
            {"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}}
            for n in ("adr_retrieve_drug_info", "openapi_mfds_get_drug_indication")
        ]

    async def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, dict(arguments)))
        return {"cite_uid": f"cite-{len(self.calls):016x}", "text": "본문"}


def _tc(i, name, args):
    return {
        "id": f"c{i}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


class ScriptedFM:
    def __init__(self, script):
        self.script = script

    async def __call__(self, messages, max_tokens, extra=None, timeout=None, **kw):
        if not self.script:
            return {"choices": [{"message": {"content": "", "tool_calls": []}}]}
        return self.script.pop(0)


def test_retrieval_fixes_language_before_dispatch() -> None:
    fm = ScriptedFM([{
        "choices": [{"message": {"content": "", "tool_calls": [
            _tc(1, "adr_retrieve_drug_info", {"drug_name": "타이레놀"}),
        ]}}]
    }])
    mcp = FakeMCP()
    asyncio.run(run_retrieval("q", ["adr_retrieve_drug_info"], fm, mcp, budget=2, ctx=CTX))
    assert mcp.calls, "도구가 안 불렸다"
    assert mcp.calls[0][1]["drug_name"] == "acetaminophen", mcp.calls


def test_duplicate_calls_do_not_spend_budget() -> None:
    fm = ScriptedFM([{
        "choices": [{"message": {"content": "", "tool_calls": [
            _tc(1, "adr_retrieve_drug_info", {"drug_name": "타이레놀"}),
            _tc(2, "adr_retrieve_drug_info", {"drug_name": "acetaminophen"}),  # 정규화하면 같아진다
        ]}}]
    }])
    mcp = FakeMCP()
    res = asyncio.run(run_retrieval("q", ["adr_retrieve_drug_info"], fm, mcp, budget=3, ctx=CTX))
    assert len(mcp.calls) == 1, f"같은 호출이 {len(mcp.calls)}번 나갔다"
    assert res.tool_calls_used == 1
    assert any("이미 불렀다" in t for t in res.trace), res.trace


def test_unfixable_call_is_skipped_without_spending_budget() -> None:
    thin = {"query_ko": "타이레놀 부작용", "drug_ko": "타이레놀"}
    fm = ScriptedFM([{
        "choices": [{"message": {"content": "", "tool_calls": [
            _tc(1, "adr_retrieve_drug_info", {"drug_name": "타이레놀"}),
        ]}}]
    }])
    mcp = FakeMCP()
    res = asyncio.run(run_retrieval("q", ["adr_retrieve_drug_info"], fm, mcp, budget=3, ctx=thin))
    assert mcp.calls == [], "부를 수 없는 호출이 나갔다"
    assert res.tool_calls_used == 0, "예산을 태웠다"


# ── 헛도는 도구 차단 · 체인 도메인 예산 ───────────────────────
class BarrenMCP(FakeMCP):
    """근거(cite_uid)를 하나도 안 주는 도구. 법령 검색이 실제로 이렇다."""

    async def list_tools(self, timeout=None):
        self.list_timeout = timeout
        return [
            {"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}}
            for n in ("openapi_law_search", "openapi_law_list_articles")
        ]

    async def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, dict(arguments)))
        return {"items": [], "message": "3 law match. Open one with openapi_law_list_articles(mst)."}


def test_a_tool_that_yields_no_evidence_is_closed_after_two_tries() -> None:
    """실측: 법령 문항에서 law_search 를 네 번 불러 예산을 태우고 답을 못 냈다.

    인자를 조금씩 바꾸면 중복 제거로는 못 막는다. 성과로 막아야 한다.
    """
    tcs = [
        _tc(1, "openapi_law_search", {"query": "국민건강보험법"}),
        _tc(2, "openapi_law_search", {"query": "국민건강보험"}),
        _tc(3, "openapi_law_search", {"query": "건강보험법"}),
        _tc(4, "openapi_law_search", {"query": "국민건강보험법 시행령"}),
    ]
    fm = ScriptedFM([{"choices": [{"message": {"content": "", "tool_calls": tcs}}]}])
    mcp = BarrenMCP()
    ctx = {"query_ko": "국민건강보험법 이의신청 조항", "query_en": "appeal article"}
    res = asyncio.run(
        run_retrieval("q", ["openapi_law_search"], fm, mcp, budget=6, ctx=ctx)
    )
    assert len(mcp.calls) == 2, f"{len(mcp.calls)}번 불렀다 — 두 번이면 닫아야 한다"
    assert any("새 근거를 못 냈다" in t for t in res.trace), res.trace


def test_a_productive_tool_is_not_closed() -> None:
    """근거를 내는 도구는 계속 쓸 수 있어야 한다."""
    tcs = [_tc(i, "adr_retrieve_drug_info", {"drug_name": f"drug{i}"}) for i in (1, 2, 3)]
    fm = ScriptedFM([{"choices": [{"message": {"content": "", "tool_calls": tcs}}]}])
    mcp = FakeMCP()
    res = asyncio.run(
        run_retrieval("q", ["adr_retrieve_drug_info"], fm, mcp, budget=6, ctx=CTX)
    )
    assert len(mcp.calls) == 3, f"{len(mcp.calls)}번만 불렀다"


def test_chain_domains_get_a_bigger_hop_budget() -> None:
    """법령은 근거 하나에 3홉이 든다. 예산 3 이면 한 번만 헛돌아도 답이 없다."""
    from router import DOMAIN_MIN_HOPS, build_route

    r = build_route({"domain": "korean_law"}, "국민건강보험법 이의신청", source="llm")
    assert r.min_hops >= 3, r.min_hops
    assert DOMAIN_MIN_HOPS["korean_law"] > DOMAIN_MIN_HOPS.get("mfds", 0)

    plain = build_route({"domain": "mfds"}, "타이레놀 허가", source="llm")
    assert plain.min_hops == 0, "체인이 아닌 도메인까지 예산을 올리면 안 된다"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
