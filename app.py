"""팀 AIM의 OpenAI 호환 멀티턴 대화 드라이버."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from submission.config import SETTINGS
from submission.orchestrator import NativeDriver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("driver")

app = FastAPI(title="AIM conversation driver")
driver = NativeDriver(SETTINGS)
DEMO_HTML = Path(__file__).with_name("static").joinpath("demo.html")


@app.on_event("startup")
async def announce() -> None:
    if not SETTINGS.fm_api_key:
        log.error("LUNIT_FM_API_KEY 가 비어 있다. 외부 모델 요청은 안전한 fallback으로 응답한다.")
    log.info(
        "driver up — model=%s max_tokens=%d thinking=%s",
        SETTINGS.fm_model,
        SETTINGS.max_tokens,
        SETTINGS.enable_thinking,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "model": SETTINGS.fm_model, "key_present": bool(SETTINGS.fm_api_key)}


@app.get("/", response_class=HTMLResponse)
async def demo() -> HTMLResponse:
    return HTMLResponse(DEMO_HTML.read_text(encoding="utf-8"))


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": SETTINGS.public_model_name, "object": "model", "owned_by": "team-aim"}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    try:
        # urllib 기반 Model/MCP 호출을 event loop 밖으로 보내 동시 요청을 막지 않는다.
        content = await asyncio.to_thread(driver.answer, messages)
    except Exception:
        log.exception("예상하지 못한 생성 실패")
        content = NativeDriver._fallback()
    return {
        "id": "chatcmpl-aim",
        "object": "chat.completion",
        "model": SETTINGS.public_model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }
