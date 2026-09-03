#!/usr/bin/env python3
"""키를 저장하지 않고 Docker 웹 데모를 빌드·실행하고 브라우저를 연다."""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIM 제출물의 Docker 웹 채팅을 실행합니다.")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--no-build", action="store_true", help="기존 aim-submission:local 이미지를 사용")
    return parser.parse_args()


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_ready(url: str, process: subprocess.Popen[bytes], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Docker container가 종료됐습니다(exit={process.returncode}).")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                page = response.read().decode("utf-8", errors="replace")
                if response.status == 200 and "AIM · Lunit L2 Demo" in page:
                    if process.poll() is not None:
                        raise RuntimeError(f"Docker container가 종료됐습니다(exit={process.returncode}).")
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.25)
    raise RuntimeError("웹 서버가 제한 시간 안에 준비되지 않았습니다.")


def main() -> int:
    args = parse_args()
    key = os.environ.get("LUNIT_FM_API_KEY", "") or getpass.getpass("LUNIT_FM_API_KEY (표시·저장되지 않음): ")
    if not key:
        print("API key가 필요합니다.", file=sys.stderr)
        return 2
    if not port_available(args.port):
        print(
            f"127.0.0.1:{args.port} 포트를 다른 서비스가 사용 중입니다. "
            f"--port {args.port + 1} 처럼 다른 포트를 지정하세요.",
            file=sys.stderr,
        )
        return 2
    if not args.no_build:
        completed = subprocess.run(
            ["docker", "build", "-t", "aim-submission:local", "."],
            cwd=REPO_ROOT,
            check=False,
        )
        if completed.returncode:
            return completed.returncode

    environment = dict(os.environ)
    environment["LUNIT_FM_API_KEY"] = key
    process = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "-p",
            f"127.0.0.1:{args.port}:8000",
            "-e",
            "LUNIT_FM_API_KEY",
            "aim-submission:local",
        ],
        cwd=REPO_ROOT,
        env=environment,
    )
    url = f"http://127.0.0.1:{args.port}"
    try:
        wait_ready(url, process)
        print(f"\n웹 데모: {url}")
        print("종료하려면 이 터미널에서 Ctrl+C를 누르세요.\n")
        webbrowser.open(url)
        return process.wait()
    except (RuntimeError, KeyboardInterrupt) as exc:
        if isinstance(exc, RuntimeError):
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
