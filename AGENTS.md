# med_ai — coding agent 안내 (Codex / Claude 공용)

Conquer Health 해커톤 제출물. 대국민 건강상담 챗봇, HealthBench Consensus 로 채점.

## 먼저 읽을 것

1. `docs/l2_playbook.md` — 대회 규칙 → 설계 결정, MCP 21종 상황별 사용법, 예산
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

```bash
make setup                                  # .venv
python scripts/probe_mcp.py                 # MCP 실측 (현장 1순위)
python serve.py --config configs/l2_live.yaml --port 8080   # 제출물 서버
python scripts/sim_loop.py --n 3            # Patient Simulator 대화 기록
make test && make audit                     # 커밋 전 필수
make ab-baseline                            # l2_raw(기준선) vs l2_live(우리)
```

## 구조 한 줄 요약

`serve.py`(무상태 OpenAI 호환 서버) → `pipeline.py`(L1 레드플래그 → 분류 →
L1b DUR → **L4 = L2 2단계**(`l2.py`: MCP 검색→생성) → L4b 출력 게이트 → L4c 비평).
L2 경로에서 L3 자체 검색과 rerank 는 쓰지 않는다.
