# CoEval HealthBench Smoke

Date: 2026-08-20
Dataset: `healthbench_consensus`
Sample size: 1, then 5
Client: `MediBotCoEvalClient`

## Configuration

```powershell
$env:PYTHONPATH='src;CoEval/src'
$env:PYTHONIOENCODING='utf-8'
python -m coeval.main client=medibot datasets=healthbench_consensus num_samples=1 runner.concurrent_limit=1 metrics.healthbench_consensus.healthbench_rubric.concurrent_limit=1 metrics.healthbench_consensus.healthbench_rubric.max_attempts=1
```

`OPENAI_API_KEY` was mapped from the local `.env` value for `MEDIBOT_MODEL_API_KEY` during the run.

## Result

### Smoke 1

- HealthBench Rubric: `0.500`
- Samples: `1`
- Inference failed: `0`
- Scoring failed: `0`
- Total time: `11.22s`

### Baseline 5

- HealthBench Rubric: `0.433`
- Samples: `5`
- Passed: `4/5`
- Inference failed: `0`
- Scoring failed: `0`
- Total time: `44.44s`

Theme breakdown:

- `theme:context_seeking`: `0.000`
- `theme:emergency_referrals`: `0.500`
- `theme:hedging`: `0.667`
- `theme:global_health`: `0.500`

## Output Paths

- `evaluation_outputs/2026-08-20/11-56-44/results_healthbench_consensus.json`
- `evaluation_outputs/2026-08-20/11-56-44/summary_healthbench_consensus.json`
- `evaluation_outputs/2026-08-20/11-56-44/summary_combined.json`
- `evaluation_outputs/2026-08-20/12-05-45/results_healthbench_consensus.json`
- `evaluation_outputs/2026-08-20/12-05-45/summary_healthbench_consensus.json`
- `evaluation_outputs/2026-08-20/12-05-45/summary_combined.json`

## Notes

Added `CoEval/src/coeval/conf/client/medibot.yaml` and patched CoEval's non-deterministic result mapping for the installed DeepEval version, which does not preserve `_sample_id` on `TestResult.additional_metadata`.

Also patched CoEval result writing to open JSON files with UTF-8 encoding on Windows.

## Analysis

The initial score is mainly limited by answer control, not by infrastructure.

Observed issues:

- Some GPT-generated answers were overwritten by P0 fallback because `GroundednessVerifier` flagged broad medical safety claims as unsupported high-risk claims.
- `context_seeking` scored `0.000`; the system often did not ask for or explicitly request the most useful missing context.
- English medical prompts such as postpartum depression, herbal supplement interactions, earache, and lab uncertainty are under-classified by the current rule-based analyzer.
- Language matching and region-specific guidance need to be handled more explicitly.

Next experiment:

- Relax full-answer fallback policy.
- Add HealthBench-oriented answer prompt.
- Add targeted context-seeking rules.
- Re-run `healthbench_consensus num_samples=10`.
