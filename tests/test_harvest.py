"""근거 수확 회귀 테스트 — 네트워크 없이 돈다.

    python tests/test_harvest.py

왜 이 파일이 있나
  라이브 MCP 를 찍어 보니 openapi 계열 도구가 수확하는 근거의 **본문이 전부
  비어 있었다.**

      openapi_mfds_get_drug_indication   수확 5건 · 본문 0자 5건
      openapi_hira_get_drug_price        수확 6건 · 본문 0자 6건
      openapi_hira_disease_check_code    수확 1건 · 본문 0자 1건
      kcd_search_codes                   수확 1건 · 본문 0자 1건

  이 도구들은 자유 텍스트 필드를 주지 않는다. 답은 indication / max_price /
  kor_name 같은 **타입 필드**에 있는데 _harvest 는 content·text·body·row·snippet
  만 찾고 있었다. 그래서 약값·허가·코드 질문에서 검색은 status=sufficient 로
  성공하고, 모델은 아무 내용도 없는 [1] 을 인용하라는 지시를 받았다.

  아래 페이로드는 전부 라이브 응답에서 그대로 떠온 것이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from retrieval import Evidence, _harvest, _render_fields  # noqa: E402

# ── 라이브에서 뜬 실제 페이로드 ───────────────────────────────
HIRA_PRICE = {
    "items": [
        {
            "cite_uid": "cite-8e6b426806c0589b",
            "source_id": "HIRA-DGAMT:652606400",
            "tool_result_type": "hira_drug_price",
            "layer": 6,
            "name": "에비스타플러스정_(1정)",
            "drug_code": "652606400",
            "company": "알보젠코리아(주)",
            "pay_type": "급여",
            "max_price": 705,
            "unit": "정",
            "route": "내복",
            "effective_date": "2021-07-01",
            "substitutable": None,
            "code_before": None,
        }
    ]
}

MFDS_IND = {
    "items": [
        {
            "cite_uid": "cite-086831cf05001bf5",
            "source_id": "MFDS-IND:202106954",
            "tool_result_type": "mfds_indication",
            "layer": 5,
            "name": "타이레놀콜드-에스정",
            "ingredient_eng": "Acetaminophen/Chlorpheniramine Maleate",
            "atc_code": "R05X",
            "indication": "감기의 제증상(콧물, 코막힘, 기침, 발열, 두통)의 완화",
            "notice": None,
            "dosage": None,
        }
    ]
}

DISEASE_CODE = {
    "cite_uid": "cite-b7787f6f01266754",
    "source_id": "HIRA-SICK:C509",
    "tool_result_type": "hira_disease_master",
    "code": "C509",
    "kor_name": "상세불명의 유방의 악성 신생물",
    "eng_name": "Malignant neoplasm of breast unspecified",
    "usable_as_primary": True,
    "sex": None,
    "min_age": None,
}

# 자유 텍스트가 있는 도구는 예전 경로가 그대로 동작해야 한다.
PUBMED = {
    "items": [
        {
            "cite_uid": "cite-63a994c38ea7330f",
            "source_id": "PMID:26615879",
            "title": "Acetaminophen hepatotoxicity in mice",
            "content": "Acetaminophen is a commonly used analgesic. " * 20,
            "source_type": "pubmed",
            "relevance_score": 0.999,
        }
    ]
}

PAGES = {
    "cite_uid": "cite-7b709eef8f81dde6",
    "title": "EAU Guidelines on Prostate Cancer",
    "source_type": "guideline",
    "pages": [
        {"page": 60, "text": "Active surveillance is appropriate for low-risk disease."},
        {"page": 61, "text": "PSA density below 0.15 supports eligibility.",
         "charts": [{"paths": ["PSA > 10 -> biopsy"]}]},
    ],
}


def harvest(payload) -> dict[str, Evidence]:
    out: dict[str, Evidence] = {}
    _harvest(payload, out)
    return out


# ── 회귀: 본문 0자가 나오면 안 된다 ───────────────────────────
def test_drug_price_yields_the_price() -> None:
    """약값 질문의 답은 pay_type·max_price·effective_date 다."""
    ev = list(harvest(HIRA_PRICE).values())[0]
    assert ev.text, "본문이 비었다 — 인용 슬롯이 빈 곳을 가리킨다"
    for must in ("pay_type: 급여", "max_price: 705", "effective_date: 2021-07-01"):
        assert must in ev.text, (must, ev.text)


def test_drug_indication_yields_the_indication() -> None:
    ev = list(harvest(MFDS_IND).values())[0]
    assert "indication: 감기의 제증상" in ev.text, ev.text
    assert "atc_code: R05X" in ev.text


def test_disease_code_yields_billability() -> None:
    """'C50.9 로 청구 가능한가' 의 답은 usable_as_primary 다."""
    ev = list(harvest(DISEASE_CODE).values())[0]
    assert "usable_as_primary: True" in ev.text, ev.text
    assert "kor_name: 상세불명의 유방의 악성 신생물" in ev.text


# ── 배관 필드는 본문에 섞이지 않는다 ──────────────────────────
def test_plumbing_fields_are_not_in_the_body() -> None:
    """cite_uid·layer·source_id 는 답에 쓰이지 않는다. 본문 예산만 먹는다."""
    ev = list(harvest(HIRA_PRICE).values())[0]
    for noise in ("cite_uid", "tool_result_type", "layer:", "source_id"):
        assert noise not in ev.text, (noise, ev.text)


def test_null_fields_are_skipped() -> None:
    ev = list(harvest(MFDS_IND).values())[0]
    assert "notice" not in ev.text and "dosage" not in ev.text, ev.text


def test_huge_nested_values_are_skipped() -> None:
    """큰 구조는 본문 후보에서 이미 걸러졌거나 배관이다."""
    payload = {"cite_uid": "c1", "blob": {"x": ["y"] * 400}, "answer": "값"}
    ev = list(harvest(payload).values())[0]
    assert "answer: 값" in ev.text
    assert "blob" not in ev.text, len(ev.text)


# ── 기존 경로는 그대로 ────────────────────────────────────────
def test_free_text_tools_are_unchanged() -> None:
    ev = list(harvest(PUBMED).values())[0]
    assert ev.text.startswith("Acetaminophen is a commonly used analgesic.")
    # 자유 텍스트가 있으면 타입 필드를 덧붙이지 않는다.
    assert "relevance_score" not in ev.text


def test_page_content_and_flowcharts_are_unchanged() -> None:
    ev = list(harvest(PAGES).values())[0]
    assert "Active surveillance is appropriate" in ev.text
    assert "[flowchart] PSA > 10 -> biopsy" in ev.text


def test_render_fields_on_empty_payload() -> None:
    assert _render_fields({}) == ""
    assert _render_fields({"cite_uid": "c1", "layer": 5}) == ""


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
