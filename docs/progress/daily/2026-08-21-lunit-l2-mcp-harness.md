# 2026-08-21 Lunit L2 MCP Harness Live Wiring

## Summary

Lunit L2를 일반 chat completion generator로만 쓰는 경로에서 벗어나, L2 사용 가이드에 맞춘
2-phase MCP tool 기반 RAG harness를 구현하고 live smoke까지 확인했다.

현재 상태는 end-to-end 구동 가능하지만, retrieval phase에서 L2가 `finalize_retrieval`을 안정적으로
호출하지 않아 harness budget fail-safe로 `partial` evidence를 넘기는 상태다. 다음 목표는 MCP page
content tool까지 안정적으로 유도해 `cite_uid` 기반 citation evidence를 확보하는 것이다.

## Added

- `LunitL2Harness`
  - Generation phase에는 `retrieve_relevant_content` tool만 노출
  - Retrieval phase에는 query별 Lunit MCP tool subset과 `finalize_retrieval`만 노출
  - `retrieve_relevant_content` 내부에서 retrieval phase를 실행하고 evidence를 generation phase로 반환
- `LunitMCPClient`
  - Streamable HTTP MCP `tools/list`, `tools/call` 지원
  - JSON response와 `text/event-stream` response 파싱
  - Bearer token 인증과 MCP protocol header 처리
- citation 중심 schema 확장
  - `RetrievalEvidence`: `cite_uid`, `source_url`, `corpus_tag`, `page_range`, `raw_tool_name`, `raw_result`
  - `TraceRecord`: `l2_retrieval_phase_status`, `l2_mcp_tool_call_count`, `l2_selected_cite_uids`, `l2_finalize_note`
- L2 provider wiring
  - `MEDIBOT_FINAL_MODEL_PROVIDER=lunit_l2`이면 `MedicalGenerator`가 `LunitL2Harness`를 사용
  - L2 provider에서는 기존 pre-generation `SourceRegistry` RAG를 우회
  - harness가 만든 evidence를 workflow의 `final_evidence`와 trace에 반영
- live MCP compatibility fix
  - Lunit MCP server는 `2026-07-28`을 지원하지 않음
  - `MEDIBOT_MCP_PROTOCOL_VERSION=2025-11-25`로 조정
- input limit 대응
  - retrieval query별 MCP tool subset만 노출
  - MCP tool description compact 처리
  - L2에 되먹이는 MCP tool result text truncation
- retrieval fail-safe
  - L2가 `finalize_retrieval`을 호출하지 않으면 tool budget에서 phase 종료
  - `cite_uid`가 없더라도 MCP raw result를 `partial` evidence로 generation phase에 전달

## Live Verification

환경:

```text
LUNIT_FM_API_URL=https://model.hackathon.lunit.io
LUNIT_FM_MODEL=Lunit/L2-preview
MEDIBOT_FINAL_MODEL_PROVIDER=lunit_l2
MEDIBOT_REQUIRE_L2_FINAL=1
MEDIBOT_ALLOW_FALLBACK=0
MEDIBOT_MCP_URL=https://mcp.hackathon.lunit.io/mcp
MEDIBOT_MCP_PROTOCOL_VERSION=2025-11-25
```

확인 결과:

- Lunit Model API: `200 OK`
- Lunit MCP `tools/list`: `21` tools 확인
- 대표 MCP tools:
  - `rag_get_all_data_sources`
  - `rag_get_data_source_detail`
  - `rag_sql_query`
  - `rag_vector_query`
  - `adr_retrieve_drug_info`
- MediBot live pipeline smoke:
  - `generation_mode=remote:lunit_l2:harness`
  - `triage_class=NON_EMERGENT`
  - `retrieval_used=True`
  - `safety_status=pass`
  - `l2_retrieval_phase_status=partial`
  - `l2_mcp_tool_call_count=10`
  - `l2_selected_cite_uids=[]`
  - `l2_finalize_note=retrieval ended by harness after tool budget or missing finalize_retrieval`

## Evaluation

```text
pytest
26 passed
```

추가 targeted checks:

- `tests/unit/test_lunit_l2_harness.py`
  - generation phase가 `retrieve_relevant_content`만 노출하는지 검증
  - retrieval phase가 MCP tools + `finalize_retrieval`만 노출하는지 검증
  - MCP SSE response parsing 검증
  - fake L2/MCP 2-phase flow에서 `cite_uid` 보존 검증
- `tests/integration/test_workflow_smoke.py::test_lunit_l2_workflow_uses_mcp_harness_and_writes_trace`
  - workflow trace에 L2 retrieval phase status, MCP tool call count, selected cite uid, finalize note 기록 검증

수정 파일 대상 ruff check는 통과했다. 전체 `ruff check src tests`는 기존 미사용 인자/import 정렬/SIM114 이슈가 남아 별도 정리 대상이다.

## Current Limitations

- L2 retrieval phase가 live 환경에서 `finalize_retrieval`을 안정적으로 호출하지 않는다.
- live guideline smoke에서는 `cite_uid` 기반 evidence selection이 아직 발생하지 않았다.
- 현재 live RAG는 MCP raw result 기반 `partial` evidence를 generation phase에 넘겨 최종 답변까지 생성하는 상태다.
- MCP tool budget 기본값이 `10`이라 live smoke latency가 30초를 넘을 수 있다.
- L2 응답에는 reasoning field가 함께 오므로, 최종 사용자 응답과 trace 저장 시 content 중심으로만 사용한다.

## Next Work

1. Retrieval prompt를 더 강하게 조정해 `finalize_retrieval` 호출을 유도한다.
2. guideline/HIRA flow에서 `index_get_relevant_nodes` 이후 `index_get_page_content`까지 좁히는 tool-use trajectory를 안정화한다.
3. MCP result에서 nested `cite_uid` 추출 coverage를 확대한다.
4. source별 tool subset과 tool budget을 query intent별로 튜닝한다.
5. `healthbench_consensus` 소량 live evaluation으로 `mini` baseline과 L2 MCP harness를 비교한다.
