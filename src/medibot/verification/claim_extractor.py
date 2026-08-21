from medibot.core.schemas import ClinicalClaim, RetrievalEvidence


class ClaimExtractor:
    def extract(self, answer: str, evidence: list[RetrievalEvidence]) -> list[ClinicalClaim]:
        claims: list[ClinicalClaim] = []
        evidence_ids = [item.id for item in evidence]
        sentences = [part.strip() for part in answer.replace("\n", " ").split(".") if part.strip()]
        for idx, sentence in enumerate(sentences, start=1):
            lowered = sentence.lower()
            claim_type = "other"
            asks_for_context = any(
                term in lowered
                for term in [
                    "please provide",
                    "helpful details",
                    "to confirm",
                    "tell me",
                    "알려",
                    "확인",
                ]
            )
            if not asks_for_context and self._is_dosage_claim(lowered):
                claim_type = "dosage"
            elif self._is_interaction_claim(lowered):
                claim_type = "interaction"
            elif any(
                term in lowered
                for term in ["응급", "119", "emergency", "진료", "clinician", "urgent care"]
            ):
                claim_type = "recommendation"
            elif any(term in lowered for term in ["risk", "위험", "부담"]):
                claim_type = "risk"
            claims.append(
                ClinicalClaim(
                    id=f"claim-{idx}",
                    text=sentence,
                    claim_type=claim_type,
                    evidence_ids=evidence_ids
                    if claim_type in {"dosage", "interaction", "recommendation", "risk"}
                    else [],
                )
            )
        return claims

    def _is_dosage_claim(self, lowered: str) -> bool:
        if any(term in lowered for term in ["dose", "dosage", "용량"]):
            return True
        if "mg" not in lowered:
            return False
        if any(
            unit in lowered
            for unit in ["mg/dl", "mg / dl", "mg per dl", "mg/l"]
        ):
            return False
        return any(
            term in lowered
            for term in [
                "take",
                "every",
                "daily",
                "per day",
                "tablet",
                "capsule",
                "복용",
                "먹",
                "하루",
                "정씩",
            ]
        )

    def _is_interaction_claim(self, lowered: str) -> bool:
        if any(
            term in lowered
            for term in [
                "interaction",
                "interact",
                "interfere",
                "contraindication",
                "contraindicated",
                "상호작용",
                "금기",
                "효과를 약하게",
            ]
        ):
            return True

        has_blood_pressure_med_context = any(
            term in lowered
            for term in [
                "blood pressure medication",
                "blood pressure medicine",
                "antihypertensive",
                "혈압약",
                "고혈압약",
                "혈압 약",
            ]
        )
        has_medicine_context = any(
            term in lowered
            for term in [
                "drug",
                "medicine",
                "medication",
                "ibuprofen",
                "nsaid",
                "이부프로펜",
                "부루펜",
                "약물",
                "복용",
            ]
        )
        return has_blood_pressure_med_context and has_medicine_context
