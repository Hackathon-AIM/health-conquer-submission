from medibot.evaluation.live_smoke import summarize_trace


def test_live_smoke_summary_passes_required_tool_and_cite_uid() -> None:
    scenario = {
        "name": "guideline",
        "required_tools": ["index_get_page_content"],
        "require_cite_uid": True,
    }
    trace = {
        "generation_mode": "remote:lunit_l2:harness",
        "l2_retrieval_phase_status": "sufficient",
        "l2_mcp_tool_call_count": 2,
        "l2_selected_cite_uids": ["cite-guideline-1"],
        "retrieval_results": [
            {
                "raw_tool_name": "index_get_page_content",
                "cite_uid": "cite-guideline-1",
            }
        ],
    }

    result = summarize_trace(scenario, trace)

    assert result["passed"] is True
    assert result["raw_tool_names"] == ["index_get_page_content"]
    assert result["l2_selected_cite_uids"] == ["cite-guideline-1"]


def test_live_smoke_summary_reports_missing_conditions() -> None:
    scenario = {
        "name": "hira_law",
        "required_tools": ["index_get_page_content"],
        "require_cite_uid": True,
    }
    trace = {
        "generation_mode": "fallback",
        "l2_retrieval_phase_status": "no_evidence",
        "l2_mcp_tool_call_count": 0,
        "retrieval_results": [{"raw_tool_name": "hira_updates_search"}],
    }

    result = summarize_trace(scenario, trace)

    assert result["passed"] is False
    assert "generation_mode_not_l2_harness" in result["errors"]
    assert "no_mcp_tool_calls" in result["errors"]
    assert "retrieval_status_no_evidence" in result["errors"]
    assert "missing_required_tool:index_get_page_content" in result["errors"]
    assert "missing_cite_uid" in result["errors"]
