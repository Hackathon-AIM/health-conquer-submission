from medibot.core.schemas import RetrievalEvidence


class EvidenceFilter:
    def filter(
        self, evidence: list[RetrievalEvidence], limit: int
    ) -> list[RetrievalEvidence]:
        seen: set[str] = set()
        filtered: list[RetrievalEvidence] = []
        for item in evidence:
            if item.id in seen:
                continue
            if item.relevance_score < 0.2:
                continue
            seen.add(item.id)
            filtered.append(item)
            if len(filtered) >= limit:
                break
        return filtered
