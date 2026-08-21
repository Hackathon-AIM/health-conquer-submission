"""시간 예산 가드 회귀 테스트 — 컨테이너 안에서 pytest 없이도 돈다.

    python tests/test_budget_guard.py      # 평문 실행
    pytest tests/                          # pytest 가 있으면 그대로 수집된다

여기서 지키려는 것은 하나다. **요청 하나가 자기 시간 예산을 넘지 않는다.**
넘으면 평가 클라이언트가 먼저 끊고, 그 문항은 답이 나빠서가 아니라 도달하지
못해서 0점이 된다 — 실측으로 본 실패 방식이다.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

import mcp_client  # noqa: E402
from budget import MIN_CALL_S, Deadline, call_cap  # noqa: E402
from retrieval import run_retrieval  # noqa: E402


# ── 가짜들 ────────────────────────────────────────────────────
class FakeMCP:
    """list_tools / call_tool 만 흉내 낸다. 호출 기록을 남긴다."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict, float | None]] = []
        self.delay = delay

    async def list_tools(self, timeout: float | None = None) -> list[dict]:
        self.list_timeout = timeout
        return [
            {
                "name": "kcd_get_name",
                "description": "KCD code -> name",
                "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}},
            }
        ]

    async def call_tool(self, name: str, arguments: dict, timeout: float | None = None):
        self.calls.append((name, arguments, timeout))
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"cite_uid": f"cite-{len(self.calls):016x}", "text": "본문", "title": "t"}


def _tool_call(idx: int, name: str = "kcd_get_name", args: dict | None = None) -> dict:
    return {
        "id": f"call_{idx}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {"code": f"A0{idx}"})},
    }


