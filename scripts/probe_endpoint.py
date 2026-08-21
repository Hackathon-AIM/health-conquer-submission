"""엔드포인트의 실제 한계를 직접 재는 스크립트.

문서나 구전으로 도는 숫자를 믿지 않는다. 코드에 박힌 상수가 틀렸던 전례가 있다
(`SERVER_MAX_TOKENS = 2048` 은 실제로 32,768 이었다). 값이 의심되면 이걸 돌린다.

    python scripts/probe_endpoint.py             # 빠른 확인 (호출 4회)
    python scripts/probe_endpoint.py --deep      # 이분탐색으로 경계까지 (호출 ~15회)

2026-08-22 측정 결과:
    max_model_len        131,072  (/v1/models 및 실제 거절 경계 모두 일치)
    max_tokens 상한       32,768  (그 위는 output_limit_exceeded)
    한국어                1.19 자/토큰 (0.84 tok/자)
    메시지 바이트 상한    ~723KB (message_too_large), nginx 413 은 ~1,031KB
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from submission.config import SETTINGS, SERVER_MAX_CONTEXT, SERVER_MAX_TOKENS  # noqa: E402

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {SETTINGS.fm_api_key}",
}
BASE = SETTINGS.fm_api_url.rstrip("/")


def _get(path: str) -> tuple[int, str]:
    request = urllib.request.Request(f"{BASE}{path}", headers=HEADERS, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _chat(filler_chars: int, max_tokens: int = 1, timeout: float = 180) -> dict[str, Any]:
    payload = {
        "model": SETTINGS.fm_model,
        "messages": [{"role": "user", "content": "a " * (filler_chars // 2)}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=body, headers=HEADERS, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
            return {"status": response.status, "usage": data.get("usage") or {}, "bytes": len(body)}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300].replace("\n", " ")
        return {"status": exc.code, "error": detail, "bytes": len(body)}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "error": f"{type(exc).__name__}: {exc}", "bytes": len(body)}


def quick() -> None:
    print("=" * 78)
    print("선언된 값 — GET /v1/models")
    print("=" * 78)
    status, body = _get("/v1/models")
    declared = None
    if status == 200:
        try:
            declared = (json.loads(body).get("data") or [{}])[0].get("max_model_len")
        except (json.JSONDecodeError, IndexError, AttributeError):
            pass
    print(f"  HTTP {status}  max_model_len={declared}  (코드 상수: {SERVER_MAX_CONTEXT})")

    print()
    print("=" * 78)
    print("실측 — 한국어 토큰 비율")
    print("=" * 78)
    korean = "고혈압 환자의 목표 혈압은 수축기 130 mmHg 미만입니다. " * 40
    payload = {
        "model": SETTINGS.fm_model,
        "messages": [{"role": "user", "content": korean}],
        "max_tokens": 1, "temperature": 0,
    }
    request = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=HEADERS, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            usage = json.loads(response.read().decode("utf-8", "replace")).get("usage") or {}
        tokens = usage.get("prompt_tokens") or 0
        if tokens:
            print(f"  한국어 {len(korean):,}자 -> {tokens:,} tok  ({len(korean)/tokens:.2f} 자/토큰)")
    except Exception as exc:  # noqa: BLE001
        print(f"  실패: {exc}")

    print()
    print("=" * 78)
    print("실측 — 컨텍스트 경계 (선언값 바로 아래 / 바로 위)")
    print("=" * 78)
    for chars, label in ((262_000, "선언값 바로 아래"), (280_000, "선언값 초과")):
        result = _chat(chars)
        print(f"  {chars:>9,}자 -> HTTP {result['status']}"
              f"  prompt_tokens={result.get('usage', {}).get('prompt_tokens')}  ({label})")
        if result.get("error"):
            print(f"      {result['error'][:220]}")

    print()
    print("=" * 78)
    print("실측 — 출력 상한")
    print("=" * 78)
    result = _chat(100, max_tokens=SERVER_MAX_TOKENS + 1)
    print(f"  max_tokens={SERVER_MAX_TOKENS + 1} -> HTTP {result['status']}  {result.get('error', '')[:200]}")


def deep() -> None:
    print()
    print("=" * 78)
    print("이분탐색 — 메시지 바이트 상한")
    print("=" * 78)
    low, high = 280_000, 1_200_000
    for _ in range(7):
        mid = (low + high) // 2
        result = _chat(mid)
        code = ""
        if result.get("error"):
            try:
                code = json.loads(result["error"]).get("error", {}).get("code", "")
            except (json.JSONDecodeError, AttributeError):
                code = "nginx-413" if result["status"] == 413 else ""
        print(f"  {mid:>9,}자 ({result['bytes']/1024:>6.0f} KB) -> HTTP {result['status']} {code}")
        if result["status"] == 200:
            low = mid
        else:
            high = mid
    print(f"  => 경계는 {low:,} ~ {high:,}자 사이")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep", action="store_true", help="이분탐색까지 (호출 수가 는다)")
    args = parser.parse_args()
    if not SETTINGS.fm_api_key:
        print("키가 없다. LUNIT_FM_API_KEY 를 설정하거나 submission/config.py 를 확인하라.")
        return 1
    quick()
    if args.deep:
        deep()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
