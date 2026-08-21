"""모델 호출 전에 적용하는 보수적인 응급 신호 게이트."""

from __future__ import annotations

import re


_PATTERNS = (
    re.compile(r"(가슴|흉부).{0,12}(심하게|쥐어짜|압박|통증).{0,24}(식은땀|호흡|숨|구토|퍼지)"),
    re.compile(r"(숨을?\s*못|호흡.{0,8}(곤란|안\s*돼)|질식|입술.{0,6}(파래|청색))"),
    re.compile(r"(의식.{0,8}(없|잃|흐려)|깨워도.{0,5}(반응|안\s*일어나)|경련.{0,8}(계속|반복))"),
    re.compile(r"(한쪽.{0,8}(마비|힘이\s*빠)|말이.{0,8}(어눌|안\s*나)|얼굴.{0,8}(처지|마비))"),
    re.compile(r"(피가.{0,8}(멈추지|계속)|토혈|객혈|검은\s*변|대량\s*출혈)"),
    re.compile(r"(혀|목|입술).{0,8}(붓|부어).{0,16}(숨|호흡|삼키)"),
    re.compile(r"(약|농약|세제|화학물질).{0,10}(많이|한꺼번에|과다).{0,10}(먹|마셨|복용)"),
    re.compile(r"(자살|죽고\s*싶|스스로.{0,8}(해치|죽)|극단적\s*선택)"),
    re.compile(r"(severe chest pain|cannot breathe|unconscious|stroke symptoms|heavy bleeding|anaphylaxis)", re.I),
)


def emergency_response(text: str) -> str | None:
    compact = " ".join(text.split())
    if not any(pattern.search(compact) for pattern in _PATTERNS):
        return None
    if re.search(r"(가슴|흉부).{0,12}(심하게|쥐어짜|압박|통증).{0,24}(식은땀|호흡|숨|구토|퍼지)", compact):
        opening = "119에 지금 연락하세요. 말씀하신 가슴의 심한 통증·압박감과 식은땀은 즉시 평가가 필요한 증상입니다. "
    elif re.search(r"(숨을?\s*못|호흡.{0,8}(곤란|안\s*돼)|질식|입술.{0,6}(파래|청색))", compact):
        opening = "119에 지금 연락하세요. 말씀하신 심한 호흡곤란은 즉시 평가가 필요한 증상입니다. "
    else:
        opening = "119에 지금 연락하거나 가까운 응급실로 가세요. 말씀하신 증상은 즉시 평가가 필요할 수 있습니다. "
    return (
        opening
        + "혼자 운전하지 말고 "
        "주변 사람에게 도움을 요청하세요. 의식이나 호흡이 없으면 119 안내에 따라 심폐소생술을 시작하고, "
        "복용한 약이나 물질이 있다면 용기·이름·복용량·시간을 함께 가져가세요."
    )
