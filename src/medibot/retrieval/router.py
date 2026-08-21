from medibot.core.schemas import QueryAnalysis


class DynamicSourceRouter:
    def route(self, analysis: QueryAnalysis, source_queries: dict[str, list[str]]) -> dict[str, int]:
        if analysis.retrieval_need == "skip":
            return {}

        quotas: dict[str, int] = {}
        for source in source_queries:
            if source == "drug" and analysis.intent == "medication_safety":
                quotas[source] = 5
            elif source == "faers" and analysis.intent == "medication_safety":
                quotas[source] = 3
            elif source == "disease_code" and analysis.intent == "disease_code_lookup":
                quotas[source] = 4
            elif source == "guideline":
                quotas[source] = 4
            elif source == "pubmed" and analysis.risk_class in {"medium", "high"}:
                quotas[source] = 3
            elif source in {"hira", "insurance_law", "drug_pricing"} and analysis.jurisdiction == "KR":
                quotas[source] = 3

        return quotas
