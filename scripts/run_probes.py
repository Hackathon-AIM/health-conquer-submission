#!/usr/bin/env python3
"""고정 프로브 8개를 동일 endpoint에 실행하고 결과·회귀 diff를 저장한다."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROBES = ROOT / "evals" / "probes.json"


def post(url: str, messages: list[dict[str, str]]) -> str:
    payload = json.dumps(
        {"model": "conquer-health-l2-native", "messages": messages},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"예상하지 못한 응답: {list(data)[:8]}") from exc


def evaluate(text: str, checks: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for key, value in checks.items():
        if key.startswith("contains_any") and not any(item.lower() in text.lower() for item in value):
            failures.append(f"다음 중 하나가 없음: {value}")
        elif key == "contains_all":
            missing = [item for item in value if item.lower() not in text.lower()]
            if missing:
                failures.append(f"필수 표현 없음: {missing}")
        elif key == "not_contains":
            found = [item for item in value if item.lower() in text.lower()]
            if found:
                failures.append(f"금지 표현 발견: {found}")
        elif key == "not_match":
            found = [pattern for pattern in value if re.search(pattern, text, re.I | re.S)]
            if found:
                failures.append(f"금지 패턴 발견: {found}")
        elif key == "first_sentence_contains":
            first = re.split(r"(?<=[.!?。])\s+|\n", text, maxsplit=1)[0]
            if str(value).lower() not in first.lower():
                failures.append(f"첫 문장에 {value!r} 없음")
        elif key == "max_question_marks" and text.count("?") > int(value):
            failures.append(f"질문이 {text.count('?')}개로 상한 {value} 초과")
        elif key == "max_hangul_chars" and len(re.findall(r"[가-힣]", text)) > int(value):
            failures.append("영어 답변에 한글이 포함됨")
        elif key == "min_chars" and len(text) < int(value):
            failures.append(f"답변 길이 {len(text)}자가 최소 {value}자 미만")
        elif key == "max_chars" and len(text) > int(value):
            failures.append(f"답변 길이 {len(text)}자가 상한 {value}자 초과")
    return failures


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def result_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    for probe in result.get("probes", []):
        lines.append(f"## {probe['id']} {probe['title']}")
        for message in probe.get("history", []):
            lines.append(f"{message['role']}: {message['content']}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIM 고정 프로브 회귀 테스트")
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:18080/v1/chat/completions",
        help="실행 중인 Docker 제출 API",
    )
    parser.add_argument("--probes", type=Path, default=DEFAULT_PROBES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path, help="이전 결과 JSON과 응답 diff 출력")
    parser.add_argument("--only", nargs="+", help="실행할 probe ID만 지정")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    probes = load_json(args.probes)
    if args.only:
        wanted = set(args.only)
        probes = [probe for probe in probes if probe["id"] in wanted]
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    output = args.output or ROOT / "runs" / f"probes-{timestamp}.json"
    result: dict[str, Any] = {
        "created_at": datetime.now().astimezone().isoformat(),
        "endpoint": args.endpoint,
        "probes": [],
    }
    failed = 0

    for index, probe in enumerate(probes, 1):
        history: list[dict[str, str]] = []
        turn_times: list[float] = []
        print(f"\n[{index}/{len(probes)}] {probe['id']} · {probe['title']}")
        print(f"기준: {probe['criterion']}")
        try:
            for user_text in probe["turns"]:
                history.append({"role": "user", "content": user_text})
                started = time.monotonic()
                assistant_text = post(args.endpoint, history)
                turn_times.append(round(time.monotonic() - started, 3))
                history.append({"role": "assistant", "content": assistant_text})
            failures = evaluate(history[-1]["content"], probe.get("checks") or {})
        except RuntimeError as exc:
            failures = [f"요청 실패: {exc}"]
        status = "PASS" if not failures else "FAIL"
        failed += bool(failures)
        print(f"{status} · {sum(turn_times):.1f}s")
        if history and history[-1]["role"] == "assistant":
            print(history[-1]["content"])
        for failure in failures:
            print(f"  - {failure}")
        result["probes"].append({
            "id": probe["id"],
            "title": probe["title"],
            "criterion": probe["criterion"],
            "status": status,
            "failures": failures,
            "turn_seconds": turn_times,
            "history": history,
        })

    result["summary"] = {"total": len(probes), "passed": len(probes) - failed, "failed": failed}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n결과: {len(probes) - failed}/{len(probes)} PASS")
    print(f"저장: {output.resolve()}")

    if args.baseline:
        baseline = load_json(args.baseline)
        diff = difflib.unified_diff(
            result_text(baseline).splitlines(keepends=True),
            result_text(result).splitlines(keepends=True),
            fromfile=str(args.baseline),
            tofile=str(output),
        )
        print("\n" + "".join(diff))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
