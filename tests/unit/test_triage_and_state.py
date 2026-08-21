from medibot.clinical.analyzer import QueryAnalyzer
from medibot.clinical.state_manager import ClinicalStateManager
from medibot.clinical.triage import SafetyPreTriage
from medibot.core.schemas import ChatMessage


def test_emergency_phrase_is_emergent() -> None:
    triage = SafetyPreTriage()
    assert triage.classify("갑자기 심한 가슴 통증이 있고 숨이 안 쉬어져요") == "EMERGENT"


def test_assistant_claim_is_not_promoted_to_user_fact() -> None:
    manager = ClinicalStateManager()
    state = manager.get_or_create("s1")
    manager.update_from_messages(
        state,
        [
            ChatMessage(role="user", content="머리가 아파요"),
            ChatMessage(role="assistant", content="고혈압 때문일 수 있습니다."),
        ],
    )
    assert "conditions" not in state.user_asserted_facts
    assert state.assistant_previous_claims["last_response"] == "고혈압 때문일 수 있습니다."


def test_healthbench_english_intent_rules_capture_missing_context() -> None:
    analyzer = QueryAnalyzer()
    cases = [
        (
            "Can you make a 3-month plan for postpartum depression?",
            "mental_health_plan",
            "thoughts of self-harm or harming the baby",
        ),
        (
            "Can my herbal supplement interfere with my blood pressure medication?",
            "medication_safety",
            "supplement name and dose",
        ),
        (
            "My lab result is abnormal but I am not sure what it means.",
            "lab_result_uncertainty",
            "which lab test",
        ),
        (
            "I have a mild earache in Boston. What should I do?",
            "symptom_triage",
            "ear drainage",
        ),
        (
            "Could this be leishmaniasis after travel in North Africa?",
            "travel_tropical_infection",
            "travel location and dates",
        ),
    ]
    for message, expected_intent, expected_missing in cases:
        analysis = analyzer.analyze(message, "NON_EMERGENT")
        assert analysis.intent == expected_intent
        assert analysis.risk_class in {"medium", "high"}
        assert expected_missing in analysis.missing_information
