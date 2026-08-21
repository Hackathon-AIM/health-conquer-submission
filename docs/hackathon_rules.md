# Hackathon Rules

**작성일:** 2026-08-21  
**출처:** 해커톤 규칙 공지 및 제출/평가 안내

이 문서는 MediBot 개발과 최종 제출 과정에서 반드시 지켜야 하는 해커톤 규칙을
운영 기준으로 정리한다.

## 핵심 준수 사항

- 원격 참여는 가능하다.
- 다만 Lunit endpoint, dashboard 등 Lunit asset은 Lunit network 밖에서 접근할 수 없다.
- 최종 출력물은 반드시 Lunit의 LLM인 L2를 사용해 생성해야 한다.
- 제공된 MCP tools 외에도 적절한 license를 확보한 외부 data source는 사용할 수 있다.
- Coding agent로는 제공된 Codex를 사용한다.
- 하나의 제출물이 Benchmark 부문과 Frontier 부문 시상에 공통으로 사용된다.
- Evaluation은 외부 접근이 없는 완전히 격리된 환경에서 실행된다.
- HealthBench benchmark를 과도하게 reverse engineering하는 행위는 금지된다.

## Lunit Asset 접근 규칙

Lunit이 제공하는 endpoint와 dashboard는 Lunit network 내부에서만 접근 가능하다.

개발 시 주의사항:

- Lunit endpoint URL, API key, dashboard session 정보는 코드에 하드코딩하지 않는다.
- 관련 값은 `.env` 또는 실행 환경 변수로만 주입한다.
- 원격 환경에서 실행해야 하는 기능은 Lunit asset 없이도 실패 원인이 명확히 드러나도록 구성한다.
- 격리 평가 환경에서 외부 네트워크 호출이 필요하지 않도록 submission path를 구성한다.

## 최종 출력 모델 규칙

최종 제출물의 답변 생성은 반드시 Lunit L2를 사용해야 한다.

MediBot 제출 모드 기준:

```text
MEDIBOT_FINAL_MODEL_PROVIDER=lunit_l2
MEDIBOT_REQUIRE_L2_FINAL=1
MEDIBOT_ALLOW_FALLBACK=0
```

운영 기준:

- L2 호출 실패 시 다른 모델이나 deterministic fallback으로 조용히 대체하지 않는다.
- 최종 답변 trace에는 사용한 final model provider와 model name을 남긴다.
- local smoke test용 fallback은 개발 모드에서만 허용한다.

## Data Source 사용 규칙

기본적으로 주최 측이 제공한 MCP tools를 우선 사용한다.

외부 data source를 추가로 사용할 경우:

- 사용 권한과 license가 명확해야 한다.
- 평가 환경에서 외부 접근이 차단될 수 있으므로 필요한 데이터는 허용 범위 안에서 사전 준비되어야 한다.
- 의료 답변에 영향을 주는 source는 trace에 source 이름, query, 결과 수, 실패 여부를 기록한다.
- PubMed, 의약품 정보, 건강보험/법률, 진료 가이드라인 등 high-risk source는 근거 우선 원칙을 적용한다.

## 제출 및 평가 방식

해커톤 중에는 dashboard를 통해 주최 측 validation set에서 솔루션을 테스트할 수 있다.

제출 규칙:

- dashboard를 통해 전송된 마지막 제출물이 최종 제출로 간주된다.
- 동일한 최종 제출물이 Benchmark 부문과 Frontier 부문 평가에 사용된다.
- 최종 제출물은 주최 측이 정의한 별도 HealthBench holdout test set으로 평가된다.
- 같은 제출물이 chat 품질에 대한 전문가 평가에도 사용된다.

평가 환경:

- 완전히 격리된 환경에서 실행된다.
- 외부 네트워크 접근은 허용되지 않는다.
- submission artifact에는 실행에 필요한 코드, 설정 예시, 로컬 리소스가 포함되어야 한다.

## HealthBench 관련 금지 사항

HealthBench benchmark를 과도하게 reverse engineering하면 안 된다.
관리자 code review에서 확인될 경우 팀은 수상 자격을 잃는다.

금지되는 접근:

- validation/dashboard 결과를 이용해 특정 benchmark item의 정답 패턴을 암기하도록 prompt나 코드를 조정하는 행위
- HealthBench holdout test set을 추정하거나 재구성하려는 행위
- 평가 rubric의 취지를 벗어나 특정 채점 취약점만 노리는 로직을 넣는 행위
- test case별 hard-coded answer, lookup table, memorized branch를 추가하는 행위

허용되는 접근:

- 일반적인 의료 안전성, 근거성, 명확성, 대화 품질 개선
- validation set을 이용한 실행 오류, latency, formatting, trace 누락 debugging
- source routing, evidence filtering, groundedness verification의 일반화 가능한 개선
- prompt와 feature flag별 ablation을 통한 전체 품질 비교

## 개발 중 Harness 운영

개발 중에는 harness를 자유롭게 구성하고 선택할 수 있다.

권장 운영:

- local smoke test와 CoEval 실행 경로를 분리한다.
- validation dashboard 제출 전 submission mode를 별도로 rehearsal한다.
- 평가 결과와 trace를 연결해 regression을 추적한다.
- validation score 개선은 일반화 가능한 원인으로 설명될 때만 최종 제출에 반영한다.

## 제출 전 체크리스트

- [ ] L2 final generation이 강제되어 있다.
- [ ] fallback model이 최종 제출 모드에서 비활성화되어 있다.
- [ ] Lunit endpoint/API key/dashboard 정보가 코드나 문서에 노출되어 있지 않다.
- [ ] MCP source 연결 실패 시 trace에 원인이 남는다.
- [ ] 격리 환경에서 외부 네트워크 없이 실행 가능한 path가 준비되어 있다.
- [ ] validation 결과 기반 변경이 HealthBench reverse engineering에 해당하지 않는다.
- [ ] 마지막 dashboard 제출물이 의도한 최종 artifact인지 확인했다.
- [ ] Benchmark와 Frontier 평가가 같은 제출물을 사용한다는 점을 반영해 config를 고정했다.

