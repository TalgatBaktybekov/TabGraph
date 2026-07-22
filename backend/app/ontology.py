"""Graph schema constants shared by extraction, resolution, and QA."""

import hashlib
import uuid

# Stamped as `ontology_version` on every derived node; bump per MIGRATIONS.md.
ONTOLOGY_VERSION = "1.3"

IRI_PREFIX = "tabkg:"

# User-facing evidence lifecycle on a capture (distinct from the pipeline's
# extraction_status): passive capture lands in `staged` (metadata + chunks
# only); an explicit gesture moves it to `promoted` (full extraction into the
# graph); `excluded` removes it from recall and all future processing.
EVIDENCE_STATUSES = ("staged", "promoted", "excluded")
DEFAULT_EVIDENCE_STATUS = "staged"

PROJECT_LABEL = "Project"
IN_PROJECT = "IN_PROJECT"  # Page -> Project, assigned at promotion

# Machine proposes, user confirms: review lifecycle on CONTRADICTS edges.
# Only confirmed edges should appear in reports by default.
CONTRADICTS_REVIEW_STATES = ("proposed", "confirmed", "dismissed")


def entity_iri() -> str:
    return f"{IRI_PREFIX}entity/{uuid.uuid4()}"


def project_iri() -> str:
    return f"{IRI_PREFIX}project/{uuid.uuid4()}"


def claim_iri() -> str:
    return f"{IRI_PREFIX}claim/{uuid.uuid4()}"


def page_iri(canonical_url: str) -> str:
    digest = hashlib.sha256(canonical_url.encode()).hexdigest()[:16]
    return f"{IRI_PREFIX}page/{digest}"


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

# How central an entity is to the page it was extracted from. Stored on the
# MENTIONS edge; the weights drive community detection and claims gating.
SALIENCE_LEVELS: dict[str, str] = {
    "primary": "The page is chiefly about this entity.",
    "secondary": "Discussed substantively, but not the page's main subject.",
    "passing": "Mentioned in passing; no substantive discussion.",
}
SALIENCE_WEIGHTS = {"primary": 3, "secondary": 1, "passing": 0}
DEFAULT_SALIENCE = "passing"

# --- Claims layer (v1.1) ---

CLAIM_LABEL = "Claim"
CLAIM_PROPERTIES = [
    "statement", "stance", "evidence", "captured_at", "embedding", "confidence",
]
CLAIM_STANCES = ("asserts", "disputes", "questions")
MAX_CLAIMS_PER_PAGE = 8
MAX_EVIDENCE_WORDS = 40

ABOUT = "ABOUT"            # Claim -> entity, 1..n
ASSERTED_IN = "ASSERTED_IN"  # Claim -> Page, exactly one

# Discovery-layer edges and nodes (written by scheduled jobs, not extraction).
BRIDGE = "BRIDGE"                    # Claim—Claim, non-obvious connection
CONTRADICTS = "CONTRADICTS"          # Claim—Claim, sources disagree
COMMUNITY_SUMMARY_LABEL = "CommunitySummary"
MEMBER_OF_COMMUNITY = "MEMBER_OF"    # entity -> CommunitySummary

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


def salience_for_prompt() -> str:
    """Salience level definitions for the extraction prompt."""
    return "\n".join(f"- {name}: {desc}" for name, desc in SALIENCE_LEVELS.items())
