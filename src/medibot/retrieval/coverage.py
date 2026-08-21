from medibot.core.schemas import QueryAnalysis, RetrievalEvidence


class CoverageCheck:
    def check(
        self, analysis: QueryAnalysis, evidence: list[RetrievalEvidence]
    ) -> dict[str, object]:
        required: list[str] = []
        if analysis.intent == "medication_safety":
            required.extend(["interaction", "safe_next_action"])
            if "hypertension" in analysis.diseases:
                required.append("blood_pressure_effect")

        supports = {support for item in evidence for support in item.supports}
        missing = [item for item in required if item not in supports]
        return {
            "required": required,
            "covered": sorted(set(required).intersection(supports)),
            "missing": missing,
            "sufficient": not missing,
        }
