"""OpenAI 호환 서버 — 우리 챗봇을 CoEval이 부를 수 있게 감싼다.

★ 이게 제출물의 실체다.

CoEval(https://github.com/lunit-io/CoEval)은 평가 대상을 **OpenAI 호환 엔드포인트**로 호출한다.

    mise run eval -- datasets=healthbench_consensus \\
        client.api_base=http://localhost:8080/v1 \\
        client.model=medai

즉 우리가 제출할 것은 Python 함수가 아니라 `/v1/chat/completions` 를 노출하는 서버다.

⚠️ 무상태(stateless) 설계가 핵심
  CoEval 은 매 호출에 대화 전체를 messages 로 보낸다. 서버가 세션을 들고 있으면
  동시 평가 시 서로 섞인다. 그래서 매 요청마다 messages 를 재생(replay)해서
  SessionState 를 새로 만든다. 재생은 규칙 기반이라 LLM 호출이 없고 결정론적이다.

실행:
    python serve.py                          # 기본 8080 포트, mock 설정
    python serve.py --config configs/live.yaml --port 8080
    curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \\
      -d '{"model":"medai","messages":[{"role":"user","content":"머리가 아파요"}]}' | jq -r '.choices[0].message.content'

의존성 없음 (stdlib http.server). FastAPI 를 쓰고 싶으면 이 파일을 참고해 바꿔도 되지만,
해커톤에서 의존성 하나 줄이는 게 사고 하나 줄이는 것이다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from medai import config as cfgmod          # noqa: E402
from medai import entities as ent           # noqa: E402
from medai import session as sess           # noqa: E402
from medai.contracts import SessionState    # noqa: E402
from medai.pipeline import Pipeline         # noqa: E402

MODEL_NAME = "medai"

_PIPE: Pipeline | None = None
_CFG = None
_LOOP: asyncio.AbstractEventLoop | None = None
_TURN_TIMEOUT: float = 300.0     # main() 에서 config 값으로 덮는다


# ─────────────────────────────────────────────────────────────
# 무상태 세션 복원
# ─────────────────────────────────────────────────────────────
def session_from_messages(messages: list[dict]) -> tuple[SessionState, str]:
    """messages 전체를 재생해 세션 슬롯을 복원하고, 마지막 user 발화를 반환한다.

    CoEval 은 턴마다 대화 전체를 보내므로 서버가 상태를 들고 있으면 안 된다.
    (동시 평가 시 세션이 서로 섞인다)

    복원은 L1 규칙 추출만 쓴다 — LLM 호출 없이 결정론적이고 빠르다.
    이 덕분에 "1턴에 흘린 음주"가 3턴 약 추천에 반영되는 동작이 무상태에서도 유지된다.
    """
    s = SessionState()
    last_user = ""

    for m in messages:
        role = m.get("role", "")
        content = str(m.get("content", "") or "")
        if role == "system":
            continue
        if role == "user":
            last_user = content

    # 마지막 user 발화를 제외한 나머지를 이력 + 슬롯으로 재생
    idx_last_user = max(
        (i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1
    )
    for i, m in enumerate(messages):
        role, content = m.get("role", ""), str(m.get("content", "") or "")
        if role == "system" or i == idx_last_user:
            continue
        s.history.append({"role": role, "content": content[:1200]})
        if role != "user":
            continue

        # 규칙 기반 슬롯 복원
        if s.age is None:
            a = ent.extract_age(content)
            if a is not None:
                s.age = a
        have = {r.type for r in s.risk_factors}
        for r in ent.extract_risk_factors(content, s.age):
            if r.type not in have:
                s.risk_factors.append(r)
                have.add(r.type)
                if r.type == "pregnancy":
                    s.pregnant = True
        if not s.symptom_duration:
            t = ent.extract_temporal(content)
            if t:
                s.symptom_duration = t
        # 복용 중이라고 밝힌 약만 승격 (문의는 제외)
        if sess._TAKING.search(content):
            for name in ent.rule_extract_drugs(content):
                if name not in s.medication_names:
                    s.medication_names.append(name)
                for c in ent.product_to_ingredients().get(ent.key(name), []):
                    if c not in s.medications:
                        s.medications.append(c)

    s.history = s.history[-8:]
    return s, last_user


# ─────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # 기본 stderr 로깅 억제
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            return self._send(200, {
                "object": "list",
                "data": [{"id": MODEL_NAME, "object": "model",
                          "created": 0, "owned_by": "team"}],
            })
        if self.path.rstrip("/") in ("/health", "/healthz"):
            return self._send(200, {"status": "ok"})
        return self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            return self._send(404, {"error": {"message": "not found"}})

        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"error": {"message": f"bad request: {e}"}})

        messages = req.get("messages") or []
        if not messages:
            return self._send(400, {"error": {"message": "messages required"}})

        fut = None
        try:
            session, text = session_from_messages(messages)
            fut = asyncio.run_coroutine_threadsafe(
                _PIPE.run_turn(text, session), _LOOP  # type: ignore[arg-type]
            )
            # 상한은 config 다 (server.turn_timeout). 하드코딩하지 않는다 —
            # 짧게 잡으면 '느린 답'이 '날아간 답'이 되고, 폴백 문구는 그 문항의
            # 가점을 전부 놓친다. 근거: docs/spec.md §5.2 (CoEval timeout 360)
            res = fut.result(timeout=_TURN_TIMEOUT)
            answer = res.answer
            trace = res.trace
        except Exception as e:
            # 타임아웃이면 코루틴이 백그라운드 루프에 계속 남는다. 취소해서
            # 다음 요청의 예산을 갉아먹지 않게 한다.
            if fut is not None and not fut.done():
                fut.cancel()
            # 평가 중 500을 내면 그 문항이 통째로 날아간다.
            # 실패해도 안전한 문자열을 돌려주는 편이 낫다.
            answer = ("죄송합니다. 일시적인 오류로 정확한 안내를 드리지 못했습니다. "
                      "증상이 지속되거나 악화되면 가까운 의료기관에서 진료를 받아보세요.")
            trace = {"error": repr(e)}

        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        self._send(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model") or MODEL_NAME,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
            # 토큰 카운트는 근사치. CoEval 이 쓰지 않지만 스키마를 맞춘다.
            "usage": {
                "prompt_tokens": int(prompt_chars * 0.7),
                "completion_tokens": int(len(answer) * 0.7),
                "total_tokens": int((prompt_chars + len(answer)) * 0.7),
            },
            "medai_trace": trace,   # 디버깅용 확장 필드 (표준 클라이언트는 무시한다)
        })


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    global _PIPE, _CFG, _LOOP, _TURN_TIMEOUT
    ap = argparse.ArgumentParser(description="OpenAI 호환 서버 (제출물)")
    # ★ 제출 규정: 컨테이너는 수동 작업 없이 0.0.0.0:8000 에서 서비스해야 한다.
    #   그래서 기본값을 8000 / configs/l2_live.yaml 로 두고, 환경변수로 덮을 수 있게 한다.
    #   (Dockerfile 의 CMD 는 인자 없이 `python serve.py` 만 부른다)
    ap.add_argument("--config", default=os.getenv("MEDAI_CONFIG", "configs/l2_live.yaml"))
    ap.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    a = ap.parse_args()

    _CFG = cfgmod.load(a.config)
    _PIPE = Pipeline(_CFG)
    _TURN_TIMEOUT = float(_CFG["server"]["turn_timeout"])

    # 파이프라인은 async 라 백그라운드 이벤트 루프에서 돌리고,
    # HTTP 스레드는 run_coroutine_threadsafe 로 결과를 받는다.
    _LOOP = asyncio.new_event_loop()
    threading.Thread(target=_run_loop, args=(_LOOP,), daemon=True).start()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("=" * 66)
    print(f"  OpenAI 호환 서버 · {a.host}:{a.port}")
    print(f"  config      : {a.config} ({_CFG.get('name')})")
    print(f"  retrieval   : {_CFG['retrieval']['mode']}")
    print(f"  FM base_url : {_CFG['llm']['base_url'] or '(미설정 — 오프라인 스텁)'}")
    print(f"  turn timeout: {_TURN_TIMEOUT:.0f}s (conquer_val/test 클라이언트는 180s)")
    print()
    print("  CoEval 연결:")
    print(f"    mise run eval -- datasets=healthbench_consensus \\")
    print(f"        client.api_base=http://<이-호스트>:{a.port}/v1 \\")
    print(f"        client.model={MODEL_NAME}")
    print("=" * 66)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    main()
