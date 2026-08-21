import json
from pathlib import Path

from medibot.core.schemas import TraceRecord


class TraceWriter:
    """Append-only JSONL trace writer."""

    def __init__(self, trace_path: Path) -> None:
        self.trace_path = trace_path

    def write(self, trace: TraceRecord) -> None:
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trace.model_dump(mode="json"), ensure_ascii=False))
            handle.write("\n")
