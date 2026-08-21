from medibot.core.schemas import TriageClass


class SafetyPreTriage:
    """Rule-first high-recall emergency classifier for P0."""

    EMERGENT_TERMS = [
        "severe chest pain",
        "chest pain",
        "severe dyspnea",
        "can't breathe",
        "cannot breathe",
        "stroke",
        "altered consciousness",
        "severe bleeding",
        "anaphylaxis",
        "suicidal",
        "self-harm",
        "가슴 통증",
        "흉통",
        "숨이 안",
        "호흡곤란",
        "말이 어눌",
        "마비",
        "의식",
        "피가 멈추지",
        "아나필락시스",
        "자살",
    ]
    CONDITIONAL_TERMS = [
        "fever",
        "열",
        "임신",
        "pregnant",
        "어지러",
        "dizzy",
        "복통",
        "abdominal pain",
    ]

    def classify(self, message: str) -> TriageClass:
        lowered = message.lower()
        if any(term in lowered for term in self.EMERGENT_TERMS):
            return "EMERGENT"
        if any(term in lowered for term in self.CONDITIONAL_TERMS):
            return "CONDITIONALLY_EMERGENT"
        return "NON_EMERGENT"
