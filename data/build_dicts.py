"""사전 자동 생성 — 손으로 만들지 않는다.

식약처 의약품 목록(공공데이터포털)을 받아서 3개 JSON을 만든다.
  drug_map.json                key(이름) -> 성분코드
  product_to_ingredients.json  key(제품) -> [성분코드]   ※ 복합제 1:N 전개
  drug_class.json              효능군 -> [대표 성분]

⚠️ 복합제 전개가 중요하다.
   "감기약 + 두통약"은 제품명으로는 서로 다른 약이지만 성분으로 펼치면
   아세트아미노펜이 양쪽에 들어 있어 효능군중복·용량초과가 된다.
   전개하지 않으면 이 흔한 사고 유형을 통째로 놓친다.

사용:
    python data/build_dicts.py --csv data/의약품목록.csv
    python data/build_dicts.py --seed          # 데모용 시드만 생성
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SUFFIX = re.compile(r"(정|캡슐|캅셀|시럽|산|주|액|이알|서방정|연질캡슐|정제|현탁액)$")


def key(s: str) -> str:
    s = str(s).strip().lower().replace(" ", "").replace("·", "")
    return _SUFFIX.sub("", s)


# 시드 — 대국민 챗봇에서 실제로 언급될 약은 롱테일이 아니라 소수에 집중된다.
# 상위 50개가 질문의 90%를 커버한다. 여기서 시작해 현장에서 확장할 것.
SEED_PRODUCTS = {
    "타이레놀": ["아세트아미노펜"],
    "타이레놀이알": ["아세트아미노펜"],
    "타이레놀콜드": ["아세트아미노펜", "클로르페니라민", "슈도에페드린"],
    "판피린": ["아세트아미노펜", "클로르페니라민", "카페인"],
    "게보린": ["아세트아미노펜", "이소프로필안티피린", "카페인"],
    "부루펜": ["이부프로펜"],
    "애드빌": ["이부프로펜"],
    "이지엔6": ["이부프로펜"],
    "아스피린": ["아스피린"],
    "낙센": ["나프록센"],
    "지르텍": ["세티리진"],
    "알레그라": ["펙소페나딘"],
    "클라리틴": ["로라타딘"],
    "겔포스": ["수산화알루미늄"],
    "베아제": ["소화효소"],
    "훼스탈": ["소화효소"],
    "와파린": ["와파린"],
    "암로디핀": ["암로디핀"],
    "노바스크": ["암로디핀"],
    "메트포르민": ["메트포르민"],
}

SEED_CLASS = {
    "해열진통제": ["아세트아미노펜", "이부프로펜", "아스피린", "나프록센"],
    "소염진통제": ["이부프로펜", "나프록센", "덱시부프로펜"],
    "진통제": ["아세트아미노펜", "이부프로펜", "아스피린", "나프록센"],
    "NSAID": ["이부프로펜", "아스피린", "나프록센", "디클로페낙"],
    "비스테로이드성소염진통제": ["이부프로펜", "아스피린", "나프록센"],
    "아세트아미노펜 계열": ["아세트아미노펜"],
    "항히스타민제": ["세티리진", "로라타딘", "클로르페니라민", "펙소페나딘"],
    "제산제": ["수산화알루미늄", "탄산칼슘"],
    "소화제": ["소화효소"],
    "항응고제": ["와파린"],
    "혈압약": ["암로디핀", "로사르탄", "텔미사르탄"],
    "항고혈압제": ["암로디핀", "로사르탄", "텔미사르탄"],
}


def build_from_seed():
    drug_map, p2i = {}, {}
    for prod, ings in SEED_PRODUCTS.items():
        p2i[key(prod)] = ings
        drug_map[key(prod)] = ings[0]
        for ing in ings:
            drug_map.setdefault(key(ing), ing)
            p2i.setdefault(key(ing), [ing])
    for cls, ings in SEED_CLASS.items():
        for ing in ings:
            drug_map.setdefault(key(ing), ing)
            p2i.setdefault(key(ing), [ing])
    return drug_map, p2i, dict(SEED_CLASS)


# 공공데이터 CSV 는 배포처마다 컬럼명이 다르다. 하나로 못 박지 말고 후보를 훑는다.
# 출처: 공공데이터포털 '식품의약품안전처_의약품 제품 허가정보' (15095677) 등
COLS = {
    "product":  ["제품명", "ITEM_NAME", "품목명", "PRDUCT", "itemName"],
    "ingr":     ["주성분", "성분명", "MAIN_ITEM_INGR", "MATERIAL_NAME", "INGR_NAME",
                 "주성분영문", "MAIN_INGR"],
    "code":     ["성분코드", "MAIN_INGR_CODE", "품목기준코드", "ITEM_SEQ", "INGR_CODE"],
    "klass":    ["약효분류명", "CLASS_NAME", "전문일반구분", "분류명", "CLASS_NO_NAME"],
    "caution":  ["사용상의주의사항", "사용상의 주의사항", "NB_DOC_DATA", "UD_DOC_DATA",
                 "주의사항"],
    "english":  ["영문제품명", "ITEM_ENG_NAME", "성분영문명", "INGR_ENG_NAME"],
}


def pick(row: dict, kind: str) -> str:
    """여러 후보 컬럼명 중 값이 있는 것을 고른다."""
    for c in COLS[kind]:
        v = row.get(c)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def split_ingredients(raw: str) -> list[str]:
    """복합제 성분 문자열을 쪼갠다.

    실제 데이터는 이런 식이다:
      "아세트아미노펜(수출용)|클로르페니라민말레산염|дл-메틸에페드린염산염"
      "아세트아미노펜 325mg, 카페인무수물 30mg"
    용량·괄호를 떼고 성분명만 남긴다.
    """
    if not raw:
        return []
    parts = re.split(r"[,/|;·]|\s{2,}", raw)
    out = []
    for p in parts:
        p = re.sub(r"\([^)]*\)", "", p)                     # 괄호 제거
        p = re.sub(r"\d+(\.\d+)?\s*(mg|g|ml|mcg|μg|IU|단위)", "", p, flags=re.I)
        p = p.strip(" .-·")
        if 2 <= len(p) <= 40:
            out.append(p)
    return out


def build_from_csv(path: Path, caution_out: Path | None = None):
    """식약처 의약품 목록 CSV -> 사전 3종 (+ risk 스크리닝).

    필요한 컬럼(있는 것만 쓴다): 제품명 / 주성분 / 성분코드 / 약효분류명 / 사용상의주의사항

    다운로드:
      https://www.data.go.kr/data/15095677/openapi.do   (의약품 제품 허가정보)
      https://nedrug.mfds.go.kr/cntnts/80              (의약품안전나라 공공데이터)
    """
    import csv
    drug_map, p2i, cls = build_from_seed()
    caution_screen: dict[str, list[str]] = {}
    n = 0

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        print(f"  컬럼: {', '.join((reader.fieldnames or [])[:12])}")
        for row in reader:
            prod = pick(row, "product")
            if not prod:
                continue
            n += 1
            code = pick(row, "code")
            ings = split_ingredients(pick(row, "ingr")) or [prod]

            # 제품 -> 성분 N개 (복합제 전개). 이게 없으면 감기약+두통약 중복을 놓친다.
            p2i[key(prod)] = ings
            drug_map[key(prod)] = code or ings[0]
            for ing in ings:
                drug_map.setdefault(key(ing), code or ing)
                p2i.setdefault(key(ing), [ing])

            eng = pick(row, "english")
            if eng:
                drug_map.setdefault(key(eng), code or ings[0])

            klass = pick(row, "klass")
            if klass:
                cls.setdefault(klass, [])
                for ing in ings:
                    if ing not in cls[klass]:
                        cls[klass].append(ing)

            # risk_check 원천: '사용상의 주의사항' 을 키워드 스크리닝
            caution = pick(row, "caution")
            if caution:
                sys.path.insert(0, str(HERE.parent / "src"))
                from medai.gates.risk import screen_caution_text
                types = screen_caution_text(caution)
                if types:
                    for ing in ings:
                        caution_screen.setdefault(ing, [])
                        for t in types:
                            if t not in caution_screen[ing]:
                                caution_screen[ing].append(t)

    print(f"  처리한 행: {n}")
    if caution_out and caution_screen:
        caution_out.write_text(
            json.dumps(caution_screen, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  risk_screen.json: {len(caution_screen)}개 성분")
    return drug_map, p2i, cls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()

    if a.csv and Path(a.csv).exists():
        dm, p2i, cls = build_from_csv(Path(a.csv), HERE / "risk_screen.json")
    else:
        dm, p2i, cls = build_from_seed()

    for name, obj in [("drug_map.json", dm),
                      ("product_to_ingredients.json", p2i),
                      ("drug_class.json", cls)]:
        (HERE / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {name}: {len(obj)}건")


if __name__ == "__main__":
    main()
