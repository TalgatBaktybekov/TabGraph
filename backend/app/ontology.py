"""Graph schema constants shared by extraction, resolution, and QA."""

PAGE_LABEL = "Page"
PAGE_PROPERTIES = ["url", "title", "captured_at", "content_hash"]

ENTITY_TYPES: dict[str, str] = {
    "Person": "A real, named individual human (not roles or job titles).",
    "Organization": "A company, institution, government body, team, or other named group.",
    "Concept": "A named idea, method, technology, field, or topic (e.g. 'knowledge graph', 'entity resolution').",
    "Product": "A named product, service, tool, library, or system that is made or sold.",
    "Event": "A named occurrence at a point in time (conference, release, acquisition, election).",
}

ENTITY_PROPERTIES = ["canonical_name", "aliases", "description", "embedding"]
EMBEDDING_DIMENSIONS = 384

ENTITY_BASE_LABEL = "Entity"
MENTIONS = "MENTIONS"

RELATION_TYPES: dict[str, dict] = {
    "WORKS_AT": {
        "source_types": ["Person"],
        "target_types": ["Organization"],
        "needs_predicate": False,
        "description": "A person works (or worked) at an organization.",
    },
    "PART_OF": {
        "source_types": ["*"],
        "target_types": ["*"],
        "needs_predicate": False,
        "description": "Component/membership: a thing is part of a larger thing (team part of company, feature part of product).",
    },
    "RELATED_TO": {
        "source_types": ["*"],
        "target_types": ["*"],
        "needs_predicate": True,
        "description": "Any other meaningful relation; the `predicate` property holds a short verb phrase (e.g. 'competes with', 'invented').",
    },
    "CREATED_BY": {
        "source_types": ["Product"],
        "target_types": ["Organization", "Person"],
        "needs_predicate": False,
        "description": "A product was created/developed by an organization or person.",
    },
}

PROVENANCE_PROPERTY = "source_page_url"


def type_allowed(rel_type: str, source_type: str, target_type: str) -> bool:
    """Check whether a relation allows the given endpoint types."""
    spec = RELATION_TYPES.get(rel_type)
    if spec is None:
        return False
    src_ok = "*" in spec["source_types"] or source_type in spec["source_types"]
    tgt_ok = "*" in spec["target_types"] or target_type in spec["target_types"]
    return src_ok and tgt_ok


def schema_for_prompt() -> str:
    """Return a compact schema summary for the prompts."""
    lines = ["Entity types:"]
    for name, desc in ENTITY_TYPES.items():
        lines.append(f"- {name}: {desc}")
    lines.append("")
    lines.append("Relationship types (source -> target):")
    for name, spec in RELATION_TYPES.items():
        src = "|".join(spec["source_types"])
        tgt = "|".join(spec["target_types"])
        extra = " Carries a short free-text `predicate` property." if spec["needs_predicate"] else ""
        lines.append(f"- {name} ({src} -> {tgt}): {spec['description']}{extra}")
    lines.append("")
    lines.append(
        f"Additionally, every captured page is a {PAGE_LABEL} node "
        f"(properties: {', '.join(PAGE_PROPERTIES)}) with a {MENTIONS} edge to "
        "every entity extracted from it, and every fact edge carries a "
        f"`{PROVENANCE_PROPERTY}` property with the URL it came from."
    )
    return "\n".join(lines)
