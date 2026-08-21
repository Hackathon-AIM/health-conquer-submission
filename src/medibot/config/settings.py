import os
from pathlib import Path

from pydantic import BaseModel, Field


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _model_api_base() -> str | None:
    explicit = (
        os.getenv("MEDIBOT_MODEL_API_BASE")
        or os.getenv("LUNIT_FM_API_URL")
        or os.getenv("LUNIT_L2_API_BASE")
        or os.getenv("OPENAI_API_BASE")
    )
    if explicit:
        return _normalize_model_api_base(explicit)
    if os.getenv("MEDIBOT_MODEL_API_KEY") or os.getenv("OPENAI_API_KEY"):
        return "https://api.openai.com/v1"
    return None


def _normalize_model_api_base(value: str) -> str:
    base = value.rstrip("/")
    if base == "https://model.hackathon.lunit.io":
        return base + "/v1"
    return base


class Settings(BaseModel):
    """Runtime settings sourced from environment variables when available."""

    model_api_base: str | None = Field(
        default_factory=_model_api_base
    )
    model_name: str = Field(
        default_factory=lambda: os.getenv("MEDIBOT_MODEL_NAME")
        or os.getenv("LUNIT_FM_MODEL")
        or os.getenv("LUNIT_L2_MODEL")
        or "medibot-fallback"
    )
    model_api_key: str | None = Field(
        default_factory=lambda: os.getenv("MEDIBOT_MODEL_API_KEY")
        or os.getenv("LUNIT_FM_API_KEY")
        or os.getenv("LUNIT_L2_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    final_model_provider: str = Field(
        default_factory=lambda: os.getenv("MEDIBOT_FINAL_MODEL_PROVIDER", "fallback")
    )
    require_l2_final: bool = Field(
        default_factory=lambda: os.getenv("MEDIBOT_REQUIRE_L2_FINAL", "0") == "1"
    )
    request_timeout_s: float = Field(
        default_factory=lambda: float(os.getenv("MEDIBOT_TIMEOUT_S", "30"))
    )
    allow_fallback: bool = Field(
        default_factory=lambda: os.getenv("MEDIBOT_ALLOW_FALLBACK", "1") != "0"
    )
    source_top_k: int = Field(
        default_factory=lambda: int(os.getenv("MEDIBOT_SOURCE_TOP_K", "5"))
    )
    source_timeout_s: float = Field(
        default_factory=lambda: float(os.getenv("MEDIBOT_SOURCE_TIMEOUT_S", "8"))
    )
    mcp_url: str = Field(
        default_factory=lambda: os.getenv(
            "MEDIBOT_MCP_URL", "https://mcp.hackathon.lunit.io/mcp"
        )
    )
    mcp_protocol_version: str = Field(
        default_factory=lambda: os.getenv(
            "MEDIBOT_MCP_PROTOCOL_VERSION", "2025-11-25"
        )
    )
    l2_generation_tool_budget: int = Field(
        default_factory=lambda: int(os.getenv("MEDIBOT_L2_GENERATION_TOOL_BUDGET", "2"))
    )
    l2_retrieval_tool_budget: int = Field(
        default_factory=lambda: int(os.getenv("MEDIBOT_L2_RETRIEVAL_TOOL_BUDGET", "10"))
    )
    l2_tool_result_char_limit: int = Field(
        default_factory=lambda: int(os.getenv("MEDIBOT_L2_TOOL_RESULT_CHAR_LIMIT", "2500"))
    )
    trace_content_char_limit: int = Field(
        default_factory=lambda: int(os.getenv("MEDIBOT_TRACE_CONTENT_CHAR_LIMIT", "1000"))
    )
    evidence_limit: int = Field(
        default_factory=lambda: int(os.getenv("MEDIBOT_EVIDENCE_LIMIT", "5"))
    )
    rag_backend: str = Field(
        default_factory=lambda: os.getenv("MEDIBOT_RAG_BACKEND", "mini")
    )
    enabled_mcp_sources: list[str] = Field(
        default_factory=lambda: [
            value.strip()
            for value in os.getenv(
                "MEDIBOT_ENABLED_MCP_SOURCES",
                (
                    "drug_label,drug_pricing,disease_code,law,guideline,"
                    "pubmed,faers,hira_coverage"
                ),
            ).split(",")
            if value.strip()
        ]
    )
    trace_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv("MEDIBOT_TRACE_PATH", "storage/traces/medibot-traces.jsonl")
        )
    )
    prompt_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("MEDIBOT_PROMPT_DIR", "prompts"))
    )

    @property
    def has_model_endpoint(self) -> bool:
        return bool(self.model_api_base and self.model_name)
