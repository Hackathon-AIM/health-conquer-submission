from medibot.core.schemas import ClinicalState, ConversationDecision, QueryAnalysis


class ConversationPolicyManager:
    """Minimal turn-aware policy for P0."""

    def decide(
        self, analysis: QueryAnalysis, state: ClinicalState
    ) -> ConversationDecision:
        if analysis.risk_class == "emergency":
            return ConversationDecision(can_answer_safely_now=True, decision="answer")
        if state.turns_remaining == 0:
            return ConversationDecision(can_answer_safely_now=True, decision="answer")
        if analysis.risk_class == "high" and analysis.missing_information:
            return ConversationDecision(
                expected_information_gain="high",
                can_answer_safely_now=True,
                decision="answer",
            )
        return ConversationDecision(can_answer_safely_now=True, decision="answer")
