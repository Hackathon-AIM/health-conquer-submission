from medibot.core.schemas import QueryAnalysis, RetrievalEvidence, SafetyStatus, TriageClass


class ResponseComposer:
    def compose(
        self,
        draft: str,
        triage_class: TriageClass,
        analysis: QueryAnalysis,
        evidence: list[RetrievalEvidence],
        safety_result: dict[str, object],
    ) -> tuple[str, SafetyStatus]:
        blocking_issues = safety_result.get("blocking_issues", [])
        if blocking_issues and evidence:
            advisory_issues = [
                *list(safety_result.get("advisory_issues", [])),
                "evidence_limited_blocking_claim",
            ]
            safety_result = {
                **safety_result,
                "blocking_issues": [],
                "advisory_issues": advisory_issues,
            }
            blocking_issues = []
        if blocking_issues:
            if analysis.intent == "medication_safety":
                return (
                    "결론부터 말하면, 약물 병용을 안전하다고 단정하려면 정확한 약 이름과 개인 상태 확인이 필요합니다.\n\n"
                    "이부프로펜 같은 NSAID는 사람에 따라 혈압 조절, 신장 기능, 위장관 출혈 위험, 다른 약의 효과에 "
                    "영향을 줄 수 있습니다. 특히 혈압약을 복용 중이거나 신장 질환, 위궤양/출혈 병력, 항응고제 복용, "
                    "임신 가능성이 있으면 반복 복용 전 의사나 약사에게 확인하세요.\n\n"
                    "확인할 정보: 복용 중인 혈압약 이름과 용량, 이부프로펜 복용 목적과 예정 기간, 신장 기능, 다른 약/보충제, "
                    "알레르기 병력입니다. 호흡곤란, 심한 흉통, 의식 저하, 심한 알레르기 반응이 있으면 즉시 119 또는 응급실 도움을 받으세요.",
                    "fallback",
                )
            return (
                "현재 정보만으로 안전하게 단정하기 어렵습니다. 증상이 심하거나 빠르게 악화되면 의료진에게 상담하세요.",
                "fallback",
            )

        korean = self._looks_korean(draft)

        if triage_class == "CONDITIONALLY_EMERGENT":
            if korean:
                draft = (
                    "먼저, 심한 통증, 호흡곤란, 의식 저하, 한쪽 마비, 심한 출혈 같은 위험 신호가 있으면 "
                    "즉시 119 또는 응급실 도움을 받으세요.\n\n"
                    + draft
                )
            else:
                draft = (
                    "First, if there is severe pain, trouble breathing, confusion, one-sided weakness, "
                    "severe bleeding, or another major red flag, seek emergency care now.\n\n"
                    + draft
                )

        evidence_note = ""
        if evidence:
            label = "참고한 근거" if korean else "Evidence used"
            evidence_note = f"\n\n{label}: " + ", ".join(item.id for item in evidence)

        if korean:
            next_action = (
                "\n\n지금 할 일: 증상의 강도, 지속 시간, 복용 중인 약, 기저질환을 함께 확인하세요. "
                "악화되거나 위험 신호가 있으면 진료를 받으세요."
            )
        else:
            next_action = (
                "\n\nNext step: track severity, duration, medicines, and relevant conditions. "
                "Seek care if symptoms worsen or red flags appear."
            )
        advisory_note = ""
        if safety_result.get("advisory_issues"):
            if korean:
                advisory_note = (
                    "\n\n확인 필요: 현재 답변에는 일반적인 의료 정보가 포함되어 있어 개인 상황에 따라 달라질 수 있습니다. "
                    "정확한 판단을 위해 증상 경과, 복용 약, 검사 수치나 지역 정보를 의료진에게 함께 알려주세요."
                )
            else:
                advisory_note = (
                    "\n\nConfirmation needed: this includes general medical information and may change with personal "
                    "details. Share symptom timing, medicines, test values, and location with a clinician when relevant."
                )
        return draft + next_action + advisory_note + evidence_note, "pass"

    def _looks_korean(self, text: str) -> bool:
        return any("가" <= char <= "힣" for char in text)
