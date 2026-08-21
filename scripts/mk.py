#!/usr/bin/env python
"""Makefile 대체 — Windows/macOS/Linux 공용 태스크 러너.

이 머신에는 `make` 가 없다. 문서에 `make X` 라고 쓰인 것은 전부

    python scripts/mk.py X

로 읽는다. 타깃 정의는 Makefile 과 1:1 로 맞춘다. 한쪽만 고치지 말 것.

    python scripts/mk.py list        # 타깃 목록
    python scripts/mk.py test        # 실행
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Windows 콘솔 기본 코드페이지(cp949)가 ✅/❌ 를 못 찍어 UnicodeEncodeError 로 죽는다.
# 자기 자신과 자식 프로세스 양쪽을 UTF-8 로 고정한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def venv_python() -> str:
    """.venv 안의 파이썬. 없으면 현재 인터프리터."""
    for rel in ("Scripts/python.exe", "bin/python"):
        p = ROOT / ".venv" / rel
        if p.exists():
            return str(p)
    return sys.executable


PY = venv_python()

# 타깃 → 명령 리스트. 각 명령은 argv 리스트다 (shell=False — 경로 공백 안전).
TARGETS: dict[str, tuple[str, list[list[str]]]] = {
    # ── 기본 ────────────────────────────────────────────────
    "setup": ("의존성 설치 (uv 우선, 없으면 pip)", []),  # 아래에서 특수 처리
    "test": ("단위 테스트", [[PY, "-m", "pytest", "tests/", "-q"]]),
    "audit": ("배선 감사 — 다이어그램 ↔ 코드 대조", [[PY, "scripts/audit_e2e.py"]]),
    "dicts": ("사전 생성", [[PY, "data/build_dicts.py", "--seed"]]),
    "clean": ("로그·캐시 삭제", []),  # 특수 처리

    # ── 실행 ────────────────────────────────────────────────
    "run": ("mock 하네스", [[PY, "-m", "eval.harness", "--config", "configs/mock.yaml"]]),
    "live": ("live 하네스", [[PY, "-m", "eval.harness", "--config", "configs/live.yaml"]]),
    "smoke": ("현장 첫 60분 점검", [[PY, "scripts/smoke.py", "configs/live.yaml"]]),

    # ── 제출물 서버 (평가 경로) ─────────────────────────────
    "serve": ("제출물 서버 — configs/live.yaml", [[PY, "serve.py", "--config", "configs/live.yaml", "--port", "8080"]]),
    "serve-mock": ("제출물 서버 — mock", [[PY, "serve.py", "--config", "configs/mock.yaml", "--port", "8080"]]),
    "serve-l2": ("제출물 서버 — configs/l2_live.yaml (본선)", [[PY, "serve.py", "--config", "configs/l2_live.yaml", "--port", "8080"]]),
    "serve-check": ("서버 스키마 확인 (다른 터미널에서)", []),  # 특수 처리

    # ── L2 / MCP ────────────────────────────────────────────
    "probe": ("MCP 실측 — 페이지 크기·cite_uid·지연", [[PY, "scripts/probe_mcp.py"]]),
    "l2-smoke": ("L2 전 구간 스모크", [[PY, "scripts/l2_smoke.py"]]),
    "chat": ("직접 대화 (멀티턴·추적 표시)", [[PY, "scripts/chat.py"]]),
    "sim": ("Patient Simulator 대화 기록 → logs/sim/", [[PY, "scripts/sim_loop.py", "--n", "3", "--turns", "3"]]),

    # ── 점수 ────────────────────────────────────────────────
    "hb": ("로컬 HealthBench — mock 20문항", [[PY, "-m", "eval.healthbench", "--variant", "consensus", "--config", "configs/mock.yaml", "--limit", "20"]]),
    "hb-live": ("로컬 HealthBench — live 100문항", [[PY, "-m", "eval.healthbench", "--variant", "consensus", "--config", "configs/live.yaml", "--limit", "100"]]),
    "ab": ("A/B — v1_no_critic vs v2_full", [
        [PY, "-m", "eval.harness", "--config", "configs/v1_no_critic.yaml"],
        [PY, "-m", "eval.harness", "--config", "configs/v2_full.yaml"],
    ]),
    "ab-emergency": ("A/B — 응급 검색 우회 가설", [
        [PY, "-m", "eval.harness", "--config", "configs/emergency_bypass.yaml"],
        [PY, "-m", "eval.harness", "--config", "configs/emergency_guideline.yaml"],
    ]),
    "ab-baseline": ("★ 기준선 — l2_raw vs l2_live (같은 50문항)", [
        [PY, "-m", "eval.healthbench", "--variant", "consensus", "--config", "configs/l2_raw.yaml", "--limit", "50"],
        [PY, "-m", "eval.healthbench", "--variant", "consensus", "--config", "configs/l2_live.yaml", "--limit", "50"],
    ]),

    # ── 제출 ────────────────────────────────────────────────
    "submit-check": ("제출 규격 정적 점검", [[PY, "scripts/submit_check.py"]]),
    "submit-check-docker": ("제출 규격 + 도커 빌드·기동 (빌드 시간 측정)", [[PY, "scripts/submit_check.py", "--docker"]]),
    "docker-build": ("이미지 빌드", [["docker", "build", "-t", "medai:local", "."]]),
    "docker-run": ("컨테이너 기동 (0.0.0.0:8000)", [["docker", "run", "--rm", "-p", "8000:8000", "medai:local"]]),
}

# 폐기된 타깃 — 대회 규칙 7 (HealthBench 역공학 금지)
RETIRED = {
    "redflags": "HealthBench 역추출은 대회 규칙 위반 (역공학 금지, 수상 자격 박탈). "
                "data/redflags.yaml 은 응급의학 일반 기준으로만 관리한다. docs/spec.md §0",
    "redflags-dump": "위와 같음",
    "redflags-validate": "위와 같음",
}


def run(cmds: list[list[str]]) -> int:
    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    for cmd in cmds:
        print(f"\n$ {' '.join(cmd)}", flush=True)
        rc = subprocess.call(cmd, cwd=ROOT, env=env)
        if rc != 0:
            return rc
    return 0


def do_setup() -> int:
    req = "requirements-serve.txt"
    if shutil.which("uv"):
        vpy = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cmds = []
        if not vpy.exists():
            cmds.append(["uv", "venv", "--python", "3.12", ".venv"])
        cmds.append(["uv", "pip", "install", "--python", str(vpy), "-r", req, "pytest"])
        return run(cmds)
    return run([[sys.executable, "-m", "pip", "install", "-r", req, "pytest"]])


def do_clean() -> int:
    for pat in ("logs/*.json", "logs/*.jsonl"):
        for p in ROOT.glob(pat):
            p.unlink()
    for p in ROOT.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    print("cleaned")
    return 0


def do_serve_check() -> int:
    import json
    import urllib.request

    base = os.environ.get("BASE", "http://localhost:8080")
    print(f"--- GET {base}/v1/models ---")
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=10) as r:
            print(r.read().decode())
    except Exception as e:
        print(f"실패: {e}\n  서버가 떠 있는지 확인: python scripts/mk.py serve-l2")
        return 1
    print(f"--- POST {base}/v1/chat/completions ---")
    body = json.dumps({"model": "medai", "messages": [{"role": "user", "content": "머리가 아파요"}]}).encode()
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=360) as r:
        d = json.loads(r.read().decode())
    print(d["choices"][0]["message"]["content"][:300])
    return 0


SPECIAL = {"setup": do_setup, "clean": do_clean, "serve-check": do_serve_check}


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("list", "help", "-h", "--help"):
        print(f"python scripts/mk.py <target>      (python: {PY})\n")
        for name, (desc, _) in TARGETS.items():
            print(f"  {name:22s} {desc}")
        print("\n폐기된 타깃: " + ", ".join(RETIRED))
        return 0

    target = args[0]
    if target in RETIRED:
        print(f"❌ 폐기됨: {RETIRED[target]}")
        return 1
    if target in SPECIAL:
        return SPECIAL[target]()
    if target not in TARGETS:
        print(f"알 수 없는 타깃: {target}\n`python scripts/mk.py list` 로 목록을 본다.")
        return 1
    return run(TARGETS[target][1])


if __name__ == "__main__":
    raise SystemExit(main())