def _msg(tool_calls=None, content: str = "") -> dict:
    return {"choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}]}


class ScriptedFM:
    """미리 정해둔 응답을 순서대로 돌려주고, 받은 messages 를 기록한다."""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.seen: list[list[dict]] = []
        self.timeouts: list[float | None] = []

    async def __call__(self, messages, max_tokens, extra=None, timeout=None, **kw):
        self.seen.append([dict(m) for m in messages])
        self.timeouts.append(timeout)
        if not self.script:
            return _msg(content="끝")
        return self.script.pop(0)


# ── 1. call_cap 산수 ──────────────────────────────────────────
def test_call_cap_arithmetic() -> None:
    dl = Deadline.start(40.0)
    # 답변+검증 몫 26초를 떼면 14초가 남는다 — cap(25)보다 작으므로 14가 이긴다.
    assert 13.0 < call_cap(dl, 25.0, reserve=26.0) <= 14.0
    # reserve 가 없으면 cap 이 이긴다.
    assert call_cap(dl, 25.0) == 25.0
    # deadline 이 없으면 호출자 기본값을 쓰라는 뜻.
    assert call_cap(None, 25.0) is None
    # 거의 다 쓴 예산이라도 floor 밑으로는 안 준다 — 부르기로 했으면 기회는 준다.
    spent = Deadline(total=40.0, started=time.monotonic() - 39.5)
    assert call_cap(spent, 25.0) == MIN_CALL_S


# ── 2. 예산은 '도구 호출 수' 여야 한다 ─────────────────────────
def test_budget_counts_tool_calls_not_fm_steps() -> None:
    """한 스텝에 도구를 3개 부르면 예산 2 는 2개만 허용해야 한다.

    예전 코드는 `for _ in range(budget)` 이라 budget 이 FM 왕복 수였고,
    모델이 한 번에 도구를 여러 개 부르면 예산이 몇 배로 샜다.
    """
    fm = ScriptedFM([_msg([_tool_call(1), _tool_call(2), _tool_call(3)])])
    mcp = FakeMCP()
    res = asyncio.run(run_retrieval("질문", ["kcd_get_name"], fm, mcp, budget=2))
    assert len(mcp.calls) == 2, f"도구가 {len(mcp.calls)}번 불렸다 — 2번이어야 한다"
    assert res.tool_calls_used == 2
    # 예산을 넘긴 세 번째는 실행되지 않았고 그 사실이 흔적에 남는다.
    assert any("skipped" in t for t in res.trace), res.trace


# ── 3. tool_call 하나에 tool 응답 하나 ────────────────────────
def test_every_tool_call_gets_a_response() -> None:
    """짝이 안 맞으면 다음 스텝 요청이 400 이다. 두 번째 스텝이 실제로 보는 것을 확인한다."""
    finalize = {
        "id": "call_f",
        "type": "function",
        "function": {
            "name": "finalize_retrieval",
            "arguments": json.dumps({"status": "sufficient", "items": [], "note": ""}),
        },
    }
    fm = ScriptedFM([_msg([_tool_call(1)]), _msg([finalize])])
    mcp = FakeMCP()
    asyncio.run(run_retrieval("질문", ["kcd_get_name"], fm, mcp, budget=3))

    assert len(fm.seen) == 2, "두 번째 스텝까지 가야 검사가 의미 있다"
    second = fm.seen[1]
    ids = {c["id"] for m in second if m.get("role") == "assistant" for c in m.get("tool_calls", [])}
    responded = {m.get("tool_call_id") for m in second if m.get("role") == "tool"}
    assert ids and ids == responded, f"짝이 안 맞는다: 요청 {ids} vs 응답 {responded}"


# ── 4. 시간이 없으면 도구를 아예 안 부른다 ─────────────────────
def test_retrieval_yields_to_deadline() -> None:
    """답 쓸 시간(reserve)까지 파먹으면서 검색하지 않는다."""
    fm = ScriptedFM([_msg([_tool_call(1)])])
    mcp = FakeMCP()
    # 40초 예산 중 39초를 이미 썼다. reserve 26초는 이미 침범 상태다.
    dl = Deadline(total=40.0, started=time.monotonic() - 39.0)
    res = asyncio.run(
        run_retrieval("질문", ["kcd_get_name"], fm, mcp, budget=3, deadline=dl, reserve=26.0)
    )
    assert mcp.calls == [], "시간이 없는데 도구를 불렀다"
    assert fm.seen == [], "시간이 없는데 FM 스텝을 돌았다"
    assert "time budget" in res.note


# ── 5. 도구별 timeout 이 남은 시간에서 나온다 ──────────────────
def test_tool_timeout_derives_from_remaining_time() -> None:
    fm = ScriptedFM([_msg([_tool_call(1)])])
    mcp = FakeMCP()
    dl = Deadline.start(40.0)
    asyncio.run(
        run_retrieval("질문", ["kcd_get_name"], fm, mcp, budget=1, deadline=dl, reserve=26.0)
    )
    assert mcp.calls, "도구가 안 불렸다"
    # 도구 목록 조회에도 상한이 걸려야 한다 — MCP 가 죽으면 여기서 180초를 태웠다.
    assert mcp.list_timeout is not None and mcp.list_timeout <= 14.0, mcp.list_timeout
    _, _, timeout = mcp.calls[0]
    # 남은 14초 안쪽이어야 한다. 예전에는 MCP_TIMEOUT(60초)이 그대로 걸렸다.
    assert timeout is not None and timeout <= 14.0, timeout
    assert timeout >= MIN_CALL_S
    # FM 스텝에도 같은 원리로 상한이 걸린다.
    assert fm.timeouts[0] is not None and fm.timeouts[0] <= 14.0


# ── 6. 재시도가 총 상한을 넘지 않는다 ──────────────────────────
class HangingClient:
    """항상 타임아웃하는 가짜 httpx 클라이언트."""

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self.posts = 0

    async def post(self, *a, **kw):
        self.posts += 1
        await asyncio.sleep(self.delay)
        raise httpx.TimeoutException("가짜 타임아웃")

    async def aclose(self) -> None:
        return None


def test_mcp_retry_respects_total_timeout() -> None:
    c = mcp_client.MCPClient(url="http://x/mcp", key="k", concurrency=2)
    c._client = HangingClient()  # type: ignore[assignment]
    started = time.monotonic()
    try:
        asyncio.run(c.call_tool("kcd_get_name", {"code": "A00"}, timeout=1.0))
        raise AssertionError("예외가 나야 한다")
    except (httpx.TimeoutException, TimeoutError):
        pass
    elapsed = time.monotonic() - started
    # 백오프(1.5초)를 기다릴 시간이 없으므로 즉시 포기해야 한다.
    assert elapsed < 1.0, f"{elapsed:.2f}s 걸렸다 — 상한 1.0s 를 넘겼다"


# ── 7. 캐시가 두 번째 호출을 없앤다 ────────────────────────────
def test_mcp_cache_hit_skips_upstream() -> None:
    """멀티턴에서 같은 검색이 반복된다. 켜면 두 번째부터는 상류에 안 나간다."""

    class CountingClient(HangingClient):
        def __init__(self) -> None:
            super().__init__()
            self.posts = 0

        async def post(self, *a, **kw):
            self.posts += 1
            req = httpx.Request("POST", "http://x/mcp")
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"text": json.dumps({"cite_uid": "cite-1"})}]},
            }
            return httpx.Response(200, text="data: " + json.dumps(body), request=req)

    old = mcp_client.MCP_CACHE_TTL_S
    mcp_client.MCP_CACHE_TTL_S = 300.0
    try:
        c = mcp_client.MCPClient(url="http://x/mcp", key="k")
        fake = CountingClient()
        c._client = fake  # type: ignore[assignment]

        async def _run():
            a = await c.call_tool("kcd_get_name", {"code": "A00"})
            b = await c.call_tool("kcd_get_name", {"code": "A00"})
            d = await c.call_tool("kcd_get_name", {"code": "B99"})
            return a, b, d

        a, b, d = asyncio.run(_run())
        assert a == b == d == {"cite_uid": "cite-1"}
        assert fake.posts == 2, f"상류 호출 {fake.posts}번 — 같은 인자는 1번이어야 한다"
        assert c.cache_hits == 1
    finally:
        mcp_client.MCP_CACHE_TTL_S = old


def test_mcp_cache_off_by_default() -> None:
    """기본값은 꺼짐이다 — 켜는 것은 A/B 로 재고 나서 한다."""
    assert float(os.environ.get("MCP_CACHE_TTL_S", "0")) == 0.0


# ── 8. 답변 호출은 남은 시간으로 굶기지 않는다 ────────────────
def test_answer_timeout_never_starves() -> None:
    from budget import answer_timeout

    spent = Deadline(total=40.0, started=time.monotonic() - 39.0)
    # 남은 1초여도 답변에는 floor 만큼 준다 — 여기서 깎으면 빈 답이 되고 그게 0점이다.
    assert answer_timeout(spent, 45.0, 25.0) == 25.0
    fresh = Deadline.start(40.0)
    # 남은 만큼 준다(cap 안쪽). start 직후라도 remaining 은 40 에서 미세하게 줄어 있다.
    assert 39.5 < answer_timeout(fresh, 45.0, 25.0) <= 40.0
    plenty = Deadline.start(120.0)
    assert answer_timeout(plenty, 45.0, 25.0) == 45.0  # cap 이 이긴다
    assert answer_timeout(None, 45.0, 25.0) is None


# ── 9. 어떤 예외든 구조 경로를 탄다 ────────────────────────────
def test_rescue_covers_non_timeout_errors() -> None:
    """httpx.ReadTimeout 은 TimeoutError 가 아니다.

    예전에는 `except asyncio.TimeoutError` 만 되살리고 나머지는 빈 문자열로
    내려보냈다. 실측에서 동시 30건 중 6건이 정확히 이 문으로 빠져 0점이 됐다.
    """
    import app as appmod

    async def boom(messages, dl):
        raise httpx.ReadTimeout("상류가 늦다")

    async def canned(messages, max_tokens, extra=None, timeout=None, **kw):
        return {"choices": [{"message": {"content": "되살린 답변입니다."}}]}

    old_gen, old_fm = appmod.generate_reply, appmod.call_fm
    appmod.generate_reply, appmod.call_fm = boom, canned
    try:
        out = asyncio.run(appmod.chat_completions({"messages": [{"role": "user", "content": "q"}]}))
    finally:
        appmod.generate_reply, appmod.call_fm = old_gen, old_fm

    got = out["choices"][0]["message"]["content"]
    assert got == "되살린 답변입니다.", f"구조 경로가 안 돌았다: {got!r}"


# ── 러너 ──────────────────────────────────────────────────────
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
