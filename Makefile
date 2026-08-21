# venv 안의 파이썬을 쓴다. `make setup` 이 .venv 를 만든다.
VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: setup dicts test run live smoke ab ab-emergency clean nuke-user-site

# ── 초기 설정 ────────────────────────────────────────────────
# ⚠️ macOS 기본 파이썬은 3.9다. 3.10+ 가 있으면 그걸 쓰고, 없으면 3.9로도 돌아간다.
setup:
	@bash scripts/setup.sh

dicts:
	$(PY) data/build_dicts.py --csv data/의약품목록.csv

# ── 실행 ────────────────────────────────────────────────────
test:
	$(PY) -m pytest tests/ -q

run:
	$(PY) -m eval.harness --config configs/mock.yaml

live:
	$(PY) -m eval.harness --config configs/live.yaml

smoke:
	$(PY) scripts/smoke.py configs/live.yaml

# ── A/B — 설정별 점수 비교. 이 루프의 회전 수가 곧 순위다 ────
ab:
	@for c in configs/v1_no_critic.yaml configs/v2_full.yaml; do \
		echo "=== $$c ==="; $(PY) -m eval.harness --config $$c; \
	done

# 응급 시 검색 우회는 HealthBench가 규정한 게 아니라 우리 가설이므로 측정한다
ab-emergency:
	@for c in configs/emergency_bypass.yaml configs/emergency_guideline.yaml; do \
		echo "=== $$c ==="; $(PY) -m eval.harness --config $$c; \
	done

clean:
	rm -rf logs/*.json logs/*.jsonl
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# ── 전역(user site-packages) 오염 제거 ──────────────────────
# `pip install -r requirements.txt` 를 venv 없이 돌려서 ~/Library/Python/3.9 에
# 깔린 패키지를 되돌린다. 시스템 파이썬을 건드리므로 한 번만 실행할 것.
nuke-user-site:
	@bash scripts/cleanup_user_site.sh

# ── 실제 HealthBench 로 점수 뽑기 ────────────────────────────
# 대회 FAQ가 "HealthBench Consensus 지표"라고 명시 → consensus 가 과녁
hb:
	$(PY) -m eval.healthbench --variant consensus --config configs/mock.yaml --limit 20

hb-live:
	$(PY) -m eval.healthbench --variant consensus --config configs/live.yaml --limit 100

hb-hard:
	$(PY) -m eval.healthbench --variant hard --config configs/live.yaml --limit 50

# ── E2E 감사 — 다이어그램 ↔ 코드 대조 ───────────────────────
audit:
	$(PY) scripts/audit_e2e.py

# ── LangGraph 경로 (선택) ───────────────────────────────────
# pipeline.py 만으로 완전히 동작한다. graph 는 시각화·관측성이 필요할 때만.
graph:
	$(PY) scripts/run_graph.py --compare "어제 술을 너무 많이 마셨는데 머리가 아파요"

graph-mermaid:
	$(PY) scripts/run_graph.py --mermaid

# ── 제출물: OpenAI 호환 서버 ────────────────────────────────
# CoEval(lunit-io/CoEval) 은 평가 대상을 OpenAI 호환 엔드포인트로 호출한다.
# 즉 제출물은 Python 함수가 아니라 이 서버다.
serve:
	$(PY) serve.py --config configs/live.yaml --port 8080

serve-mock:
	$(PY) serve.py --config configs/mock.yaml --port 8080

# 서버가 CoEval 이 기대하는 형태로 응답하는지 확인 (다른 터미널에서)
serve-check:
	@echo "--- /v1/models ---"
	@curl -s localhost:8080/v1/models; echo
	@echo "--- /v1/chat/completions ---"
	@curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
	  -d '{"model":"medai","messages":[{"role":"user","content":"머리가 아파요"}]}' \
	  | $(PY) -c "import json,sys;d=json.load(sys.stdin);print(d['choices'][0]['message']['content'][:300])"

# ── 검증 (FM 없이 지금 할 수 있는 것) ───────────────────────
validate:
	$(PY) -m eval.harness --config configs/openai.yaml

validate-hb:
	$(PY) -m eval.healthbench --variant consensus --config configs/openai.yaml --limit 20

# CoEval 이 우리 serve.py 를 부르는 경로를 미리 뚫는다
rehearsal:
	bash scripts/coeval_rehearsal.sh

# ⚠️ 폐기 — 대회 규칙: HealthBench 과도한 역공학 금지 (code review 시 수상 자격 박탈).
#    레드플래그 사전은 응급의학 일반 기준으로만 유지한다. 실행 금지.
redflags redflags-dump redflags-validate:
	@echo "❌ 폐기됨: HealthBench 역추출은 대회 규칙 위반 소지 (역공학 금지)."
	@echo "   data/redflags.yaml 은 일반 임상 기준으로만 관리. docs/l2_playbook.md §0"

# ── L2 (대회 FM) — docs/l2_playbook.md ──────────────────────
# 현장 1순위: MCP 실측. 청킹/예산 상수는 이 출력을 보고 정한다.
probe:
	$(PY) scripts/probe_mcp.py

serve-l2:
	$(PY) serve.py --config configs/l2_live.yaml --port 8080

# Patient Simulator 대화 기록 → logs/sim/ (프론티어 상 대비 — 사람이 읽는다)
sim:
	$(PY) scripts/sim_loop.py --n 3 --turns 3

# L2 전 구간 스모크 (엔드포인트→tool_calls→검색→생성→파이프라인)
l2-smoke:
	$(PY) scripts/l2_smoke.py

# 직접 대화해본다 (멀티턴 유지 · 추적 표시 · /raw 로 순수 L2 비교)
chat:
	$(PY) scripts/chat.py

# ── 제출 (docs/submission.md) ───────────────────────────────
submit-check:
	$(PY) scripts/submit_check.py

submit-check-docker:
	$(PY) scripts/submit_check.py --docker

docker-build:
	docker build -t medai:local .

docker-run:
	docker run --rm -p 8000:8000 -e LUNIT_FM_API_KEY=$${LUNIT_FM_API_KEY} medai:local

# ── L1 (대회 FM 의 16B 형제) ────────────────────────────────
# 서빙: python -m sglang.launch_server --model-path learning-unit/L1-16B-A3B \
#         --port 9006 --host 0.0.0.0 --tp 1 --dtype bfloat16 --trust-remote-code \
#         --attention-backend triton --moe-runner-backend triton
l1-smoke:
	$(PY) scripts/smoke.py configs/l1.yaml

# ★ 가장 중요한 실험 — 우리 레이어가 L2 권장 사용법(raw)보다 나은가?
#   기준선 = l2_raw (2단계 그대로, 우리 레이어 없음). 못 넘으면 레이어가 노이즈다.
#   ⚠️ 진짜 비교는 대시보드 검증 세트에서 할 것. 로컬 숫자는 방향 확인용.
ab-baseline:
	@echo "=== L2 raw (권장 2단계, 우리 레이어 없음) ==="
	$(PY) -m eval.healthbench --variant consensus --config configs/l2_raw.yaml --limit 50
	@echo
	@echo "=== 우리 파이프라인 (l2_live) ==="
	$(PY) -m eval.healthbench --variant consensus --config configs/l2_live.yaml --limit 50

# CoEval 을 macOS 에서 설치 가능하게 패치 (sglang 제거 — serve 는 안 쓰므로 무해)
patch-coeval:
	bash scripts/patch_coeval_mac.sh
