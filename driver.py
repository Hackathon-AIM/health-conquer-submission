"""제출용 대화 드라이버 — FastAPI/uvicorn 골격 위의 우리 파이프라인.

★ 왜 serve.py(stdlib http.server) 를 두고 이걸 따로 만들었나
  같은 저장소의 통과본(4c8cdc6, 29.13점)은 FastAPI + uvicorn 이고,
  우리 stdlib 서버는 세 번 연속 CoEval 기동 단계에서 실패했다
  (docker start --attach → exit 1). 로그를 볼 수 없어 원인을 특정하지 못했으므로,
  **통과가 확인된 골격과의 차이를 전부 제거**한다.

  stdlib 서버와 다른 점 (이 중 하나가 원인일 수 있다):
    · HTTP/1.1 keep-alive 처리 — CoEval 은 concurrent_limit=10 으로 동시에 때린다.
      stdlib ThreadingHTTPServer 는 스레드당 연결이고, 응답 전 예외가 나면 연결이 매달린다.
    · 응답에 비표준 필드(medai_trace)를 얹고 있었다 — 기본은 끈다.
    · uvicorn 은 SIGTERM·백프레셔·타임아웃 처리가 검증돼 있다.

  serve.py 는 로컬 개발용으로 남긴다. 제출 CMD 는 이 파일을 쓴다.

실행
    uvicorn driver:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("driver")

MODEL_NAME = os.environ.get("MEDAI_MODEL_NAME", "medai")
CONFIG = (os.environ.get("MEDAI_CONFIG") or "").strip() or "configs/l2_live.yaml"
# 비표준 필드는 기본으로 끈다 — 평가 클라이언트가 무엇을 싫어할지 모른다.
EXPOSE_TRACE = os.environ.get("MEDAI_TRACE", "0") == "1"

# 요청 하나가 FM 을 여러 번 부른다. 평가자가 10개를 동시에 보내면 상류가 먼저 무너지므로
# 우리 쪽에서 조인다 (통과본도 같은 방식: FM_CONCURRENCY / MCP_CONCURRENCY).
TURN_CONCURRENCY = int((os.environ.get("TURN_CONCURRENCY") or "8").strip() or 8)
TURN_TIMEOUT_S = float((os.environ.get("TURN_TIMEOUT_S") or "90").strip() or 90)

FALLBACK = ("죄송합니다. 일시적인 오류로 정확한 안내를 드리지 못했습니다. "
            "증상이 지속되거나 악화되면 가까운 의료기관에서 진료를 받아보세요.")

_PIPE: Any = None
_INIT_ERROR = ""
_SEM: asyncio.Semaphore | None = None
_REQ_N = 0


def _diagnose() -> None:
    """평가 환경을 stdout 에 남긴다 — 이 로그가 우리의 유일한 디버거다.

    ⚠️ 키 값은 절대 찍지 않는다. 이름과 길이만.
    """
    import re
    import urllib.error
    import urllib.request

    names = sorted(k for k in os.environ
                   if re.search(r"KEY|TOKEN|SECRET|API|LUNIT|OPENAI|MCP", k, re.I))
    log.info("[진단] 환경변수: %s",
             ", ".join(f"{k}({len(os.environ[k])})" for k in names[:15]) or "(없음)")
    try:
        from medai import config as cfgmod
        from medai.llm import _resolve_api_key, _resolve_base_url
        lc = cfgmod.load(CONFIG)["llm"]
        key = _resolve_api_key(lc)
        log.info("[진단] 키: %s", "없음" if key == "sk-noauth" else f"있음({len(key)}자)")
        targets = [("model", _resolve_base_url(lc).rstrip("/") + "/models"),
                   ("mcp", "https://mcp.hackathon.lunit.io/mcp")]
        for label, url in targets:
            try:
                req = urllib.request.Request(url)
                if key != "sk-noauth":
                    req.add_header("Authorization", f"Bearer {key}")
                with urllib.request.urlopen(req, timeout=5) as r:
                    log.info("[진단] %s → %s 연결됨", label, r.status)
            except urllib.error.HTTPError as e:
                log.info("[진단] %s → %s (네트워크는 열림)", label, e.code)
            except Exception as e:
                log.error("[진단] %s → 도달 불가: %s", label, type(e).__name__)
    except Exception as e:
        log.error("[진단] 실패: %r", e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _PIPE, _INIT_ERROR, _SEM
    _SEM = asyncio.Semaphore(TURN_CONCURRENCY)
    # ★ 초기화가 실패해도 서버는 뜬다. 컨테이너가 죽으면 제출물 전체가 0점이다.
    try:
        from medai import config as cfgmod
        from medai.pipeline import Pipeline
        _PIPE = Pipeline(cfgmod.load(CONFIG))
        log.info("파이프라인 준비 완료 — config=%s", CONFIG)
    except Exception as e:
        _INIT_ERROR = f"{type(e).__name__}: {e}"
        log.error("⚠️ 파이프라인 초기화 실패 — 축소 모드: %s", _INIT_ERROR)
        traceback.print_exc()
    _diagnose()
    log.info("driver up · model=%s · 동시 %d · 턴 타임아웃 %.0fs",
             MODEL_NAME, TURN_CONCURRENCY, TURN_TIMEOUT_S)
    yield


app = FastAPI(title="med_ai conversation driver", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "pipeline": _PIPE is not None, "error": _INIT_ERROR or None}


@app.get("/v1/models")
async def list_models() -> dict:
    return {"object": "list",
            "data": [{"id": MODEL_NAME, "object": "model",
                      "created": 0, "owned_by": "team"}]}


@app.post("/v1/chat/completions")
async def chat_completions(body: dict) -> Any:
    global _REQ_N
    _REQ_N += 1
    n = _REQ_N
    messages = body.get("messages") or []
    if not messages:
        return JSONResponse(status_code=400,
                            content={"error": {"message": "messages required"}})

    t0 = time.perf_counter()
    answer, trace = FALLBACK, {}
    try:
        if _PIPE is None:
            raise RuntimeError(f"pipeline unavailable: {_INIT_ERROR}")
        from serve import session_from_messages   # 무상태 세션 복원은 공유한다
        session, text = session_from_messages(messages)
        assert _SEM is not None
        async with _SEM:
            res = await asyncio.wait_for(_PIPE.run_turn(text, session),
                                         timeout=TURN_TIMEOUT_S)
        answer, trace = res.answer, res.trace
    except asyncio.TimeoutError:
        trace = {"error": f"turn timeout {TURN_TIMEOUT_S}s"}
        log.error("[요청 #%d] 턴 타임아웃", n)
    except Exception as e:
        trace = {"error": repr(e)}
        log.error("[요청 #%d] 실패: %r", n, e)

    secs = time.perf_counter() - t0
    r = (trace.get("l2") or {}).get("retrieval") or {}
    log.info("[요청 #%d] %d턴 · %.1fs · %d자%s", n, len(messages), secs, len(answer),
             f" · 검색 {r.get('tool_calls')}회/{r.get('status')}" if r else "")

    out = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model") or MODEL_NAME,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": answer},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    if EXPOSE_TRACE:
        out["medai_trace"] = trace
    return out
