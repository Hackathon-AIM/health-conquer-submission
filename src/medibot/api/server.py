import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, Field

from medibot.core.schemas import ChatMessage, MedibotRequest, MedibotResponse
from medibot.orchestrator.workflow import MedibotWorkflow

app = FastAPI(title="MediBot P0", version="0.1.0")
workflow = MedibotWorkflow()


class OpenAIChatMessage(BaseModel):
    role: str
    content: Any


class OpenAIChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[OpenAIChatMessage]
    temperature: float | None = None
    stream: bool = False
    user: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _to_medibot_role(role: str) -> Literal["system", "user", "assistant"]:
    if role in {"system", "developer"}:
        return "system"
    if role == "assistant":
        return "assistant"
    return "user"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text.split()))


@app.post("/chat", response_model=MedibotResponse)
async def chat(request: MedibotRequest) -> MedibotResponse:
    return await workflow.handle(request)


@app.get("/v1/models")
async def list_models() -> dict[str, object]:
    settings = workflow.settings
    return {
        "object": "list",
        "data": [
            {
                "id": settings.model_name,
                "object": "model",
                "created": 0,
                "owned_by": "lunit",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def create_chat_completion(
    request: OpenAIChatCompletionRequest,
) -> dict[str, object]:
    messages = [
        ChatMessage(
            role=_to_medibot_role(message.role),
            content=_content_to_text(message.content),
        )
        for message in request.messages
    ]
    medibot_response = await workflow.handle(
        MedibotRequest(
            session_id=request.user,
            messages=messages,
            metadata={
                **request.metadata,
                "openai_model": request.model,
                "openai_stream_requested": request.stream,
                "openai_temperature": request.temperature,
            },
        )
    )
    model = request.model or workflow.settings.model_name
    prompt_tokens = sum(_estimate_tokens(message.content) for message in messages)
    completion_tokens = _estimate_tokens(medibot_response.answer)
    return {
        "id": f"chatcmpl-{uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": medibot_response.answer,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "medibot": {
            "trace_id": medibot_response.trace_id,
            "triage_class": medibot_response.triage_class,
            "retrieval_used": medibot_response.retrieval_used,
            "evidence_ids": medibot_response.evidence_ids,
            "safety_status": medibot_response.safety_status,
        },
    }


@app.get("/debug/config")
async def debug_config() -> dict[str, object]:
    settings = workflow.settings
    return {
        "has_model_endpoint": settings.has_model_endpoint,
        "api_base": settings.model_api_base,
        "model": settings.model_name,
        "has_api_key": bool(settings.model_api_key),
        "final_model_provider": settings.final_model_provider,
        "require_l2_final": settings.require_l2_final,
        "allow_fallback": settings.allow_fallback,
        "mcp_url": settings.mcp_url,
        "mcp_protocol_version": settings.mcp_protocol_version,
        "l2_generation_tool_budget": settings.l2_generation_tool_budget,
        "l2_retrieval_tool_budget": settings.l2_retrieval_tool_budget,
        "l2_tool_result_char_limit": settings.l2_tool_result_char_limit,
        "trace_content_char_limit": settings.trace_content_char_limit,
        "rag_backend": settings.rag_backend,
        "enabled_mcp_sources": settings.enabled_mcp_sources,
        "trace_path": str(settings.trace_path),
    }
