from medibot.core.schemas import RetrievalEvidence


class Reranker:
    def rank(self, evidence: list[RetrievalEvidence]) -> list[RetrievalEvidence]:
        return sorted(evidence, key=lambda item: item.total_score, reverse=True)
