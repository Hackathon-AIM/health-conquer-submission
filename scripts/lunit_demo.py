#!/usr/bin/env python3
"""로컬 제출 서버를 띄워 수동 또는 공식 Patient Simulator 대화를 실행한다."""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def post(url: str, payload: dict[str, Any], key: str = "", retries: int = 2) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=180) as response:
                value = json.loads(response.read().decode("utf-8"))
            if not isinstance(value, dict):
                raise RuntimeError("API가 JSON object가 아닌 값을 반환했습니다.")
            return value
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code != 502 or attempt == retries:
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise RuntimeError(str(exc)) from exc
        time.sleep(attempt + 1)
    raise RuntimeError("API 요청 재시도 한도를 초과했습니다.")


def content(response: dict[str, Any]) -> str:
    try:
        text = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"예상하지 못한 Chat Completions 응답: {list(response)[:8]}") from exc
    return str(text or "").strip()


def wait_until_ready(url: str, process: subprocess.Popen[bytes], seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"로컬 서버가 시작 중 종료됐습니다(exit={process.returncode}).")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise RuntimeError("로컬 서버가 제한 시간 안에 준비되지 않았습니다.")


def print_turn(role: str, text: str, elapsed: float | None = None) -> None:
    suffix = f" · {elapsed:.1f}s" if elapsed is not None else ""
    print(f"\n{'─' * 12} {role}{suffix} {'─' * 12}")
    print(text)


def read_manual_message() -> str:
    first = input("\n환자> ").strip()
    if first != "/paste":
        return first
    print("여러 줄을 입력하세요. 단독으로 . 을 입력하면 전송합니다.")
    lines: list[str] = []
    while True:
        line = input()
        if line == ".":
            return "\n".join(lines).strip()
        lines.append(line)


def manual_mode(harness_url: str) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    print(
        "\nLunit evaluator 수동 모드\n"
        "질문을 입력하면 전체 history가 매번 제출 API로 전송됩니다.\n"
        "명령: /paste  /history  /json  /reset  /quit"
    )
    while True:
        user_text = read_manual_message()
        if not user_text:
            continue
        if user_text == "/quit":
            return history
        if user_text == "/reset":
            history.clear()
            print("대화가 초기화됐습니다.")
            continue
        if user_text == "/history":
            for message in history:
                print_turn(message["role"].upper(), message["content"])
            continue
        if user_text == "/json":
            print(json.dumps(history, ensure_ascii=False, indent=2))
            continue

        history.append({"role": "user", "content": user_text})
        started = time.monotonic()
        response = post(harness_url, {"model": "conquer-health-l2-native", "messages": history})
        assistant_text = content(response)
        elapsed = time.monotonic() - started
        history.append({"role": "assistant", "content": assistant_text})
        print_turn("AIM", assistant_text, elapsed)


def patient_mode(harness_url: str, key: str, turns: int) -> list[dict[str, str]]:
    patient_url = "https://patient.hackathon.lunit.io/v1/chat/completions"
    history: list[dict[str, str]] = []
    print(f"\n공식 Patient Simulator {turns}턴 모드")
    for turn in range(1, turns + 1):
        started = time.monotonic()
        try:
            patient = post(patient_url, {"model": "patient-simulator-ko", "messages": history}, key)
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            history = []
            patient = post(patient_url, {"model": "patient-simulator-ko", "messages": []}, key)
        user_text = content(patient)
        history.append({"role": "user", "content": user_text})
        print_turn(f"PATIENT {turn}", user_text, time.monotonic() - started)

        started = time.monotonic()
        answer = post(harness_url, {"model": "conquer-health-l2-native", "messages": history})
        assistant_text = content(answer)
        history.append({"role": "assistant", "content": assistant_text})
        print_turn(f"AIM {turn}", assistant_text, time.monotonic() - started)
    return history


def save_history(path: str, history: list[dict[str, str]]) -> None:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n대화 기록 저장: {destination}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lunit 평가 흐름을 로컬 터미널에서 재현합니다.")
    parser.add_argument("mode", nargs="?", choices=("manual", "patient"), default="manual")
    parser.add_argument("--turns", type=int, default=3, help="patient 모드 턴 수(기본 3)")
    parser.add_argument("--port", type=int, default=8000, help="자동 기동할 로컬 포트(기본 8000)")
    parser.add_argument("--save", help="종료 시 transcript를 저장할 JSON 경로")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    key = os.environ.get("LUNIT_FM_API_KEY", "") or getpass.getpass("LUNIT_FM_API_KEY (화면에 표시되지 않음): ")
    if not key:
        print("API key가 필요합니다.", file=sys.stderr)
        return 2

    environment = dict(os.environ)
    environment["LUNIT_FM_API_KEY"] = key
    if importlib.util.find_spec("uvicorn") is not None:
        server_command = [sys.executable, "-m", "uvicorn"]
    elif shutil.which("uvicorn"):
        server_command = [str(shutil.which("uvicorn"))]
    else:
        print("uvicorn이 없습니다. 먼저 pip install -r requirements.txt 를 실행하세요.", file=sys.stderr)
        return 2
    process = subprocess.Popen(
        [
            *server_command,
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=environment,
    )
    harness_url = f"http://127.0.0.1:{args.port}/v1/chat/completions"
    try:
        wait_until_ready(f"http://127.0.0.1:{args.port}/health", process)
        history = (
            manual_mode(harness_url)
            if args.mode == "manual"
            else patient_mode(harness_url, key, max(1, args.turns))
        )
        if args.save:
            save_history(args.save, history)
        return 0
    except (RuntimeError, KeyboardInterrupt, EOFError) as exc:
        if not isinstance(exc, (KeyboardInterrupt, EOFError)):
            print(f"\n오류: {exc}", file=sys.stderr)
            return 1
        print("\n종료합니다.")
        return 130
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
