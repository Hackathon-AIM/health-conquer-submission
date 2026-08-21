"""mock 소스 — 엔드포인트 없이 파이프라인 전체를 돌리기 위한 스텁.

이게 있어야 B가 엔드포인트를 못 뚫어도 A·C·D가 계속 작업할 수 있다.
현장에서 흔한 사고 1순위가 "엔드포인트가 안 열림"이므로 이 장치가 보험이다.
"""

from __future__ import annotations

import hashlib

from ..contracts import Doc
from .base import Searcher

_CANNED: dict[str, list[tuple[str, str, str]]] = {
    "guideline": [
        ("성인 두통 초기 평가",
         "일차성 두통은 병력 청취로 대부분 감별된다. 갑작스러운 최고 강도의 두통, "
         "신경학적 결손 동반, 발열·경부강직 동반 시 즉시 영상검사와 응급 평가가 필요하다. "
         "그 외에는 수분 섭취·수면 조절·유발요인 회피를 우선 권고한다.",
         "두통 진료지침 p.14"),
        ("어지럼증 진료 알고리즘",
         "기립 시 악화되는 어지럼은 기립성 저혈압을 시사한다. 항고혈압제 복용자에서 흔하며 "
         "용량 조정이 필요할 수 있다. 회전성 어지럼이 수 초~수 분 지속되면 양성돌발두위현훈을 고려한다.",
         "어지럼증 지침 p.7"),
        ("고혈압 관리 지침",
         "생활요법(체중 감량, 저염식, 절주, 규칙적 운동)이 1차이며, "
         "가정혈압 측정을 권장한다. 진료실 혈압 140/90 이상이 반복되면 약물치료를 고려한다.",
         "고혈압 진료지침 2024 p.32"),
    ],
    "drug": [
        ("아세트아미노펜 (타이레놀)",
         "해열·진통 목적. 성인 1회 500~650mg, 1일 최대 4000mg을 초과하지 않는다. "
         "사용상의 주의사항: 알코올을 상습적으로 섭취하는 사람은 간독성 위험이 증가할 수 있으므로 "
         "복용 전 의사·약사와 상의할 것. 다른 감기약과 병용 시 성분 중복에 주의.",
         "품목기준코드 195700020"),
        ("이부프로펜",
         "소염·진통·해열. 성인 1회 200~400mg, 1일 3회. "
         "사용상의 주의사항: 음주 시 위장관 출혈 위험이 증가할 수 있다. "
         "신장애 환자, 고령자는 신중히 투여. 임부에게는 투여하지 않는다.",
         "품목기준코드 197800115"),
    ],
    "law": [
        ("국민건강보험법",
         "제41조(요양급여) 가입자와 피부양자의 질병, 부상, 출산 등에 대하여 "
         "진찰·검사, 약제·치료재료의 지급, 처치·수술 및 그 밖의 치료 등의 요양급여를 실시한다.",
         "제41조"),
    ],
    "hira": [
        ("요양급여 적용기준",
         "해당 검사는 임상적으로 필요한 경우에 한하여 요양급여를 인정하며, "
         "그 외에는 비급여로 산정한다. 세부 인정기준은 고시에 따른다.",
         "고시 제2024-00호"),
    ],
    "pubmed": [
        ("Acetaminophen and alcohol: a systematic review",
         "Evidence on hepatotoxicity risk with therapeutic doses of acetaminophen in "
         "moderate alcohol consumers remains mixed. Chronic heavy alcohol use is a "
         "consistently reported risk factor. Clinicians should individualise advice.",
         "PMID:00000001"),
    ],
}


class MockSearcher(Searcher):
    def __init__(self, cfg, name: str, client=None):
        super().__init__(cfg, client)
        self.name = name

    async def _search(self, query: str, top_k: int) -> list[Doc]:
        rows = _CANNED.get(self.name, [])
        out: list[Doc] = []
        for i, (title, text, loc) in enumerate(rows[:top_k]):
            # 쿼리에 따라 점수가 흔들리게 해서 리랭킹 동작을 확인할 수 있게 함
            h = int(hashlib.md5(f"{query}{title}".encode()).hexdigest()[:4], 16) / 65535
            out.append(Doc(source=self.name, title=title, text=text,
                           locator=loc, score=round(0.5 + 0.5 * h - i * 0.05, 4)))
        return out
