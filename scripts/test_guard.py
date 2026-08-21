"""① 오프라인 검증 — 도커도 API 키도 없이, 우리가 얹은 층만 3초 안에 확인한다.

    python3 scripts/test_guard.py

무엇을 보는가
  약물 안전 게이트는 결정론(규칙·사전)이라 모델 없이 그대로 재현된다.
  그러니 컨테이너를 띄우기 전에 여기서 먼저 깨뜨린다. 여기서 통과하지 못하면
  E2E 를 돌릴 이유가 없다.

  ⚠️ 오탐(경고가 붙으면 안 되는 곳에 붙는 것)을 정탐만큼 비중 있게 본다.
     제도 질문에 약물 경고가 붙으면 지시순응·의사소통에서 깎인다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from drug_safety import analyze, guard  # noqa: E402

OK, NG = "✅", "❌"

# (이름, 대화, 모델답변, 경고가_붙어야_하나, 답변에_있어야_할_단어)
CASES = [
    ("병용금기 — 와파린+아스피린",
     "user: 와파린 먹고 있는데 아스피린 같이 먹어도 되나요",
     "아스피린은 해열진통 효과가 있어 복용하실 수 있습니다.",
     True, ("출혈", "병용", "주의", "금기")),

    ("★ 출력 검사 — 사용자는 약을 말한 적이 없다",
     "user: 어제 술을 너무 많이 마셨는데 머리가 아파요\n"
     "assistant: 수분을 충분히 드세요.\n"
     "user: 두통약 뭐 먹으면 좋을까요",
     "숙취 두통에는 아세트아미노펜(타이레놀) 계열이 도움이 됩니다.",
     True, ("음주", "술", "간", "주의")),

    ("오탐 — 제도 질문",
     "user: 국민건강보험 본인부담 상한제가 뭔가요",
     "본인부담 상한제는 1년간 본인부담금이 상한액을 넘으면 초과분을 돌려주는 제도입니다.",
     False, ()),

    ("오탐 — '수술'의 '술'을 음주로 읽던 버그",
     "user: 국민건강보험법 제41조 요양급여 범위가 어떻게 되나요",
     "요양급여에는 진찰·검사, 약제·치료재료 지급, 처치·수술 등이 포함됩니다.",
     False, ()),

    ("오탐 — 문의일 뿐 복용 중이 아니다",
     "user: 타이레놀 먹어도 되나요",
     "일반적인 두통에는 아세트아미노펜을 상용량으로 복용할 수 있습니다.",
     False, ()),

("★ 회귀 — 모델이 '피하라'고 한 약을 우리가 경고하면 안 된다",
     "user: 어제 술을 너무 많이 마셨는데 머리가 아파요\nuser: 두통약 뭐 먹으면 좋을까요",
     "아세트아미노펜(타이레놀) 계열이 가장 안전한 1차 선택입니다.\n"
     "이부프로펜·나프록센 등 소염진통제(NSAIDs)는 위출혈 위험이 커지므로 피하십시오.\n"
     "머리 수술 병력·항응고제 복용 중이라면 의사와 상담 없이 다른 약을 추가하지 마십시오.",
     True, ("아세트아미노펜",)),

    ("★ 회귀 — 없는 병용금기를 지어내면 안 된다",
     "user: 어제 술을 너무 많이 마셨는데 머리가 아파요\nuser: 두통약 뭐 먹으면 좋을까요",
     "아세트아미노펜(타이레놀) 계열이 가장 안전한 1차 선택입니다.\n"
     "이부프로펜·나프록센 등 소염진통제(NSAIDs)는 피하십시오.\n"
     "항응고제 복용 중이라면 의사와 상담하십시오.",
     True, ()),

    ("★ 회귀 — 용량 질문에 동어반복 경고 금지",
     "user: 타이레놀 성인 1회 최대 용량이 얼마인가요",
     "타이레놀(아세트아미노펜) 성인 1회 최대 용량은 1,000mg이며, "
     "하루 총량 4,000mg을 넘기면 안 됩니다.",
     False, ()),

]


def main() -> None:
    print("=" * 68)
    print("  오프라인 검증 — 약물 안전 게이트 (모델 호출 없음)")
    print("=" * 68)
    n_ok = n_ng = 0
    for name, convo, answer, want_warn, words in CASES:
        out = guard(convo, answer)
        got_warn = out != answer
        hits = analyze(convo, answer)
        good = got_warn == want_warn
        if good and want_warn and words:
            good = any(w in out for w in words)
        print(f"\n{OK if good else NG} {name}")
        print(f"   기대: {'경고 붙음' if want_warn else '원문 그대로'} / "
              f"실제: {'경고 붙음' if got_warn else '원문 그대로'} (hit {len(hits)}건)")
        if got_warn:
            print("   " + out[: out.find("\n\n")].replace("\n", "\n   "))
        n_ok, n_ng = (n_ok + 1, n_ng) if good else (n_ok, n_ng + 1)

    print("\n" + "=" * 68)
    print(f"  통과 {n_ok} · 실패 {n_ng}")
    print("=" * 68)
    sys.exit(1 if n_ng else 0)


if __name__ == "__main__":
    main()
