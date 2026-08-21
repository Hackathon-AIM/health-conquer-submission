from medibot.core.schemas import ClinicalClaim, RetrievalEvidence


class GroundednessVerifier:
    HIGH_RISK_TYPES = {"dosage", "interaction", "recommendation", "risk"}
    BLOCKING_TYPES = {"dosage", "interaction"}
    BLOCKING_TERMS = {
        "contraindication",
        "contraindicated",
        "금기",
        "절대",
        "반드시 안전",
        "always safe",
        "definitely safe",
    }

    def verify(
        self, claims: list[ClinicalClaim], evidence: list[RetrievalEvidence]
    ) -> dict[str, object]:
        evidence_ids = {item.id for item in evidence}
        unsupported: list[str] = []
        blocking: list[str] = []
        advisory: list[str] = []
        for claim in claims:
            if claim.claim_type not in self.HIGH_RISK_TYPES:
                continue
            if claim.claim_type in {"recommendation", "risk"} and any(
                term in claim.text.lower()
                for term in ["119", "응급", "의료진", "상담", "진료", "emergency", "clinician"]
            ):
                continue
            if not claim.evidence_ids or not set(claim.evidence_ids).intersection(evidence_ids):
                unsupported.append(claim.id)
                if self._is_blocking(claim):
                    blocking.append(claim.id)
                else:
                    advisory.append(claim.id)
        return {
            "passed": not blocking,
            "unsupported_claim_ids": unsupported,
            "blocking_claim_ids": blocking,
            "advisory_claim_ids": advisory,
        }

    def _is_blocking(self, claim: ClinicalClaim) -> bool:
        lowered = claim.text.lower()
        return claim.claim_type in self.BLOCKING_TYPES or any(
            term in lowered for term in self.BLOCKING_TERMS
        )
