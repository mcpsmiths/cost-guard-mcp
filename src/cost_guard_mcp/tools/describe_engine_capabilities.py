from cost_guard_mcp.types import AccuracyTier, Engine, EngineCapabilities

_CAPABILITIES: dict[str, EngineCapabilities] = {
    "bigquery": EngineCapabilities(
        engine="bigquery",
        supports_precise_bytes=True,
        supports_dollar_estimate=True,
        default_accuracy_tier=AccuracyTier.PRECISE,
        known_gaps=[
            "Estimate downgrades to UPPER_BOUND when BigQuery's own "
            "totalBytesProcessedAccuracy is not PRECISE (e.g. federated or wildcard tables, "
            "or tables with a pending streaming buffer).",
            "Projects on BigQuery Editions/capacity billing get a byte estimate only, no "
            "dollar figure — capacity billing has no fixed $/byte rate.",
        ],
    ),
}


def describe_engine_capabilities(engine: Engine) -> EngineCapabilities:
    if engine not in _CAPABILITIES:
        raise ValueError(
            f"describe_engine_capabilities: engine '{engine}' is not yet supported. "
            f"Supported engines: {sorted(_CAPABILITIES)}."
        )
    return _CAPABILITIES[engine]
