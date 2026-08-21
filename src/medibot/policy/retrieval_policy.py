from medibot.core.schemas import QueryAnalysis


class RetrievalPolicy:
    def is_required(self, analysis: QueryAnalysis) -> bool:
        return analysis.retrieval_need == "required"

    def should_retrieve(self, analysis: QueryAnalysis) -> bool:
        return analysis.retrieval_need in {"required", "optional"}
