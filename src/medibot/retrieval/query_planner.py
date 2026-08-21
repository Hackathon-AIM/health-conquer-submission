from medibot.core.schemas import QueryAnalysis


class SourceQueryPlanner:
    def plan(self, message: str, analysis: QueryAnalysis) -> dict[str, list[str]]:
        queries: dict[str, list[str]] = {
            "drug": [],
            "drug_pricing": [],
            "disease_code": [],
            "guideline": [],
            "pubmed": [],
            "faers": [],
            "hira": [],
            "insurance_law": [],
        }
        if analysis.intent == "medication_safety":
            drug_terms = " ".join(analysis.drugs) or message
            disease_terms = " ".join(analysis.diseases)
            queries["drug"].append(f"{drug_terms} {disease_terms} interaction safety")
            queries["guideline"].append(f"{drug_terms} {disease_terms} medication safety")
            queries["pubmed"].append(f"{drug_terms} blood pressure interaction review")
            queries["faers"].append(f"{drug_terms} adverse event safety signal")
        elif analysis.intent == "disease_code_lookup":
            queries["disease_code"].append(message)
            if analysis.jurisdiction == "KR":
                queries["hira"].append(message)
        elif analysis.retrieval_need == "required":
            queries["guideline"].append(message)
            if analysis.jurisdiction == "KR":
                queries["hira"].append(message)
                queries["insurance_law"].append(message)
                queries["drug_pricing"].append(message)
        elif analysis.retrieval_need == "optional":
            queries["guideline"].append(message)
        return {source: values for source, values in queries.items() if values}
