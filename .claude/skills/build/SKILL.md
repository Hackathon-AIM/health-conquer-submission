---
name: build
description: med_ai 파이프라인을 고칠 때의 절차와 제약. 레이어 경계, contracts 계약, config 스위치, 사전 확장 규칙을 담는다. 코드를 수정하거나 새 레이어/규칙을 추가할 때 사용.
---

# 파이프라인 수정 절차

## 0. 시작 전 확인

- finding 이 있는가? 없으면 `analyze` 스킬을 먼저 돌린다.
- **한 번에 하나.** 두 개를 같이 고치면 A/B 가 무의미해진다.

## 1. 레이어를 고른다

`references/layer_map.md` 에 증상 → 파일 대응표가 있다.
표에 없는 증상이면 고치기 전에 되묻는다.

## 2. 스위치를 먼저 만든다

코드보다 스위치가 먼저다. `src/medai/config.py` 의 `DEFAULTS["layers"]` 에
새 키를 **False 기본값**으로 넣는다.

```python
"layers": {
    "passthrough": False,
    "redflag": True,
    ...
    "my_fix": False,       # ← 새 동작. 기존 동작이 기본이다.
},
```

그리고 `configs/` 에 켠 버전을 만든다:

```yaml
# configs/v2_myfix.yaml
name: v2_myfix
layers:
  my_fix: true
```

이제 qa 가 `v2_full.yaml` 대 `v2_myfix.yaml` 로 잴 수 있다.

## 3. 고친다

지켜야 할 제약 (어기면 다른 데가 조용히 깨진다):

### 결정론 레이어는 결정론으로 남긴다
L1(레드플래그·엔티티 규칙), L1b(DUR 조회), L4b(출력 게이트 판정)는 규칙과 조회다.
LLM 판단을 여기 섞으면 같은 입력이 다른 답을 내고 회귀 테스트가 죽는다.
LLM 은 후보 생성에만: 규칙 ∪ LLM → span 검증 → 사전 필터.

### contracts.py 는 추가만, Optional 로만
```python
# ❌ Python 3.9 에서 pydantic 이 런타임에 평가하다가 TypeError
new_field: str | None = None
# ✅
new_field: Optional[str] = None
```
`QueryPlan` 은 **근거 필드가 결론 필드보다 앞**에 온다. LLM 이 왼쪽부터 생성하므로
필드 순서 = 사고 순서다. 결론을 앞에 두면 사후 합리화가 나온다.

### 정규식은 한국어 부분문자열을 검토한다
```python
r"술"                        # ❌ 수술·시술·기술에 다 걸린다
r"(?<![가-힣])술(?![기의])"   # ✅
```
새 패턴마다 `tests/test_gates.py` 에 "걸리면 안 되는 문장" 케이스를 같이 넣는다.

### 사전은 창작하지 않는다
`data/redflags.yaml`, `drug_map.json`, `risk_rules.yaml` 항목의 허용 출처는
**식약처 허가사항 · DUR 고시 · 응급의학 공개 임상 기준** 뿐이다.
손으로 지어낸 항목은 오탐이 되고, 오탐은 감점이 된다.

❌ **HealthBench 역추출 금지** — 대회 규칙 7, 수상 자격 박탈 사유. `make redflags` 는 폐기됐다.

### drafter 는 고정
`models.drafter` 가 심사 대상 텍스트를 쓴다. 대회 FM 에서 바꾸지 않는다.
분류·비평·추출 모델 교체는 규정 확인 후에만.

### serve.py 의 무상태 재생을 같이 고친다
CoEval 은 매 요청에 전체 messages 를 보낸다. 서버는 상태를 안 들고 있고
`session_from_messages()` 가 규칙 추출을 재생해서 세션을 복원한다.
세션에 의존하는 기능을 추가했으면 이 함수도 같이 고쳐야 1턴 정보가 3턴에 도달한다.

## 4. 자기 검증

```bash
make test      # 단위 테스트
make audit     # 배선이 다이어그램과 일치하는지 (37개 체크)
```

둘 다 통과해야 qa 에게 넘긴다. 넘길 때 형식:

```
변경: <한 줄>
스위치: layers.<key> (기본 False)
A/B: configs/<기존>.yaml vs configs/<새것>.yaml
회귀 추가: tests/test_gates.py::<테스트명>
부작용 가능성: <있으면 적는다>
```

## 자주 밟는 함정

- **리랭커 지연 로딩** — 이벤트 루프 안에서 BGE 모델을 처음 로딩하면 89초가 나온다.
  `Pipeline.__init__` 에서 `rr.preload(cfg)` 로 미리 올린다. 후보가 top_k 이하면 건너뛴다.
- **노드 이중 실행** — 래퍼(`graph.py`)의 마지막 노드가 `run_turn` 을 다시 부르면
  파이프라인 전체가 두 번 돈다. 노드는 `pipeline.py` 의 `node_*` 메서드를 재사용한다.
- **SDK 타임아웃** — OpenAI SDK 기본이 600초다. `llm.timeout` 을 반드시 명시한다.
- **`<think>` 블록** — L1 계열은 추론을 `<think>...</think>` 로 뱉는다.
  `strip_think()` 를 거치지 않으면 그대로 사용자에게 나간다. 닫는 태그 없는 경우도 처리한다.
