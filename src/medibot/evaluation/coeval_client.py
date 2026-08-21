from typing import Any

from medibot.core.schemas import ChatMessage, MedibotRequest
from medibot.orchestrator.workflow import MedibotWorkflow


class MediBotCoEvalClient:
    """CoEval-compatible messages -> answer inference client."""

    def __init__(self, workflow: MedibotWorkflow | None = None) -> None:
        self.workflow = workflow or MedibotWorkflow()

    @property
    def name(self) -> str:
        return self.__class__.__name__

    async def generate(self, messages: list[dict[str, Any]]) -> str:
        request_messages = [
            ChatMessage(role=message["role"], content=str(message["content"]))
            for message in messages
            if message.get("role") in {"system", "user", "assistant"}
        ]
        response = await self.workflow.handle(MedibotRequest(messages=request_messages))
        return response.answer
