import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
COEVAL_ARGS = [
    "-m",
    "coeval.main",
    "client=medibot",
    "datasets=healthbench_consensus",
    "num_samples=5",
    "runner.concurrent_limit=1",
    "metrics.healthbench_consensus.healthbench_rubric.concurrent_limit=1",
]


def _env_for(mode: str) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT / "src"), str(REPO_ROOT / "CoEval" / "src")]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["PYTHONIOENCODING"] = "utf-8"

    if mode == "mini":
        env.update(
            {
                "MEDIBOT_FINAL_MODEL_PROVIDER": "fallback",
                "MEDIBOT_REQUIRE_L2_FINAL": "0",
                "MEDIBOT_ALLOW_FALLBACK": "1",
                "MEDIBOT_RAG_BACKEND": "mini",
            }
        )
    elif mode == "l2_mcp":
        env.update(
            {
                "MEDIBOT_FINAL_MODEL_PROVIDER": "lunit_l2",
                "MEDIBOT_REQUIRE_L2_FINAL": "1",
                "MEDIBOT_ALLOW_FALLBACK": "0",
                "MEDIBOT_RAG_BACKEND": "lunit_mcp",
                "MEDIBOT_MCP_PROTOCOL_VERSION": "2025-11-25",
                "MEDIBOT_TIMEOUT_S": "90",
                "MEDIBOT_SOURCE_TIMEOUT_S": "60",
            }
        )
    else:
        raise ValueError(f"unsupported mode: {mode}")
    return env


def run_compare(output_path: Path) -> dict[str, Any]:
    results = []
    started = time.perf_counter()
    for mode in ["mini", "l2_mcp"]:
        run_started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, *COEVAL_ARGS],
            cwd=REPO_ROOT,
            env=_env_for(mode),
            text=True,
            capture_output=True,
            check=False,
        )
        results.append(
            {
                "mode": mode,
                "returncode": completed.returncode,
                "elapsed_ms": (time.perf_counter() - run_started) * 1000,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )

    summary = {
        "dataset": "healthbench_consensus",
        "num_samples": 5,
        "passed": all(result["returncode"] == 0 for result in results),
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "runs": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Run 5-sample HealthBench Consensus mini vs L2/MCP comparison."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("storage/evaluation_runs/coeval_healthbench_compare.json"),
    )
    args = parser.parse_args()

    summary = run_compare(args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
