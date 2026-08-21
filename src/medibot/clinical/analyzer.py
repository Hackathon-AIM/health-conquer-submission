from medibot.core.schemas import QueryAnalysis, TriageClass


class QueryAnalyzer:
    """Rule-based P0 analyzer with prompt-ready output shape."""

    DRUG_TERMS = {
        "ibuprofen": ["ibuprofen", "이부프로펜", "부루펜"],
        "amlodipine": ["amlodipine", "암로디핀"],
        "losartan": ["losartan", "로사르탄"],
        "blood_pressure_medication": [
            "blood pressure medication",
            "blood pressure medicine",
            "antihypertensive",
            "bp medication",
        ],
        "herbal_supplement": ["herbal supplement", "supplement", "herb", "st john"],
    }
    DISEASE_TERMS = {
        "hypertension": ["hypertension", "고혈압"],
        "diabetes": ["diabetes", "당뇨"],
        "postpartum_depression": [
            "postpartum depression",
            "post-partum depression",
            "postnatal depression",
            "after giving birth",
        ],
        "leishmaniasis": ["leishmaniasis", "sand fly", "sandfly"],
    }
    SYMPTOM_TERMS = {
        "earache": ["earache", "ear pain", "ear infection", "귀 통증"],
        "lab_uncertainty": [
            "lab result",
            "blood test",
            "test result",
            "reference range",
            "abnormal lab",
        ],
        "mental_health": ["depressed", "depression", "anxiety", "mood"],
        "skin_lesion": ["skin lesion", "ulcer", "sore", "rash"],
    }

    def analyze(self, message: str, triage_class: TriageClass) -> QueryAnalysis:
        lowered = message.lower()
        drugs = [
            canonical
            for canonical, terms in self.DRUG_TERMS.items()
            if any(term in lowered for term in terms)
        ]
        diseases = [
            canonical
            for canonical, terms in self.DISEASE_TERMS.items()
            if any(term in lowered for term in terms)
        ]
        symptoms = [
            canonical
            for canonical, terms in self.SYMPTOM_TERMS.items()
            if any(term in lowered for term in terms)
        ]

        asks_dosage = any(term in lowered for term in ["dosage", "dose", "용량", "몇 mg"])
        asks_interaction = any(
            term in lowered
            for term in [
                "같이",
                "상호작용",
                "interaction",
                "interact",
                "interfere",
                "먹어도",
                "병용",
            ]
        )
        asks_guideline = any(term in lowered for term in ["guideline", "가이드라인", "권고"])
        asks_law = any(term in lowered for term in ["보험", "법", "심평원", "hira"])
        asks_code = any(
            term in lowered
            for term in ["질병코드", "질병 코드", "질병 분류", "kcd", "icd", "diagnosis code"]
        )
        asks_adverse_event = any(
            term in lowered
            for term in ["부작용", "이상사례", "adverse event", "side effect", "faers"]
        )
        asks_plan = any(term in lowered for term in ["plan", "schedule", "3-month", "3 month", "계획"])
        asks_lab = "lab_uncertainty" in symptoms
        asks_travel = any(
            term in lowered
            for term in ["north africa", "africa", "travel", "traveler", "여행", "북아프리카"]
        )

        retrieval_need = "skip"
        risk_class = "low"
        intent = "general_health_information"
        complexity = "simple"
        graph_candidate = False
        missing_information: list[str] = []

        if triage_class == "EMERGENT":
            risk_class = "emergency"
            intent = "emergency_triage"
            retrieval_need = "skip"
        elif "postpartum_depression" in diseases or (
            "mental_health" in symptoms and any(term in lowered for term in ["postpartum", "post-partum", "postnatal"])
        ):
            risk_class = "medium"
            intent = "mental_health_plan" if asks_plan else "mental_health_support"
            retrieval_need = "optional"
            complexity = "medium"
            missing_information = [
                "severity and duration of mood symptoms",
                "sleep, appetite, and ability to care for self or baby",
                "thoughts of self-harm or harming the baby",
                "current support, therapy, and medications",
            ]
        elif "herbal_supplement" in drugs and (
            "blood_pressure_medication" in drugs or "hypertension" in diseases or asks_interaction
        ):
            risk_class = "high"
            intent = "medication_safety"
            retrieval_need = "required"
            complexity = "medium"
            graph_candidate = True
            missing_information = [
                "supplement name and dose",
                "blood pressure medication name and dose",
                "kidney disease, liver disease, pregnancy, and other conditions",
                "other prescription, over-the-counter, or herbal products",
            ]
        elif drugs and (asks_interaction or asks_dosage or asks_adverse_event):
            risk_class = "high"
            intent = "medication_safety"
            retrieval_need = "required"
            complexity = "medium"
            graph_candidate = len(drugs) > 1 or bool(diseases)
            missing_information = [
                "exact medication names and doses",
                "kidney disease or other relevant conditions",
                "how often and how long the medicine would be used",
            ]
        elif asks_lab:
            risk_class = "medium"
            intent = "lab_result_uncertainty"
            retrieval_need = "optional"
            complexity = "medium"
            missing_information = [
                "which lab test",
                "exact value and units",
                "reference range",
                "test date",
                "symptoms and clinical context",
            ]
        elif "earache" in symptoms:
            risk_class = "medium"
            intent = "symptom_triage"
            retrieval_need = "optional"
            complexity = "medium"
            missing_information = [
                "fever",
                "ear drainage",
                "hearing loss",
                "severe pain",
                "dizziness",
                "immune compromise",
            ]
        elif "leishmaniasis" in diseases or asks_travel:
            risk_class = "medium"
            intent = "travel_tropical_infection"
            retrieval_need = "optional"
            complexity = "medium"
            missing_information = [
                "travel location and dates",
                "skin lesion appearance and duration",
                "fever, weight loss, or enlarged spleen",
                "available local clinician or infectious disease specialist",
            ]
        elif asks_code:
            risk_class = "medium"
            intent = "disease_code_lookup"
            retrieval_need = "required"
            complexity = "medium"
        elif asks_guideline or asks_law:
            risk_class = "medium"
            intent = "clinical_guidance"
            retrieval_need = "required"
            complexity = "medium"
        elif diseases or drugs:
            retrieval_need = "optional"
            complexity = "medium"

        jurisdiction = "KR" if asks_law or asks_code else None
        return QueryAnalysis(
            intent=intent,
            complexity=complexity,
            risk_class=risk_class,
            jurisdiction=jurisdiction,
            jurisdiction_confidence=1.0 if jurisdiction else 0.0,
            uncertainty_type="reducible" if risk_class in {"medium", "high"} else "none",
            missing_information=missing_information,
            retrieval_need=retrieval_need,
            graph_reasoning_candidate=graph_candidate,
            diseases=diseases,
            symptoms=symptoms,
            drugs=drugs,
        )
