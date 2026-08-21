from collections import defaultdict
from uuid import uuid4

from medibot.core.schemas import ChatMessage, ClinicalState


class ClinicalStateManager:
    """In-memory session state for P0 multi-turn smoke testing."""

    def __init__(self) -> None:
        self._states: dict[str, ClinicalState] = {}

    def get_or_create(self, session_id: str | None) -> ClinicalState:
        sid = session_id or str(uuid4())
        if sid not in self._states:
            self._states[sid] = ClinicalState(session_id=sid)
        return self._states[sid]

    def update_from_messages(
        self, state: ClinicalState, messages: list[ChatMessage]
    ) -> ClinicalState:
        user_turns = [m for m in messages if m.role == "user"]
        assistant_turns = [m for m in messages if m.role == "assistant"]
        state.turn_index = max(len(user_turns), state.turn_index)
        state.turns_remaining = max(0, 3 - state.turn_index)

        facts: dict[str, list[str]] = defaultdict(list)
        for msg in user_turns:
            text = msg.content.lower()
            if any(term in text for term in ["고혈압", "hypertension"]):
                facts["conditions"].append("hypertension")
            if any(term in text for term in ["당뇨", "diabetes"]):
                facts["conditions"].append("diabetes")
            for drug in ["amlodipine", "losartan", "로사르탄", "이부프로펜", "ibuprofen"]:
                if drug in text:
                    facts["medications"].append(drug)
            if any(term in text for term in ["한국", "국내", "심평원", "건강보험"]):
                state.jurisdiction = "KR"

        for key, values in facts.items():
            existing = set(state.user_asserted_facts.get(key, []))
            state.user_asserted_facts[key] = sorted(existing.union(values))

        if assistant_turns:
            state.assistant_previous_claims["last_response"] = assistant_turns[-1].content
        return state
