"""MCP 데이터 실측 — 청킹·파싱·예산 결정은 이 출력을 보고 한다.

⚠️ 코퍼스는 서버 쪽에 이미 인덱싱돼 있다. 우리가 청킹하는 게 아니다.
   우리가 정하는 것은 (1) 페이지를 몇 장씩 열람할지 (2) 근거 블록을 몇 자로 자를지
   (3) 어떤 소스를 어떤 intent 에 붙일지 — 전부 이 스크립트의 실측치가 근거다.

실행 (Lunit 네트워크 안에서):
    export LUNIT_FM_API_KEY=lunit_...
    python scripts/probe_mcp.py            # 전체 프로브 → data/probe/*.json
    python scripts/probe_mcp.py --quick    # 도구 목록 + 데이터소스만

읽는 법 (출력 요약 끝에 판단 가이드가 붙는다):
    · page_chars      페이지당 문자 수 → evidence_chars_per_item, 열람 페이지 수 결정
    · cite_uid 형식   mcp_client.find_cite_uids 패턴이 맞는지 확인
    · latency_ms      도구별 지연 → max_tool_calls 예산의 실제 시간 환산
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medai.mcp_client import McpClient, find_cite_uids   # noqa: E402

OUT = ROOT / "data" / "probe"

# 실측용 대표 질의 — 인자 이름은 1차 프로브에서 서버 검증 에러로 확인한 실제 스키마
PROBES = [
    ("index_list_documents", {"corpus_tag": "guideline", "query": "고혈압 만성 신장질환"}),
    ("index_get_relevant_nodes", {"corpus_tag": "guideline", "query": "blood pressure target CKD"}),
    ("index_keyword_search", {"corpus_tag": "hira", "query": "요양급여"}),
    ("hira_updates_search", {"query": "고혈압 약제 급여기준"}),
    ("kcd_search_codes", {"name": "고혈압"}),
    ("kcd_get_name", {"code": "I10"}),
    ("openapi_mfds_check_drug_permission", {"drug_name": "타이레놀"}),
    ("openapi_mfds_get_drug_indication", {"drug_name": "아세트아미노펜"}),
    ("openapi_mfds_find_drugs_by_ingredient", {"ingredient": "아세트아미노펜"}),
    ("adr_retrieve_drug_info", {"drug_name": "acetaminophen"}),
    ("openapi_hira_get_drug_price", {"drug_name": "타이레놀"}),
    ("openapi_law_search", {"query": "국민건강보험법"}),
    ("openapi_law_list_articles", {"mst": "276651"}),   # 국민건강보험법 (1차 실측 mst)
    ("rag_vector_query", {"collection_name": "pubmed_abstracts",
                          "query": "hypertension chronic kidney disease target"}),
    ("rag_vector_query", {"collection_name": "hira_faq", "query": "본인부담 상한제"}),
]


def _sizes(text: str) -> dict:
    return {"chars": len(text), "lines": text.count("\n") + 1,
            "approx_tokens_ko": int(len(text) * 0.7)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    mcp = McpClient()
    print("연결 중…", flush=True)
    tools = await mcp.list_tools()
    (OUT / "tools.json").write_text(
        json.dumps(tools, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ tools/list: {len(tools)}개 → data/probe/tools.json")
    for t in tools:
        req = (t.get("inputSchema") or {}).get("required", [])
        print(f"   · {t['name']}  required={req}")

    # 데이터소스 스키마 — SQL 프롬프트에 박을 재료
    ds_raw = await mcp.call("rag_get_all_data_sources", {})
    (OUT / "data_sources.json").write_text(ds_raw, encoding="utf-8")
    print(f"\n✅ rag_get_all_data_sources → data/probe/data_sources.json "
          f"({len(ds_raw)}자)")
    for src_name in ("faers_12q4_25q4", "dailymed_26_08", "kcd",
                     "pubmed_abstracts", "hira_faq"):
        d = await mcp.call("rag_get_data_source_detail", {"source_name": src_name})
        (OUT / f"schema_{src_name}.json").write_text(d, encoding="utf-8")
        print(f"   · schema_{src_name}.json ({len(d)}자)")

    if a.quick:
        await mcp.aclose()
        return

    # 대표 질의 실측
    print("\n── 대표 질의 실측 ──")
    report = []
    for name, args in PROBES:
        t0 = time.perf_counter()
        out = await mcp.call(name, args)
        ms = int((time.perf_counter() - t0) * 1000)
        uids = find_cite_uids(out)
        rec = {"tool": name, "args": args, "latency_ms": ms,
               **_sizes(out), "cite_uids": uids[:5],
               "head": out[:400]}
        report.append(rec)
        mark = "⚠️" if out.startswith("[tool_error]") else "✅"
        print(f" {mark} {name:42s} {ms:6d}ms {len(out):7d}자 "
              f"cite_uid={len(uids)}")

    # 페이지 열람 실측 — 청킹 상수의 핵심 근거
    # 1차 프로브로 필드 구조 확인됨: [{doc_id, node_id, range:[s,e], summary, ...}]
    print("\n── index_get_page_content 실측 (페이지 폭 1·3·5장) ──")
    try:
        full = await mcp.call("index_get_relevant_nodes",
                              {"corpus_tag": "guideline",
                               "query": "blood pressure target CKD"})
        nodes = json.loads(full)
        (OUT / "sample_relevant_nodes.json").write_text(
            json.dumps(nodes, ensure_ascii=False), encoding="utf-8")
        top = nodes[0]
        doc_id, (s, e) = top["doc_id"], top["range"]
        page_stats = []
        for width in (1, 3, 5):
            t0 = time.perf_counter()
            pg = await mcp.call("index_get_page_content", {
                "corpus_tag": "guideline", "doc_id": doc_id,
                "start_page": s, "end_page": min(s + width - 1, s + 19),
            })
            ms = int((time.perf_counter() - t0) * 1000)
            uids = find_cite_uids(pg)
            per_page = len(pg) // max(width, 1)
            page_stats.append({"pages": width, "latency_ms": ms,
                               "chars": len(pg), "chars_per_page": per_page,
                               "cite_uids": uids[:3]})
            print(f"   {width}장: {ms}ms {len(pg)}자 (페이지당 ~{per_page}자) "
                  f"cite_uid={len(uids)}개 {uids[:2]}")
            if width == 1:
                (OUT / "sample_page_content.json").write_text(pg, encoding="utf-8")
        (OUT / "page_stats.json").write_text(
            json.dumps(page_stats, ensure_ascii=False, indent=2), encoding="utf-8")
        cpp = page_stats[0]["chars_per_page"]
        print(f"   → 판단: evidence_chars_per_item(현재 1800) 대비 페이지당 {cpp}자.")
        if cpp > 3600:
            print("     페이지가 커서 1페이지도 잘린다 — 열람 범위를 노드 range 그대로(좁게) 유지")
        else:
            print(f"     블록 하나에 약 {max(1, 1800 // max(cpp, 1))}페이지 분량이 들어간다")
    except Exception as ex:
        print(f"   (실패 — 수동 확인 필요: {ex!r})")

    (OUT / "probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in report if not r["head"].startswith("[tool_error]")]
    if ok:
        lat = [r["latency_ms"] for r in ok]
        print("\n── 판단 가이드 ──")
        print(f" · 도구 지연 중앙값 {statistics.median(lat)}ms / 최대 {max(lat)}ms")
        print(f"   → max_tool_calls=8 이면 검색 단계 최악 "
              f"{8 * max(lat) / 1000:.0f}s + 모델 추론. 턴 예산과 비교할 것")
        big = max(ok, key=lambda r: r["chars"])
        print(f" · 최대 응답 {big['tool']} {big['chars']}자 "
              f"(≈{big['approx_tokens_ko']}tok)")
        print("   → evidence_chars_per_item 과 tool 결과 8000자 컷이 적절한지 판단")
        no_cite = [r["tool"] for r in ok if not r["cite_uids"]]
        print(f" · cite_uid 없는 도구: {no_cite}")
        print("   → 이 도구들 결과는 인용 불가. finalize 에 안 실리므로")
        print("     프롬프트에서 '탐색용'으로만 쓰게 하거나 find_cite_uids 패턴을 고칠 것")
    print(f"\n전체 결과: data/probe/  (probe_report.json 포함)")
    await mcp.aclose()


if __name__ == "__main__":
    asyncio.run(main())
