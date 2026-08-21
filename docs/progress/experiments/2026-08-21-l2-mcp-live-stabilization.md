# L2/MCP Live Stabilization

Date: 2026-08-21

## Scope

This iteration stabilizes the L2 retrieval phase with prompt-level guidance only.
The harness does not synthesize `index_get_page_content` or `finalize_retrieval`
calls on behalf of L2.

## Commands

Targeted tests:

```powershell
python -m pytest tests\unit\test_lunit_l2_harness.py tests\unit\test_live_smoke.py tests\integration\test_workflow_smoke.py -q
```

Source-level L2/MCP live smoke:

```powershell
python -m medibot.evaluation.live_smoke --scenario-set l2_mcp_sources --output storage/evaluation_runs/l2_mcp_live_smoke.json
```

CoEval 5-sample mini vs L2/MCP comparison:

```powershell
python scripts\run_coeval_healthbench_compare.py --output storage/evaluation_runs/coeval_healthbench_compare.json
```

## Acceptance

- `generation_mode=remote:lunit_l2:harness`
- MCP tool call count is greater than zero for every scenario
- `guideline` reaches `index_get_page_content` and returns at least one `cite_uid`
- `hira_law` reaches `index_get_page_content` and returns at least one `cite_uid`
- `medication` reaches a medication evidence tool
- `pubmed` reaches `rag_vector_query`
- CoEval runs `healthbench_consensus num_samples=5` once with mini RAG baseline and once with L2/MCP harness

## Results

Pending live execution.
