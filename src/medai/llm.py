"""FM 클라이언트 — OpenAI 호환 엔드포인트 래퍼.

핵심 역할 3가지
  1. 역할별 모델 라우팅 (설정 한 줄로 교체 가능)
  2. JSON 출력 강제 + 실패 시 복구 (도메인 특화 모델의 최대 리스크)
  3. 모든 호출을 JSONL로 기록 (디버깅 속도가 곧 순위)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config, LOGS

try:
    from openai import AsyncOpenAI
except ImportError:  # 오프라인 스캐폴딩 시에도 import 가능하게
    AsyncOpenAI = None  # type: ignore


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

# L1 계열(learning-unit/L1-*)은 <think>...</think> 로 단계적 추론을 한 뒤 답한다.
# 안 벗기면 (1) think 안의 중괄호를 JSON 으로 오인하고
#           (2) 사고 과정이 그대로 사용자 답변으로 나간다.
_THINK = re.compile(r"<think>.*?</think>\s*", re.S | re.I)
_THINK_OPEN = re.compile(r"<think>.*$", re.S | re.I)   # 닫히지 않은 경우(토큰 초과)


def strip_think(text: str) -> str:
    """추론 블록을 제거한다.

    ⚠️ max_tokens 가 모자라면 </think> 가 안 닫힌 채 잘린다.
       그때는 열린 태그 이후를 통째로 버린다 — 사고 과정이 답변으로 새는 것보다 낫다.
       (모델 카드 권장: max_tokens 2048 이상)
    """
    out = _THINK.sub("", text)
    if "<think>" in out.lower():
        out = _THINK_OPEN.sub("", out)
    return out.strip()


def _extract_json(text: str) -> str:
    """모델이 코드펜스·잡담·추론 블록을 붙여도 JSON만 뽑아낸다."""
    text = strip_think(text)
    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    if start == -1:
        return text
    # 균형 잡힌 괄호까지만
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


class LLM:
    def __init__(self, cfg: Config, run_id: str | None = None):
        self.cfg = cfg
        self.models: dict[str, str] = dict(cfg["models"])
        lc = cfg["llm"]
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self._log_path = Path(LOGS) / f"llm_{self.run_id}.jsonl"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

        self.enabled = bool(lc.get("base_url")) and AsyncOpenAI is not None
        self.client = None
        if self.enabled:
            self.client = AsyncOpenAI(
                base_url=lc["base_url"],
                api_key=os.getenv(lc.get("api_key_env", "MEDAI_API_KEY"), "sk-noauth"),
                timeout=lc.get("timeout", 30.0),        # ⚠️ 기본 600초 방지
                max_retries=lc.get("max_retries", 1),
            )

    # ── 로깅 ────────────────────────────────────────────────
    async def _log(self, rec: dict[str, Any]) -> None:
        async with self._lock:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ── 기본 호출 ───────────────────────────────────────────
    async def chat(
        self,
        role: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        model = self.models.get(role, self.models["drafter"])
        lc = self.cfg["llm"]
        t0 = time.perf_counter()

        if not self.enabled:
            out = _offline_stub(role, messages)
            await self._log({"role": role, "model": "OFFLINE", "ms": 0, "out": out[:400]})
            return out

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": lc.get("temperature", 0.2) if temperature is None else temperature,
            "max_tokens": lc.get("max_tokens", 1200) if max_tokens is None else max_tokens,
        }
        if json_mode:
            # 지원하지 않는 엔드포인트면 아래 except에서 무시하고 재시도
            kwargs["response_format"] = {"type": "json_object"}

        try:
            r = await self.client.chat.completions.create(**kwargs)  # type: ignore
        except Exception as e:
            if json_mode:
                kwargs.pop("response_format", None)
                r = await self.client.chat.completions.create(**kwargs)  # type: ignore
            else:
                await self._log({"role": role, "model": model, "error": repr(e)})
                raise

        raw_out = (r.choices[0].message.content or "").strip()
        out = strip_think(raw_out) if self.cfg["llm"].get("strip_think", True) else raw_out
        thought = len(raw_out) - len(out)
        await self._log({
            "role": role, "model": model,
            "ms": int((time.perf_counter() - t0) * 1000),
            "prompt_chars": sum(len(m["content"]) for m in messages),
            "think_chars": thought,     # 추론에 얼마나 썼는지 — 비용·지연 진단용
            "out": out[:2000],
        })
        return out

    # ── JSON 호출 (재시도 + 복구) ────────────────────────────
    async def chat_json(
        self,
        role: str,
        messages: list[dict[str, str]],
        *,
        attempts: int = 2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        last = ""
        for i in range(attempts):
            raw = await self.chat(
                role,
                messages,
                temperature=0.0 if i > 0 else None,   # 재시도는 결정론적으로
                max_tokens=max_tokens,
                json_mode=True,
            )
            last = raw
            try:
                return json.loads(_extract_json(raw))
            except Exception:
                if i + 1 < attempts:
                    messages = messages + [
                        {"role": "assistant", "content": raw[:500]},
                        {"role": "user", "content": "위 출력이 유효한 JSON이 아닙니다. JSON 객체만 다시 출력하세요."},
                    ]
        await self._log({"role": role, "json_parse_failed": True, "out": last[:1000]})
        return {}


def _offline_stub(role: str, messages: list[dict[str, str]]) -> str:
    """base_url이 비어 있을 때도 파이프라인 전체가 돌게 하는 오프라인 스텁.

    현장에서 엔드포인트를 못 뚫어도 A·C·D가 계속 작업할 수 있게 하는 장치.
    키워드 기반의 조잡한 분류지만, 라우팅·게이트·비평 경로를 실제로 태우므로
    엔드포인트 없이도 각 계층의 동작을 눈으로 확인할 수 있다.
    """
    user = messages[-1]["content"] if messages else ""

    if role == "classifier":
        drug_words = ["타이레놀", "아스피린", "이부프로펜", "약", "진통제", "감기약"]
        has_drug = any(w in user for w in drug_words)
        policy = any(w in user for w in ["보험", "급여", "본인부담", "실비", "건강검진"])
        alcohol = any(w in user for w in ["음주", "숙취", "취했", "회식"]) or \
            re.search(r"(?<![가-힣])술", user) is not None
        recommend = any(w in user for w in ["뭐 먹", "뭘 먹", "먹어야", "추천"])
        dose_q = any(w in user for w in ["몇 알", "최대", "용량", "얼마나"])

        if policy:
            intent, queries = "policy", {"law": user[:60], "hira": user[:60]}
        elif has_drug and dose_q:
            intent, queries = "drug_safety", {"drug": user[:60]}
        elif recommend or alcohol:
            intent, queries = "drug_recommend", {"guideline": user[:60], "drug": user[:60]}
        else:
            intent, queries = "symptom_consult", {"guideline": user[:60]}

        risks = [{"type": "alcohol", "when": "어제", "detail": "음주"}] if alcohol else []
        drugs = [{"name": w, "span": w} for w in drug_words[:3] if w in user and len(w) > 2]

        return json.dumps({
            "reasoning": f"[offline stub] 키워드 기반 분류: {intent}",
            "missing_info": [] if dose_q else ["증상 지속기간"],
            "why_it_changes_answer": "" if dose_q else "지속기간에 따라 응급도 판단이 달라짐",
            "intent": intent,
            "need_followup": not dose_q,
            "persona": "layperson",
            "entities": {"drugs": drugs, "symptoms": [], "risk_factors": risks, "temporal": None},
            "extra_sources": [],
            "queries": queries,
            "expects_drug_output": bool(has_drug or recommend or alcohol),
        }, ensure_ascii=False)

    if role in ("critic", "grader"):
        # HealthBench 채점 프롬프트인 경우 (eval/healthbench.py)
        if "criteria_met" in user:
            # 오프라인에서는 응답에 기준 키워드가 있으면 충족으로 본다 (구조 확인용)
            body = user.split("[기준]")[-1]
            crit = body.strip()[:40]
            comp = user.split("[AI 응답]")[-1].split("[기준]")[0]
            hit = any(w in comp for w in crit.replace("한다", "").split()[:3] if len(w) > 1)
            return json.dumps({"criteria_met": hit, "explanation": "[offline stub]"},
                              ensure_ascii=False)
        return json.dumps({"violations": [], "passed": True}, ensure_ascii=False)

    if role == "extractor":
        # 초안에 등장한 약을 잡아 L4b 경로를 실제로 태운다
        found = [w for w in ["아세트아미노펜", "이부프로펜", "해열진통제", "타이레놀"] if w in user]
        return json.dumps(
            {"drugs": [{"name": w, "span": w} for w in found]}, ensure_ascii=False
        )

    if role == "rewriter":
        return (
            "**음주 후에는 진통제 선택에 주의가 필요합니다.** "
            "확실치 않으면 약사와 상의하신 뒤 복용하세요.\n\n"
            "[offline stub] 재작성 경로가 실행되었습니다."
        )

    # drafter — 분류 결과에 따라 그럴듯한 초안을 만들어 뒤 계층을 태운다
    if "음주" in user or "숙취" in user:
        return (
            "숙취로 인한 두통에는 **충분한 수분 섭취와 휴식**이 먼저입니다.\n\n"
            "통증이 심하면 해열진통제를 고려할 수 있습니다.\n\n"
            "[offline stub] 엔드포인트 미설정 — base_url을 채우면 실제 FM이 답합니다."
        )
    return (
        "**증상이 지속되면 가까운 의원에서 진료를 받아보세요.**\n\n"
        "[offline stub] 엔드포인트가 설정되지 않았습니다. "
        "configs/*.yaml 의 llm.base_url 또는 MEDAI_BASE_URL 환경변수를 설정하세요."
    )
