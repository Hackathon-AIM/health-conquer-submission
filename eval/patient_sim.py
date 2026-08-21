"""시뮬레이션 환자 에이전트.

⚠️ 이건 루닛이 제공할 진행 하네스의 로컬 대체품이다.
   현장에서 루닛 하네스를 받으면 이 클래스를 그쪽 호출로 바꾸면 된다.

심사 구조 이해가 전략을 바꾼다
  · 시작 질문은 고정 (모든 팀 동일)
  · 후속 발화는 '환자를 연기하는 LLM'이 생성
  · 임상의는 답변 하나가 아니라 3턴 대화 전체를 프론티어와 블라인드 비교

⭐ 그래서 되묻기가 유효하다
  환자 에이전트는 페르소나(나이·병력·증상)를 들고 있고,
  우리가 물어보면 그 정보를 준다. 안 물어보면 안 준다.
  → 되묻고 → 정보를 받고 → 개인화된 답을 하는 흐름이 HealthBench 루브릭과 맞물린다.
  다만 3턴뿐이라 1턴을 통째로 질문에만 쓰면 손해다.
  "답변 + 되묻기"를 한 턴에 같이 하는 게 최적일 가능성이 높다.

현장 확인: 하네스 코드에서 환자 에이전트의 시스템 프롬프트를 반드시 읽을 것.
  얘가 어떻게 행동하도록 지시받았는지(정보를 순순히 주는지, 감정적인지,
  애매하게 말하는지) 알면 우리 대응 전략이 달라진다.
"""

from __future__ import annotations

from medai.config import Config
from medai.llm import LLM

SYSTEM = """당신은 건강 상담 챗봇을 이용하는 일반인 환자를 연기합니다.

[페르소나]
{persona}

[규칙]
- 의학 전문 용어를 쓰지 않고 일상적인 말투로 이야기합니다.
- 챗봇이 물어본 정보만 알려줍니다. 묻지 않은 것을 먼저 말하지 않습니다.
- 페르소나에 없는 정보를 만들어내지 않습니다. 모르면 "잘 모르겠어요"라고 합니다.
- 한 번에 한두 문장으로 짧게 말합니다.
- 대화가 충분히 마무리되었다고 판단되면 정확히 "[END]" 만 출력합니다.
"""


class PatientAgent:
    def __init__(self, llm: LLM, cfg: Config):
        self.llm = llm
        self.cfg = cfg

    async def next_utterance(self, persona: str, convo: list[dict]) -> str:
        hist = []
        for t in convo:
            hist.append({"role": "user", "content": t["user"]})
            hist.append({"role": "assistant", "content": t["assistant"]})

        # 역할 반전: 환자 입장에서는 챗봇 발화가 '상대'다
        flipped = [
            {"role": "assistant" if m["role"] == "user" else "user", "content": m["content"]}
            for m in hist
        ]

        out = await self.llm.chat(
            "critic",   # 보조 역할 슬롯 재사용 (설정에서 모델 교체 가능)
            [{"role": "system", "content": SYSTEM.format(persona=persona or "특이사항 없음")}]
            + flipped
            + [{"role": "user", "content": "위 답변에 대해 환자로서 이어서 할 말을 한두 문장으로 하세요."}],
            temperature=0.7,
            max_tokens=200,
        )
        out = (out or "").strip()
        if "[END]" in out or out.startswith("[offline stub]"):
            return ""
        return out
