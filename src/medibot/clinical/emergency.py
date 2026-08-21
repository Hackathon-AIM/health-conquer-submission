class EmergencyFastPath:
    """Immediate response path that does not wait for retrieval."""

    def compose(self, user_message: str) -> str:
        return (
            "지금 표현한 증상은 응급 상황일 수 있습니다. 즉시 119에 연락하거나 가까운 응급실로 이동하세요.\n\n"
            "가능하면 혼자 이동하지 말고 주변 사람에게 도움을 요청하세요. 의식 저하, 심한 호흡곤란, "
            "가슴 통증, 한쪽 마비, 말이 어눌함, 심한 출혈이 있으면 기다리지 않는 것이 안전합니다.\n\n"
            "음식이나 약을 추가로 복용하기 전에 응급 상담을 먼저 받으세요."
        )
