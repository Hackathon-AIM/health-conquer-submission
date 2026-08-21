from medibot.core.schemas import TriageClass


class ClinicalSafetyVerifier:
    def verify(
        self,
        answer: str,
        triage_class: TriageClass,
        coverage_result: dict[str, object],
        groundedness_result: dict[str, object],
    ) -> dict[str, object]:
        blocking_issues: list[str] = []
        advisory_issues: list[str] = []
        lowered_answer = answer.lower()
        if triage_class == "EMERGENT" and not any(
            term in lowered_answer for term in ["119", "응급실", "emergency"]
        ):
            blocking_issues.append("missing_emergency_action")
        if coverage_result.get("sufficient") is False:
            advisory_issues.append("insufficient_high_risk_evidence")

        blocking_claim_ids = groundedness_result.get("blocking_claim_ids", [])
        advisory_claim_ids = groundedness_result.get("advisory_claim_ids", [])
        if blocking_claim_ids:
            blocking_issues.append("unsupported_blocking_claim")
        if advisory_claim_ids:
            advisory_issues.append("unsupported_advisory_claim")

        issues = [*blocking_issues, *advisory_issues]
        return {
            "passed": not blocking_issues,
            "issues": issues,
            "blocking_issues": blocking_issues,
            "advisory_issues": advisory_issues,
        }
