import asyncio
import time

from medibot.config.settings import Settings
from medibot.core.schemas import RetrievalEvidence
from medibot.sources.base import MedicalSource
from medibot.sources.lunit_mcp import build_lunit_mcp_sources
from medibot.sources.mini import MiniEvidenceSource


LOGICAL_TO_MCP_SOURCE: dict[str, str] = {
    "drug": "drug_label",
    "guideline": "guideline",
    "pubmed": "pubmed",
    "hira": "hira_coverage",
    "insurance_law": "law",
}


class SourceRegistry:
    """Routes logical retrieval sources to concrete source adapters."""

    def __init__(
        self,
        settings: Settings,
        sources: dict[str, MedicalSource] | None = None,
    ) -> None:
        self.settings = settings
        self.mini_source = MiniEvidenceSource()
        self.sources: dict[str, MedicalSource] = {}
        if settings.rag_backend in {"lunit_mcp", "hybrid"}:
            self.sources.update(build_lunit_mcp_sources(settings.enabled_mcp_sources))
        if sources:
            self.sources.update(sources)

    async def search(
        self,
        source_name: str,
        query: str,
        top_k: int,
        filters: dict | None = None,
    ) -> tuple[list[RetrievalEvidence], dict[str, object]]:
        started = time.perf_counter()
        adapter_name = self._adapter_name_for(source_name)
        adapter = self._adapter_for(source_name)
        diagnostic: dict[str, object] = {
            "source": source_name,
            "adapter": adapter_name,
            "query": query,
            "top_k": top_k,
            "status": "not_called",
            "result_count": 0,
        }

        if adapter is None:
            diagnostic["status"] = "missing_adapter"
            diagnostic["latency_ms"] = self._elapsed_ms(started)
            return [], diagnostic

        if getattr(adapter, "configured", True) is False:
            diagnostic["status"] = "stub_unconfigured"
            diagnostic["latency_ms"] = self._elapsed_ms(started)
            return [], diagnostic

        try:
            evidence = await asyncio.wait_for(
                adapter.search(query, top_k=top_k, filters=filters),
                timeout=self.settings.source_timeout_s,
            )
        except TimeoutError:
            diagnostic["status"] = "timeout"
            diagnostic["error"] = "source_timeout"
            diagnostic["latency_ms"] = self._elapsed_ms(started)
            return [], diagnostic
        except Exception as exc:
            diagnostic["status"] = "error"
            diagnostic["error"] = type(exc).__name__
            diagnostic["latency_ms"] = self._elapsed_ms(started)
            return [], diagnostic

        diagnostic["status"] = "success"
        diagnostic["result_count"] = len(evidence)
        diagnostic["latency_ms"] = self._elapsed_ms(started)
        return evidence, diagnostic

    def _adapter_for(self, source_name: str) -> MedicalSource | None:
        if self.settings.rag_backend == "mini":
            return self.mini_source

        mcp_name = LOGICAL_TO_MCP_SOURCE.get(source_name, source_name)
        adapter = self.sources.get(mcp_name)
        if adapter is not None:
            return adapter
        if self.settings.rag_backend == "hybrid":
            return self.mini_source
        return None

    def _adapter_name_for(self, source_name: str) -> str:
        if self.settings.rag_backend == "mini":
            return "mini"
        return LOGICAL_TO_MCP_SOURCE.get(source_name, source_name)

    def _elapsed_ms(self, started: float) -> float:
        return (time.perf_counter() - started) * 1000
