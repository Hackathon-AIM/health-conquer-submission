# med_ai — coding agent 안내 (Codex / Claude 공용)

Conquer Health 해커톤 제출물. 대국민 건강상담 챗봇, HealthBench Consensus 로 채점.

## 먼저 읽을 것

0. **`docs/spec.md`** — 대회 사양 원문 SSOT. 엔드포인트·MCP 21종·2단계 계약·제출 제약·CoEval 설정.
   **사양에 관한 사실은 기억이 아니라 여기서 읽는다.**
1. `docs/l2_playbook.md` — 규칙 → 설계 결정, MCP 상황별 사용법, 예산 상수의 실측 근거
2. `.claude/skills/build/references/layer_map.md` — 증상 → 파일 지도
3. `.claude/skills/verify/references/golden_scenarios.md` — 깨지면 되돌리는 안전 시나리오 5종

## 절대 규칙

- **최종 출력은 반드시 L2** (`Lunit/L2-preview`). drafter·rewriter·critic 등
  최종 텍스트를 만지는 모든 호출이 해당된다.
- **HealthBench 역공학 금지** (수상 자격 박탈). 평가 데이터에서 패턴·사전을
  역추출하는 코드를 작성하지 않는다.
- **평가는 격리 환경** — 제출물 경로에서 외부 API(OpenAI 등) 호출 금지.
  외부 데이터는 파일로 구워 동봉한다.
- API 키는 `.env` 에만. 코드·yaml·커밋에 절대 넣지 않는다.
- 변경은 한 번에 하나, config 스위치(기본 False)와 함께. A/B 없이 채택하지 않는다.

## 실행

`make` 가 없는 환경(Windows 등)에서는 `python scripts/mk.py <target>` 이 대신한다.

```bash
python scripts/mk.py setup        # .venv (uv 우선)
python scripts/mk.py probe        # MCP 실측 (현장 1순위)
python scripts/mk.py serve-l2     # 제출물 서버 (configs/l2_live.yaml)
python scripts/mk.py sim          # Patient Simulator 대화 기록
python scripts/mk.py test         # 커밋 전 필수
python scripts/mk.py audit        # 커밋 전 필수
python scripts/mk.py submit-check # 제출 규격 점검
python scripts/mk.py ab-baseline  # l2_raw(기준선) vs l2_live(우리)
python scripts/mk.py list         # 전체 타깃
```

⚠️ **`app.py` 와 `submission/` 는 Dockerfile 이 COPY 하지 않는다 = 평가 경로가 아니다.**
평가되는 것은 `serve.py` → `src/medai/` 뿐이다.

## 구조 한 줄 요약

`serve.py`(무상태 OpenAI 호환 서버) → `pipeline.py`(L1 레드플래그 → 분류 →
L1b DUR → **L4 = L2 2단계**(`l2.py`: MCP 검색→생성) → L4b 출력 게이트 → L4c 비평).
L2 경로에서 L3 자체 검색과 rerank 는 쓰지 않는다.
