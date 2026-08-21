from medibot.core.schemas import QueryAnalysis, ResponseRequirements, TriageClass


class ResponseRequirementPlanner:
    def plan(
        self, analysis: QueryAnalysis, triage_class: TriageClass
    ) -> ResponseRequirements:
        must_include = ["direct_answer", "safe_next_action"]
        must_avoid = ["overdiagnosis", "unsupported_dosage"]
        if triage_class != "NON_EMERGENT":
            must_include.extend(["red_flags", "care_escalation"])
        if analysis.missing_information:
            must_include.append("useful_missing_context")
        if analysis.intent == "medication_safety":
            must_include.extend(["interaction", "warning_signs"])
            must_avoid.append("unsupported_interaction")
        if analysis.intent in {"mental_health_plan", "mental_health_support"}:
            must_include.extend(["therapy_benefit", "self_harm_red_flags"])
        if analysis.intent == "lab_result_uncertainty":
            must_include.append("lab_context_needed")
        if analysis.intent == "symptom_triage":
            must_include.extend(["red_flags", "care_escalation"])
        return ResponseRequirements(
            must_include=must_include,
            must_avoid=must_avoid,
            audience="layperson",
            depth="medium",
            direct_answer_first=True,
        )
