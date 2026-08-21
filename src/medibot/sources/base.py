from typing import Protocol

from medibot.core.schemas import RetrievalEvidence


class MedicalSource(Protocol):
    name: str

    async def search(
        self, query: str, top_k: int, filters: dict | None = None
    ) -> list[RetrievalEvidence]:
        ...
