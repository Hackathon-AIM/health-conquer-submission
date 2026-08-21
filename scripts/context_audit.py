"""컨텍스트 예산 감사 — 우리 워크로드에서 실제로 몇 토큰이 오가는지 잰다.

가이드의 절감률(98.7%, 94%, >95%)은 전부 남의 워크로드 숫자다. 우리 숫자는 우리가 재야 한다.
이 스크립트는 두 가지를 한다.

  1. 오프라인(기본): 합성 대용량 MCP 결과를 프록시에 통과시켜 원문 → 영수증 → 근거 블록의
     토큰 수를 비교한다. 네트워크도 키도 필요 없다.
  2. --live: 실제 MCP 서버에서 tools/list 를 받아 메뉴세(menu tax)를 재고, 실제 툴을 한 번
     불러 결과 크기를 잰다. LUNIT_FM_API_KEY 가 필요하다.

    python scripts/context_audit.py
    python scripts/context_audit.py --live --query "와파린과 아스피린 병용"
    python scripts/context_audit.py --budget 1024 2560 8192
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from submission.config import SETTINGS  # noqa: E402
from submission.context import COUNTER, ContextBudget  # noqa: E402
from submission.orchestrator import FINALIZE_TOOL, NativeDriver  # noqa: E402
from submission.rag import SessionIndex  # noqa: E402
from submission.toolspec import build_tools  # noqa: E402


def _synthetic_result(documents: int = 60, paragraphs: int = 30) -> dict[str, Any]:
    """실제로 마주치는 모양 — 본문이 EmbeddedResource 안에 JSON 문자열로 들어 있다."""
    payload = {
        "results": [
            {
                "cite_uid": f"guideline:{index}",
                "title": f"고혈압 진료지침 {index}장",
                "url": f"https://example.org/doc/{index}?utm_source=mcp&utm_medium=tool",
                "content": "\n\n".join(
                    f"{index}-{p} 성인 고혈압 환자의 목표 혈압은 수축기 130 mmHg 미만이다. "
                    "이뇨제 병용 시 저칼륨혈증과 신기능을 주기적으로 확인한다. "
                    "임신부에서는 ACE 억제제와 ARB 를 금기한다. " * 3
                    for p in range(paragraphs)
                ),
            }
            for index in range(documents)
        ]
    }
    return {
        "content": [
            {"type": "text", "text": f"search completed: {documents} documents"},
            {"type": "resource", "resource": {
                "uri": "index://search/1",
                "mimeType": "application/json",
                "text": json.dumps(payload, ensure_ascii=False),
            }},
        ]
    }


def _tokens(value: Any) -> int:
    if isinstance(value, str):
        return COUNTER.estimate_text(value)
    return COUNTER.estimate_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def audit_result(result: Any, query: str, budget: ContextBudget) -> dict[str, Any]:
    driver = NativeDriver.__new__(NativeDriver)
    driver.settings = SETTINGS
    index = SessionIndex(COUNTER, chunk_tokens=NativeDriver._chunk_tokens(budget))
    observed: dict[str, Any] = {}
    NativeDriver._absorb("index_keyword_search", result, index, observed)

    raw = _tokens(result)
    legacy = min(raw, COUNTER.estimate_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":"))[:4_000]
    ))
    receipt = driver._receipt(result, index, query, budget)
    picked = index.select(query, token_budget=budget.evidence, top_k=SETTINGS.max_evidence_blocks)
    evidence = index.render(picked)
    return {
        "raw": raw,
        "legacy_4000_chars": legacy,
        "receipt": _tokens(receipt),
        "evidence": _tokens(evidence),
        "docs": len(index.documents),
        "chunks": len(index),
        "picked": len(picked),
        "receipt_text": receipt,
        "evidence_text": evidence,
    }


def _print_budget(budget: ContextBudget) -> None:
    parts = budget.describe()
    print(f"  예산 {parts['total']:>6} tok  ="
          f"  system {parts['system']}"
          f" + 대화 {parts['conversation']}"
          f" + 근거 {parts['evidence']}"
          f" + 툴 {parts['tools']}"
          f" + 예비 {parts['reserve']}")


def offline(budgets: list[int], query: str) -> None:
    result = _synthetic_result()
    print("=" * 78)
    print("오프라인 감사 — 합성 대용량 MCP 결과")
    print("=" * 78)
    for total in budgets:
        budget = ContextBudget.allocate(total)
        _print_budget(budget)
        report = audit_result(result, query, budget)
        cut = 100 * (1 - report["evidence"] / report["raw"])
        print(f"    원문            {report['raw']:>8,} tok   ({report['docs']} docs → {report['chunks']} chunks)")
        print(f"    기존(4000자 컷) {report['legacy_4000_chars']:>8,} tok   ← 여전히 JSON 중간에서 잘린다")
        print(f"    영수증          {report['receipt']:>8,} tok   ← 검색 루프에서 모델이 보는 것")
        print(f"    근거 블록       {report['evidence']:>8,} tok   ← 생성 단계에 실제로 들어가는 것 ({report['picked']} 조각)")
        print(f"    절감            {cut:>7.2f}%")
        assert report["evidence"] <= budget.evidence + 40, "근거 블록이 예산을 넘었다"
        assert report["receipt"] <= budget.evidence, "영수증이 예산을 넘었다"
        print()
    print("샘플 영수증:")
    for line in audit_result(result, query, ContextBudget.allocate(2560))["receipt_text"].splitlines():
        print(f"    {line}")
    print()


def live(query: str, budgets: list[int]) -> None:
    from submission.mcp import LunitMCPClient, MCPError, openai_tools

    if not SETTINGS.fm_api_key:
        print("LUNIT_FM_API_KEY 가 없어 --live 를 건너뜁니다.")
        return
    client = LunitMCPClient(SETTINGS)
    try:
        definitions = client.list_tools()
    except MCPError as exc:
        print(f"tools/list 실패: {exc}")
        return

    full = _tokens(openai_tools(definitions))
    print("=" * 78)
    print("라이브 감사 — 실제 MCP 서버")
    print("=" * 78)
    print(f"  툴 {len(definitions)}개, 정의를 그대로 실으면 {full:,} tok (menu tax)")
    for total in budgets:
        budget = ContextBudget.allocate(total)
        tools, diagnostics = build_tools(
            definitions, query, COUNTER,
            token_budget=budget.tools, always_include=[FINALIZE_TOOL],
            max_tools=SETTINGS.max_mcp_tools,
        )
        print(f"  예산 {total:>6}: 툴 {len(tools) - 1}개 / level {diagnostics['level']}"
              f" / {_tokens(tools):,} tok  {diagnostics['selected']}")

    name = next((t["function"]["name"] for t in tools if t["function"]["name"] != "finalize_retrieval"), "")
    if not name:
        print("  예산 안에 들어가는 툴이 없다 — 규칙 라우팅 경로로 동작한다.")
        return
    definition = next(d for d in definitions if d.get("name") == name)
    from submission.orchestrator import _auto_arguments

    arguments = _auto_arguments(definition, query)
    if arguments is None:
        print(f"  {name}: 인자를 자동으로 못 채운다 (앞선 조회 결과가 필요한 툴)")
        return
    print(f"\n  호출: {name}({json.dumps(arguments, ensure_ascii=False)})")
    try:
        result = client.call_tool(name, arguments)
    except MCPError as exc:
        print(f"  호출 실패: {exc}")
        return
    budget = ContextBudget.allocate(budgets[-1])
    report = audit_result(result, query, budget)
    print(f"    원문      {report['raw']:>8,} tok")
    print(f"    영수증    {report['receipt']:>8,} tok")
    print(f"    근거 블록 {report['evidence']:>8,} tok")
    if report["raw"]:
        print(f"    절감      {100 * (1 - report['evidence'] / report['raw']):>7.2f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=int, nargs="+", default=[1024, 2560, 8192])
    parser.add_argument("--query", default="고혈압 환자의 목표 혈압과 이뇨제 병용 시 주의점")
    parser.add_argument("--live", action="store_true", help="실제 MCP 서버를 호출한다")
    args = parser.parse_args()

    print(f"\n토큰 추정기: scale={COUNTER.scale:.3f} (실측 보정 {COUNTER.samples}회)")
    print(f"현재 설정: LUNIT_MAX_INPUT_TOKENS={os.environ.get('LUNIT_MAX_INPUT_TOKENS', '2560 (기본)')}\n")
    offline(args.budget, args.query)
    if args.live:
        live(args.query, args.budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
