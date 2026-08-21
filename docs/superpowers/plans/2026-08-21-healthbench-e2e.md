# HealthBench E2E 검사기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 합성 영어·한국어 대화와 실제 MCP 문서 왕복을 점검하는 제출 서비스 E2E 검사기를 만든다.

**Architecture:** 표준 라이브러리 HTTP 클라이언트가 제출 서비스와 MCP를 각각 호출한다. 테스트는 로컬 HTTP 서버로 서비스 응답과 MCP의 SSE/JSON-RPC 결과를 흉내 내어 네트워크 없이 판정 로직을 고정한다.

**Tech Stack:** Python 3.13 표준 라이브러리, `unittest`, `http.server`.

---

### Task 1: 검사 케이스와 판정 규칙

**Files:**
- Create: `tests/test_healthbench_e2e.py`
- Create: `scripts/healthbench_e2e.py`

- [ ] **Step 1: Write the failing test**

```python
def test_default_scenarios_cover_both_languages_and_emergencies():
    scenarios = default_scenarios()
    assert {s.name for s in scenarios} == {
        "en_multiturn", "ko_multiturn", "en_emergency", "ko_emergency",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_healthbench_e2e.py -v`

Expected: FAIL because `scripts.healthbench_e2e` does not exist.

- [ ] **Step 3: Write minimal implementation**

Add immutable scenario records and language/emergency response predicates.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_healthbench_e2e.py -v`

Expected: PASS.

### Task 2: OpenAI-compatible service client

**Files:**
- Modify: `tests/test_healthbench_e2e.py`
- Modify: `scripts/healthbench_e2e.py`

- [ ] **Step 1: Write the failing test**

Use a local `ThreadingHTTPServer` and assert that the runner sends the complete multi-turn message list and records a failing case when the response is empty.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_healthbench_e2e.py -v`

Expected: FAIL because the HTTP client and result aggregation do not exist.

- [ ] **Step 3: Write minimal implementation**

Use `urllib.request` for the health, model, and chat requests. Return only check names, status, response length, and latency in reports.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_healthbench_e2e.py -v`

Expected: PASS.

### Task 3: MCP document round trip

**Files:**
- Modify: `tests/test_healthbench_e2e.py`
- Modify: `scripts/healthbench_e2e.py`

- [ ] **Step 1: Write the failing test**

Use a fake JSON-RPC MCP server that returns a document node and page content with `cite_uid`; assert `run_mcp_document_check` executes `tools/list`, node search, and page fetch in order.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_healthbench_e2e.py -v`

Expected: FAIL because MCP client support does not exist.

- [ ] **Step 3: Write minimal implementation**

Parse JSON or one-line SSE responses, validate the required index tools, and inspect only metadata/text presence without writing source content.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_healthbench_e2e.py -v`

Expected: PASS.

### Task 4: Command-line interface and verification

**Files:**
- Modify: `scripts/healthbench_e2e.py`
- Modify: `README.md`

- [ ] **Step 1: Write the failing test**

Assert `--skip-mcp` bypasses MCP and that any failed check maps to exit code 1.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_healthbench_e2e.py -v`

Expected: FAIL because command configuration has no skip behavior.

- [ ] **Step 3: Write minimal implementation**

Add `--base-url`, `--mcp-url`, `--skip-mcp`, `--timeout`, and a concise README invocation.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_healthbench_e2e.py -v`

Expected: PASS, then run the submission test suite and a local Docker service smoke test.
