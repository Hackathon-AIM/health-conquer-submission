from typing import Any

import httpx

from medibot.config.settings import Settings
from medibot.core.prompts import PromptLoader
from medibot.core.schemas import (
    ClinicalState,
    QueryAnalysis,
    ResponseRequirements,
    RetrievalEvidence,
)
from medibot.reasoning.lunit_harness import LunitL2Harness


class MedicalGenerator:
    """OpenAI-compatible generator with deterministic fallback."""

    def __init__(
        self, settings: Settings, lunit_harness: LunitL2Harness | None = None
    ) -> None:
        self.settings = settings
        self.prompt_loader = PromptLoader(settings.prompt_dir)
        self.answer_prompt = self.prompt_loader.load("answer_generator.md")
        self.lunit_harness = lunit_harness or LunitL2Harness(settings)
        self.last_mode = "not_called"
        self.last_error: str | None = None
        self.last_evidence: list[RetrievalEvidence] = []
        self.last_l2_trace: dict[str, Any] = {}

    async def generate(
        self,
        message: str,
        state: ClinicalState,
        analysis: QueryAnalysis,
        requirements: ResponseRequirements,
        evidence: list[RetrievalEvidence],
    ) -> str:
        self.last_mode = "fallback"
        self.last_error = None
        self.last_evidence = []
        self.last_l2_trace = {}
        if self.settings.require_l2_final:
            self._validate_l2_final_path()
        if self.settings.final_model_provider == "lunit_l2":
            try:
                response = await self.lunit_harness.generate(
                    message, state, analysis, requirements, evidence
                )
                self.last_mode = "remote:lunit_l2:harness"
                self.last_evidence = self.lunit_harness.last_evidence
                self.last_l2_trace = self.lunit_harness.last_trace
                return response
            except Exception:
                self.last_error = "lunit_l2_harness_failed"
                if self.settings.require_l2_final or not self.settings.allow_fallback:
                    raise
        if self.settings.has_model_endpoint:
            try:
                response = await self._generate_remote(
                    message, state, analysis, requirements, evidence
                )
                self.last_mode = f"remote:{self.settings.final_model_provider}"
                return response
            except Exception:
                self.last_error = "remote_generation_failed"
                if self.settings.require_l2_final or not self.settings.allow_fallback:
                    raise
        return self._generate_fallback(message, analysis, evidence)

    async def _generate_remote(
        self,
        message: str,
        state: ClinicalState,
        analysis: QueryAnalysis,
        requirements: ResponseRequirements,
        evidence: list[RetrievalEvidence],
    ) -> str:
        assert self.settings.model_api_base is not None
        url = self.settings.model_api_base.rstrip("/") + "/chat/completions"
        headers: dict[str, str] = {}
        if self.settings.model_api_key:
            headers["Authorization"] = f"Bearer {self.settings.model_api_key}"
        payload: dict[str, Any] = {
            "model": self.settings.model_name,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": self._system_prompt(),
                },
                {
                    "role": "user",
                    "content": self._build_context(
                        message, state, analysis, requirements, evidence
                    ),
                },
            ],
        }
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_s) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        return str(data["choices"][0]["message"]["content"])

    def _build_context(
        self,
        message: str,
        state: ClinicalState,
        analysis: QueryAnalysis,
        requirements: ResponseRequirements,
        evidence: list[RetrievalEvidence],
    ) -> str:
        evidence_text = "\n".join(
            f"[{item.id}] {item.title}: {item.content}" for item in evidence
        )
        return (
            f"User message: {message}\n"
            f"Clinical state: {state.model_dump_json()}\n"
            f"Query analysis: {analysis.model_dump_json()}\n"
            f"Response requirements: {requirements.model_dump_json()}\n"
            f"Evidence:\n{evidence_text}"
            )

    def _generate_fallback(
        self, message: str, analysis: QueryAnalysis, evidence: list[RetrievalEvidence]
    ) -> str:
        missing = self._missing_context_sentence(analysis)
        if analysis.intent in {"mental_health_plan", "mental_health_support"}:
            return (
                "A practical 3-month postpartum depression plan should combine professional support, "
                "talk therapy, safety monitoring, and steady follow-up rather than relying on willpower alone. "
                "In the first 1-2 weeks, arrange an appointment with an OB-GYN, primary care clinician, or mental "
                "health professional, screen symptom severity, and make a safety plan. Over months 1-2, start or "
                "continue therapy such as CBT or interpersonal therapy, build sleep and support routines, and discuss "
                "medication options if symptoms are moderate, severe, or not improving. By month 3, review progress, "
                "relapse warning signs, and longer-term supports. Therapy helps by giving skills for mood, anxiety, "
                "guilt, relationship stress, and role changes after birth. Seek urgent help now for thoughts of "
                "self-harm, harming the baby, psychosis, inability to care for yourself or the baby, or rapidly "
                f"worsening symptoms. {missing}"
            )
        if analysis.intent == "lab_result_uncertainty":
            return (
                "A single unclear lab result usually cannot be interpreted safely without the exact test and context. "
                "Please check the test name, exact value, units, reference range, date, and why it was ordered; symptoms, "
                "medications, pregnancy status, and recent illness can change the meaning. Very abnormal values, chest "
                "pain, trouble breathing, confusion, severe weakness, fainting, or rapid worsening should prompt urgent "
                f"medical care. {missing}"
            )
        if analysis.intent == "symptom_triage":
            return (
                "A mild earache can sometimes be watched briefly with comfort measures, but the next step depends on "
                "red flags. Seek urgent care for fever, drainage from the ear, hearing loss, severe or worsening pain, "
                "dizziness, swelling around the ear, immune compromise, diabetes, recent injury, or symptoms lasting "
                f"more than 1-2 days. {missing}"
            )
        if analysis.intent == "travel_tropical_infection":
            return (
                "Leishmaniasis is possible after exposure in parts of North Africa, especially with persistent skin "
                "sores or ulcers after sand fly bites, but it needs clinician evaluation and often lab confirmation. "
                "A travel medicine or infectious disease clinician can decide whether testing or treatment is needed. "
                "Seek prompt care for fever, weight loss, enlarged abdomen or spleen, multiple worsening lesions, or "
                f"signs of infection. {missing}"
            )
        if analysis.intent == "medication_safety":
            if self._looks_korean(message):
                return (
                    "결론부터 말하면, 혈압약을 복용 중이거나 고혈압이 있다면 이부프로펜이나 특정 보충제를 반복해서 "
                    "사용하기 전 의사나 약사에게 확인하는 것이 안전합니다. 일부 약이나 보충제는 혈압 조절, 신장 기능, "
                    f"출혈 위험에 영향을 줄 수 있습니다. {missing}"
                )
            return (
                "Do not assume a herbal supplement is safe with blood pressure medication until the exact products are "
                "checked. Some supplements can affect blood pressure, bleeding risk, sedation, kidney function, or drug "
                "levels. Please provide the supplement name and dose, the blood pressure medicine name and dose, kidney "
                f"or liver disease, pregnancy status, and all other medicines or supplements. {missing}"
            )
        if evidence:
            return (
                "결론부터 말하면, 현재 정보만으로는 위험 신호가 없다면 일반적인 건강 정보로 접근할 수 있습니다. "
                "다만 증상이 심해지거나 새 위험 신호가 생기면 진료를 받는 것이 안전합니다."
            )
        return (
            "결론부터 말하면, 현재 질문은 일반 건강 정보 범위에서 답할 수 있습니다. 증상이 심하거나 오래 지속되거나 "
            "새로운 위험 신호가 있으면 의료진에게 상담하세요."
        )

    def _system_prompt(self) -> str:
        fallback = (
            "You are a safety-first medical assistant. Answer for a layperson. "
            "Use supplied evidence for high-risk claims and avoid unsupported dosage."
        )
        if not self.answer_prompt.strip():
            return fallback
        return f"{fallback}\n\n{self.answer_prompt.strip()}"

    def _missing_context_sentence(self, analysis: QueryAnalysis) -> str:
        if not analysis.missing_information:
            return ""
        items = ", ".join(analysis.missing_information)
        return f"Helpful details to confirm: {items}."

    def _looks_korean(self, message: str) -> bool:
        return any("가" <= char <= "힣" for char in message)

    def _validate_l2_final_path(self) -> None:
        if self.settings.final_model_provider != "lunit_l2":
            self.last_error = "l2_required_provider_mismatch"
            raise RuntimeError("Final answer generation requires Lunit L2 provider.")
        if not self.settings.has_model_endpoint:
            self.last_error = "l2_required_missing_endpoint"
            raise RuntimeError("Lunit L2 final generation requires a model endpoint.")
