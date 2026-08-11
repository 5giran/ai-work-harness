from __future__ import annotations

from typing import Any

from .errors import HarnessError

SUPPORTED_MODALITIES = frozenset({"text", "json", "csv"})
BATCH_CAPABILITIES = frozenset({"transform", "aggregate", "rank"})
RULE_CAPABILITIES = frozenset({"validate", "apply_rules"})

UNSUPPORTED_CATEGORIES = {
    "network": ("UNSUPPORTED_NETWORK", "Network inputs are outside this prototype"),
    "live_api": ("UNSUPPORTED_LIVE_API", "Live API inputs are outside this prototype"),
    "retrieval": ("UNSUPPORTED_RETRIEVAL", "Retrieval workflows are outside this prototype"),
    "multimodal": (
        "UNSUPPORTED_MULTIMODAL",
        "Multimodal inputs are outside this prototype",
    ),
}


def select_route(profile: dict[str, Any]) -> str:
    input_source = profile["input_source"]
    if input_source != "local_files":
        code, message = UNSUPPORTED_CATEGORIES.get(
            input_source,
            ("UNREGISTERED_INPUT_SOURCE", "Input source is not registered"),
        )
        raise HarnessError(code, message, details={"input_source": input_source})

    modalities = set(profile["modalities"])
    if "multimodal" in modalities:
        code, message = UNSUPPORTED_CATEGORIES["multimodal"]
        raise HarnessError(code, message, details={"modalities": sorted(modalities)})
    unsupported_modalities = modalities - SUPPORTED_MODALITIES
    if unsupported_modalities:
        raise HarnessError(
            "UNSUPPORTED_MODALITY",
            "One or more local input modalities are not registered",
            details={"modalities": sorted(unsupported_modalities)},
        )

    capabilities = set(profile["capabilities"])
    registered = BATCH_CAPABILITIES | RULE_CAPABILITIES
    unregistered = capabilities - registered
    if unregistered:
        raise HarnessError(
            "UNREGISTERED_CAPABILITY",
            "One or more capabilities are not registered",
            details={"capabilities": sorted(unregistered)},
        )

    uses_batch = bool(capabilities & BATCH_CAPABILITIES)
    uses_rules = bool(capabilities & RULE_CAPABILITIES)
    if uses_batch and uses_rules:
        raise HarnessError(
            "MIXED_CAPABILITY_FAMILIES",
            "Capabilities from different route families cannot be combined",
            details={"capabilities": sorted(capabilities)},
        )
    if uses_batch:
        return "batch_pipeline"
    if uses_rules:
        return "rule_decision"
    raise HarnessError(
        "EMPTY_CAPABILITIES",
        "At least one registered capability is required",
        details={"capabilities": []},
    )
