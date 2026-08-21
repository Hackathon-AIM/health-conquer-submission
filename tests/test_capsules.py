from capsules import AMBIGUOUS_RISK, select_capsules


def messages(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def test_ordinary_question_has_no_capsule() -> None:
    assert select_capsules(messages("What exercises help with high blood pressure?")) == []


def test_short_high_risk_fragment_gets_capsule() -> None:
    assert select_capsules(messages("Emergency insulin pump failure")) == [AMBIGUOUS_RISK]


def test_detailed_high_risk_question_is_not_treated_as_ambiguous() -> None:
    text = (
        "My insulin pump stopped after I changed the battery, and my glucose is 240. "
        "What should I do while I wait for the replacement?"
    )
    assert AMBIGUOUS_RISK not in select_capsules(messages(text))


def test_latest_user_turn_only() -> None:
    conversation = [
        {"role": "user", "content": "Emergency insulin pump failure"},
        {"role": "assistant", "content": "Please clarify."},
        {"role": "user", "content": "It is working now."},
    ]
    assert select_capsules(conversation) == []
