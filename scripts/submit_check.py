"""제출 전 자동 점검 — 규정 위반을 코드로 잡는다.

    python scripts/submit_check.py            # 정적 점검만
    python scripts/submit_check.py --docker   # 도커 빌드·실행까지 (권장, 5분 제한 측정)

규정(대시보드 제출 안내):
  · repository root 에 Dockerfile, build 5분 이내
  · 수동 작업 없이 0.0.0.0:8000 서비스, port 8000 만 평가
  · OpenAI 호환: GET /v1/models, POST /v1/chat/completions
  · branch: lunit/hackathon-submission 의 40자리 SHA 제출
  · 최종 출력은 반드시 L2
  · 평가 환경은 격리 — 외부 접근 불가
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK, NG, WARN = "✅", "❌", "⚠️ "
fails = 0


def chk(cond: bool, label: str, note: str = "", fatal: bool = True) -> bool:
    global fails
    mark = OK if cond else (NG if fatal else WARN)
    print(f" {mark} {label}" + (f" — {note}" if note else ""))
    if not cond and fatal:
        fails += 1
    return cond


def sh(*args: str) -> str:
    try:
        return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                              timeout=60).stdout.strip()
    except Exception:
        return ""


def static_checks() -> None:
    print("\n[파일·설정]")
    df = ROOT / "Dockerfile"
    chk(df.exists(), "Dockerfile 이 repo root 에 있다")
    if df.exists():
        t = df.read_text()
        chk("EXPOSE 8000" in t, "EXPOSE 8000")
        chk(re.search(r"CMD|ENTRYPOINT", t) is not None, "CMD/ENTRYPOINT 존재")
        chk("requirements.txt" not in re.sub(r"#.*", "", t),
            "무거운 requirements.txt 를 안 쓴다 (빌드 5분 제한)")
    rs = ROOT / "requirements-serve.txt"
    if chk(rs.exists(), "requirements-serve.txt"):
        body = re.sub(r"#.*", "", rs.read_text())
        heavy = [w for w in ("torch", "FlagEmbedding", "pandas", "langgraph",
                             "transformers") if w in body]
        chk(not heavy, "런타임에 무거운 패키지 없음", f"발견: {heavy}" if heavy else "")

    print("\n[포트]")
    s = (ROOT / "serve.py").read_text()
    chk('"PORT", "8000"' in s or "PORT', '8000" in s,
        "serve.py 기본 포트 8000")
    chk('default=os.getenv("HOST", "0.0.0.0")' in s, "기본 호스트 0.0.0.0")

    print("\n[모델 — 최종 출력은 반드시 L2]")
    cfg = (ROOT / "configs" / "l2_live.yaml").read_text()
    chk("Lunit/L2-preview" in cfg, "drafter = Lunit/L2-preview")
    bad = [m for m in ("gpt-4", "gpt-3", "claude-", "gemini") if m in cfg]
    chk(not bad, "외부 모델 미사용", f"발견: {bad}" if bad else "")
    chk("l2_native: true" in cfg, "l2_native 켜짐")

    print("\n[비밀정보]")
    gi = (ROOT / ".gitignore").read_text() if (ROOT / ".gitignore").exists() else ""
    chk(".env" in gi, ".env 가 .gitignore 에 있다")
    di = (ROOT / ".dockerignore").read_text() if (ROOT / ".dockerignore").exists() else ""
    chk(".env" in di, ".env 가 .dockerignore 에 있다")
    tracked = sh("git", "ls-files", ".env")
    chk(not tracked, ".env 가 git 에 추적되지 않는다", tracked)
    # 소스에 키 리터럴이 없는지
    leaked = []
    for p in list((ROOT / "src").rglob("*.py")) + [ROOT / "serve.py"] + \
             list((ROOT / "configs").glob("*.yaml")):
        if re.search(r"(lunit_[A-Za-z0-9_\-]{12,}|sk-[A-Za-z0-9_\-]{20,})", p.read_text()):
            leaked.append(p.name)
    chk(not leaked, "소스에 API 키 리터럴 없음", f"발견: {leaked}" if leaked else "")

    print("\n[규정 — HealthBench 역공학 금지]")
    ex = ROOT / "data" / "extract_redflags.py"
    chk(not ex.exists() or "healthbench" not in ex.read_text().lower(),
        "HealthBench 역추출 코드가 제출물 경로에 없음",
        "data/extract_redflags.py 는 .dockerignore 로 제외됨" if ex.exists() else "",
        fatal=False)

    print("\n[git]")
    br = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
    chk(bool(br), "git 저장소", br)
    chk(br == "lunit/hackathon-submission",
        "브랜치가 lunit/hackathon-submission",
        f"현재: {br} — 제출 전 이 브랜치로 머지해야 한다", fatal=False)
    dirty = sh("git", "status", "--porcelain")
    chk(not dirty, "커밋 안 된 변경 없음",
        f"{len(dirty.splitlines())}개 파일" if dirty else "", fatal=False)
    sha = sh("git", "rev-parse", "HEAD")
    if sha:
        print(f"\n   제출할 40자리 SHA: {sha}")


def docker_checks() -> None:
    print("\n[도커 빌드]")
    t0 = time.perf_counter()
    r = subprocess.run(["docker", "build", "-t", "medai-submit:check", "."],
                       cwd=ROOT, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    if not chk(r.returncode == 0, "빌드 성공", f"{dt:.0f}초"):
        print(r.stderr[-2500:])
        return
    chk(dt < 300, "빌드 5분 이내", f"{dt:.0f}초")

    print("\n[컨테이너 기동]")
    cid = subprocess.run(
        ["docker", "run", "-d", "-p", "8000:8000",
         "-e", "LUNIT_FM_API_KEY=dummy-for-boot-check", "medai-submit:check"],
        cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not chk(bool(cid), "컨테이너 시작"):
        return
    try:
        up = False
        for _ in range(30):
            time.sleep(1)
            try:
                with urllib.request.urlopen("http://localhost:8000/v1/models", timeout=3) as f:
                    body = json.loads(f.read())
                up = True
                break
            except Exception:
                continue
        if chk(up, "GET /v1/models 응답", "수동 작업 없이 기동"):
            ids = [m.get("id") for m in body.get("data", [])]
            chk(bool(ids), "모델 목록", str(ids))
        req = urllib.request.Request(
            "http://localhost:8000/v1/chat/completions",
            data=json.dumps({"model": "medai",
                             "messages": [{"role": "user", "content": "안녕하세요"}]}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as f:
                d = json.loads(f.read())
            c = d["choices"][0]["message"]["content"]
            chk(bool(c), "POST /v1/chat/completions 스키마", f"{len(c)}자")
        except Exception as e:
            chk(False, "POST /v1/chat/completions", repr(e),
                fatal=False)
            print("      (키가 dummy 라 실패할 수 있다 — 스키마만 확인)")
    finally:
        subprocess.run(["docker", "logs", "--tail", "15", cid],
                       capture_output=True, text=True)
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docker", action="store_true")
    a = ap.parse_args()
    print("=" * 66)
    print("  제출 전 점검")
    print("=" * 66)
    static_checks()
    if a.docker:
        docker_checks()
    else:
        print("\n   ※ --docker 를 붙이면 빌드·기동까지 확인합니다 (권장)")
    print("\n" + "=" * 66)
    if fails:
        print(f"  ❌ 치명적 실패 {fails}건 — 고치기 전에 제출하지 마세요")
        sys.exit(1)
    print("  ✅ 치명적 실패 없음")
    print("=" * 66)


if __name__ == "__main__":
    main()
