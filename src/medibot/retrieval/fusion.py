from medibot.core.schemas import RetrievalEvidence


class EvidenceFusion:
    def fuse(
        self, vector_evidence: list[RetrievalEvidence], graph_evidence: list[RetrievalEvidence] | None = None
    ) -> list[RetrievalEvidence]:
        by_id: dict[str, RetrievalEvidence] = {}
        for item in [*vector_evidence, *(graph_evidence or [])]:
            existing = by_id.get(item.id)
            if existing is None or item.total_score > existing.total_score:
                by_id[item.id] = item
        return sorted(by_id.values(), key=lambda item: item.total_score, reverse=True)
