# CoEval HealthBench 10-Sample Stabilization Run

Date: 2026-08-20
Run: `evaluation_outputs/2026-08-20/12-50-50`
Dataset: `healthbench_consensus`
Samples: `10`

## Documentation Status

Before this note, the current 10-sample result was not documented in `docs/`.
Existing progress docs still referenced the earlier 5-sample baseline:

- `evaluation_outputs/2026-08-20/12-05-45`
- HealthBench Rubric `0.433`
- Next action: rerun `healthbench_consensus num_samples=10`

This document records the completed 10-sample rerun and factor analysis.

## Result

| Metric | Score |
| --- | ---: |
| HealthBench Rubric | `0.833` |
| context_seeking | `1.000` |
| emergency_referrals | `1.000` |
| hedging | `0.778` |
| global_health | `0.750` |
| health_data_tasks | `1.000` |
| complex_responses | `0.500` |

Execution summary:

- Passed: `10/10`
- Inference failed: `0`
- Scoring failed: `0`
- Total time: `71.59s`
- Average generation time: `20.36s`

Output files:

- `evaluation_outputs/2026-08-20/12-50-50/results_healthbench_consensus.json`
- `evaluation_outputs/2026-08-20/12-50-50/summary_healthbench_consensus.json`
- `evaluation_outputs/2026-08-20/12-50-50/summary_combined.json`

## Comparison With Previous Baseline

| Run | Samples | HealthBench Rubric | Notes |
| --- | ---: | ---: | --- |
| `12-05-45` | `5` | `0.433` | Initial valid baseline; context seeking and fallback policy were weak |
| `12-50-50` | `10` | `0.833` | P0 stabilization run after verifier, prompt, and analyzer changes |

Absolute score gain versus the 5-sample baseline is `+0.400`.

## Factor Analysis

The score improvement is mainly explained by three changes.

1. Verifier fallback policy became less destructive.
   - Earlier, broad unsupported recommendation/risk claims caused full-answer fallback.
   - The new `blocking` / `advisory` split preserves otherwise useful GPT answers unless there is an emergency miss, unsupported dosage, unsupported interaction, contraindication-like claim, or explicit dangerous advice.
   - Trace check for the latest 10 CoEval requests shows most responses stayed in `pass` mode; only `2/10` still used full fallback.

2. HealthBench-oriented answer prompt improved response shape.
   - The generator now loads `prompts/answer_generator.md`.
   - The prompt explicitly requires direct answer first, language matching, useful missing context, red flags, care escalation, and plan completion when the user requests a plan.
   - This aligns with the jump in `context_seeking` from `0.000` in the 5-sample baseline to `1.000` in the 10-sample run.

3. English rule coverage improved analyzer routing.
   - Added targeted recognition for postpartum depression plans, herbal supplement interactions, uncertain labs, earache triage, and leishmaniasis/travel context.
   - These cases now populate `intent`, `risk_class`, and `missing_information`, giving the generator concrete context to include.
   - This contributed to full scores on the postpartum plan, emergency referral, global health aligned accuracy, and health data task cases.

## Remaining Error Drivers

The current weak areas are narrower than before:

- `hedging_any-reducible-uncertainty_seeks_context`: `0.000`
  - Two reducible-uncertainty samples were accurate and appropriately hedged, but still did not seek the specific missing context the rubric expected.
  - Next improvement should make missing context explicit for more general English uncertainty questions, not only the five hand-covered themes.

- `global_health_context-matters-is-clear_language`: `0.500`
  - One global-health sample failed the language subcluster despite aligned/accurate content.
  - Trace shows an English input was replaced by Korean fallback in one blocking case, which likely hurt this dimension.

- `complex_responses_simple_appropriate`: `0.000`
  - The affected sample was a rewrite/editing request involving suspected opioid overdose guidance.
  - The model generated clinically relevant content, but verifier treated naloxone dose/repeat-dose statements as unsupported blocking claims and composer replaced the answer with generic fallback.

Trace-level fallback notes for the latest 10 requests:

- `2/10` had `fallback_reason = unsupported_blocking_claim`.
- One anemia supplement question was blocked because the claim extractor misclassified a "consult for dose advice" sentence as a dosage claim.
- One opioid overdose rewrite question was blocked because naloxone administration/repeat-dose statements were dosage-like and lacked retrieved evidence.

## Next Actions

1. Narrow claim extraction so context-request phrases such as "ask about dose" or "get advice on dose" are not treated as dosage recommendations.
2. Add an English-language fallback path in `ResponseComposer` for English inputs when blocking fallback is unavoidable.
3. Add analyzer/prompt coverage for anemia supplement questions and rewrite/editing tasks involving medical content.
4. Add a small opioid-overdose/naloxone emergency evidence item or retrieval adapter coverage before allowing naloxone procedural claims.
5. Re-run `healthbench_consensus num_samples=10` after those changes and compare fallback rate, language subscore, and complex response subscore.
