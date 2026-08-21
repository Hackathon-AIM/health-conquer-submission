import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from medibot.config.settings import Settings
from medibot.core.schemas import ChatMessage, MedibotRequest
from medibot.orchestrator.workflow import MedibotWorkflow

L2_MCP_SOURCE_SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "guideline",
        "message": (
            "What do current hypertension guidelines say about blood pressure "
            "targets in chronic kidney disease?"
        ),
        "required_tools": ["index_get_page_content"],
        "require_cite_uid": True,
    },
    {
        "name": "medication",
        "message": (
            "Can ibuprofen interact with blood pressure medication? Use drug label "
            "or medication safety evidence."
        ),
        "required_tools": [
            "adr_retrieve_drug_info",
            "openapi_mfds_get_drug_indication",
        ],
        "require_cite_uid": False,
    },
    {
        "name": "hira_law",
        "message": (
            "심평원 HIRA 고혈압 급여기준을 HIRA 문서 page content 근거로 확인해줘."
        ),
        "required_tools": ["index_get_page_content"],
        "require_cite_uid": True,
    },
    {
        "name": "pubmed",
        "message": (
            "Find PubMed abstract evidence from trials or studies about dapagliflozin "
            "or SGLT2 inhibitors in chronic kidney disease and cardiovascular outcomes."
        ),
        "required_tools": ["rag_vector_query"],
        "require_cite_uid": False,
    },
]

SCENARIO_SETS = {"l2_mcp_sources": L2_MCP_SOURCE_SCENARIOS}


async def run_live_smoke(scenario_set: str, output_path: Path) -> dict[str, Any]:
    scenarios = SCENARIO_SETS[scenario_set]
    trace_path = output_path.with_suffix(".traces.jsonl")
    results = []
    started = time.perf_counter()

    for scenario in scenarios:
        result = await _run_scenario(scenario, trace_path)
        results.append(result)

    summary = {
        "scenario_set": scenario_set,
        "passed": all(result["passed"] for result in results),
        "scenario_count": len(results),
        "trace_path": str(trace_path),
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "scenarios": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


async def _run_scenario(
    scenario: dict[str, Any], trace_path: Path
) -> dict[str, Any]:
    base_settings = Settings()
    settings = Settings(
        final_model_provider="lunit_l2",
        require_l2_final=True,
        allow_fallback=False,
        rag_backend="lunit_mcp",
        trace_path=trace_path,
        request_timeout_s=max(base_settings.request_timeout_s, 90.0),
        source_timeout_s=max(base_settings.source_timeout_s, 60.0),
    )
    started = time.perf_counter()
    try:
        workflow = MedibotWorkflow(settings=settings)
        response = await workflow.handle(
            MedibotRequest(
                session_id=f"live-smoke-{scenario['name']}",
                messages=[ChatMessage(role="user", content=scenario["message"])],
            )
        )
        trace = _read_last_trace(trace_path)
        result = summarize_trace(scenario, trace)
        result["answer_preview"] = response.answer[:500]
    except Exception as exc:
        trace = _read_last_trace(trace_path) if trace_path.exists() else {}
        result = summarize_trace(scenario, trace)
        result["passed"] = False
        result["errors"].append(f"exception:{type(exc).__name__}:{exc}")
    result["elapsed_ms"] = (time.perf_counter() - started) * 1000
    return result


def summarize_trace(scenario: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    evidence = list(trace.get("retrieval_results") or [])
    raw_tool_names = [
        str(item.get("raw_tool_name"))
        for item in evidence
        if item.get("raw_tool_name")
    ]
    cite_uids = list(trace.get("l2_selected_cite_uids") or [])
    if not cite_uids:
        cite_uids = [
            str(item.get("cite_uid")) for item in evidence if item.get("cite_uid")
        ]

    errors: list[str] = []
    if trace.get("generation_mode") != "remote:lunit_l2:harness":
        errors.append("generation_mode_not_l2_harness")
    if int(trace.get("l2_mcp_tool_call_count") or 0) <= 0:
        errors.append("no_mcp_tool_calls")
    if trace.get("l2_retrieval_phase_status") == "no_evidence":
        errors.append("retrieval_status_no_evidence")

    required_tools = list(scenario.get("required_tools") or [])
    if required_tools and not any(tool in raw_tool_names for tool in required_tools):
        errors.append("missing_required_tool:" + "|".join(required_tools))
    if scenario.get("require_cite_uid") and not cite_uids:
        errors.append("missing_cite_uid")

    return {
        "name": scenario["name"],
        "passed": not errors,
        "errors": errors,
        "generation_mode": trace.get("generation_mode"),
        "l2_retrieval_phase_status": trace.get("l2_retrieval_phase_status"),
        "l2_mcp_tool_call_count": int(trace.get("l2_mcp_tool_call_count") or 0),
        "l2_selected_cite_uids": cite_uids,
        "l2_finalize_note": trace.get("l2_finalize_note"),
        "raw_tool_names": raw_tool_names,
        "evidence_count": len(evidence),
    }


def _read_last_trace(trace_path: Path) -> dict[str, Any]:
    lines = [
        line
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        return {}
    parsed = json.loads(lines[-1])
    return parsed if isinstance(parsed, dict) else {}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run live L2/MCP smoke scenarios.")
    parser.add_argument("--scenario-set", choices=sorted(SCENARIO_SETS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    summary = asyncio.run(run_live_smoke(args.scenario_set, args.output))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
