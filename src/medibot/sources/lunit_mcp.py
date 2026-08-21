from medibot.core.schemas import RetrievalEvidence


class LunitMCPSource:
    """Placeholder adapter for Lunit-provided MCP tools.

    The hackathon will provide the concrete MCP tool schemas on site. Until then,
    this adapter documents the source boundary and reports itself as unconfigured
    so the workflow can trace missing MCP wiring without failing the whole request.
    """

    configured = False

    def __init__(self, name: str, tool_area: str) -> None:
        self.name = name
        self.tool_area = tool_area

    async def search(
        self, query: str, top_k: int, filters: dict | None = None
    ) -> list[RetrievalEvidence]:
        return []


LUNIT_MCP_SOURCE_SPECS: dict[str, str] = {
    "drug_label": "drug label, approval, dosage, contraindication, and safety data",
    "drug_pricing": "drug reimbursement, benefit status, and pricing data",
    "disease_code": "disease classification code lookup",
    "law": "health insurance law and related statute clauses",
    "guideline": "clinical practice guidelines",
    "pubmed": "current biomedical literature through PubMed",
    "faers": "drug adverse event signals through FAERS",
    "hira_coverage": "HIRA reimbursement criteria and question-answer data",
}


def build_lunit_mcp_sources(enabled: list[str]) -> dict[str, LunitMCPSource]:
    return {
        source_name: LunitMCPSource(source_name, LUNIT_MCP_SOURCE_SPECS[source_name])
        for source_name in enabled
        if source_name in LUNIT_MCP_SOURCE_SPECS
    }
