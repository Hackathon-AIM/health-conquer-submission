from pydantic import BaseModel


class FeatureFlags(BaseModel):
    """P0 feature switches with graph/recovery/repair disabled by default."""

    enable_graph_reasoning: bool = False
    enable_query_recovery: bool = False
    enable_answer_planner: bool = False
    enable_targeted_repair: bool = False
    enable_evidence_coverage_check: bool = True
    enable_dynamic_source_routing: bool = True
