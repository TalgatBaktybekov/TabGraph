"""Extract entities, relationships, and claims from page text into SQLite staging."""

import json

from . import db, llm, ontology

# Keep the prompt bounded for long pages.
MAX_INPUT_CHARS = 12_000
# Claims are only extracted for pages with enough substance.
MIN_CLAIM_TEXT_CHARS = 800

SYSTEM_PROMPT = f"""You extract a knowledge graph from one web page's text.

{ontology.schema_for_prompt()}

Salience levels (how central an entity is to THIS page):
{ontology.salience_for_prompt()}

A Claim is a substantive, self-contained statement the page asserts about an
entity — a fact, finding, argument, or strong opinion that would be worth
remembering on its own. Written as ONE declarative sentence understandable
WITHOUT the page: pronouns like "it" or "this approach" must be resolved to
names. NOT claims: vague generalities ("AI is transforming everything"),
navigation text, questions, meta-statements ("the article discusses X"), or
restatements of an entity's definition (descriptions live on the entity).

Claim examples (good):
- "Neo4j's native vector index supports approximate nearest-neighbor search from version 5.11."
- "Kuzu's development was discontinued after the team was acqui-hired by Apple in 2025."
Claim examples (bad — do not extract):
- "Knowledge graphs are powerful" (too vague)
- "The article discusses entity resolution" (meta-statement)
- "It reduced latency by 40%" (unresolved pronoun — rewrite with the named subject or drop)

Return ONLY a JSON object of this exact shape:
{{
  "entities": [
    {{
      "name": "most complete name used in the text",
      "type": "one of: {", ".join(ontology.ENTITY_TYPES)}",
      "aliases": ["other names/abbreviations used in the text"],
      "description": "one sentence describing this entity, grounded in the text",
      "salience": "one of: {", ".join(ontology.SALIENCE_LEVELS)}"
    }}
  ],
  "relationships": [
    {{
      "source": "entity name (must appear in entities)",
      "type": "one of: {", ".join(ontology.RELATION_TYPES)}",
      "target": "entity name (must appear in entities)",
      "predicate": "short verb phrase — REQUIRED for RELATED_TO, omit otherwise"
    }}
  ],
  "claims": [
    {{
      "statement": "one self-contained declarative sentence naming its subject",
      "stance": "one of: {", ".join(ontology.CLAIM_STANCES)} — the PAGE's stance toward the statement",
      "evidence": "verbatim quote from the page text supporting the claim, at most {ontology.MAX_EVIDENCE_WORDS} words",
      "entities": ["names of extracted entities this claim is about (1 or more)"],
      "confidence": 0.9
    }}
  ]
}}

Rules:
- Extract only entities genuinely discussed in the text, not boilerplate,
  navigation, ads, or the site's own chrome. Prefer fewer, higher-quality
  entities (typically 3-15 per page).
- Do not invent facts: every relationship must be stated or clearly implied
  by the text.
- Do not extract the page itself as an entity; MENTIONS edges are added by
  the pipeline, not by you.
- Claims: only about entities with salience primary or secondary on this
  page. 0 to {ontology.MAX_CLAIMS_PER_PAGE} claims per page — an EMPTY claims
  list is valid and common (docs pages, thin content). The `evidence` quote
  must appear verbatim in the page text."""


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def _validate_claims(
    raw_claims: list, entities: list[dict], page_text: str
) -> tuple[list[dict], int]:
    """Validate claims against the ontology rules; returns (claims, dropped)."""
    eligible = {
        e["name"]
        for e in entities
        if e.get("salience") in ("primary", "secondary")
    }
    # Names + aliases of eligible entities, for the "statement must name one" check.
    eligible_names: dict[str, str] = {}
    for e in entities:
        if e["name"] in eligible:
            for n in [e["name"], *e.get("aliases", [])]:
                eligible_names[n.casefold()] = e["name"]

    has_primary = any(e.get("salience") == "primary" for e in entities)
    text_norm = _normalize_ws(page_text).casefold()
    long_enough = len(page_text) >= MIN_CLAIM_TEXT_CHARS

    claims: list[dict] = []
    dropped = 0
    for claim in raw_claims:
        if len(claims) >= ontology.MAX_CLAIMS_PER_PAGE:
            dropped += 1
            continue
        statement = _normalize_ws(str(claim.get("statement", "")))
        stance = str(claim.get("stance", "")).strip()
        evidence = _normalize_ws(str(claim.get("evidence", "")))
        # Cost control: no claims on thin pages or pages without a primary entity.
        if not (has_primary and long_enough):
            dropped += 1
            continue
        if not statement or stance not in ontology.CLAIM_STANCES:
            dropped += 1
            continue
        if not evidence or len(evidence.split()) > ontology.MAX_EVIDENCE_WORDS:
            dropped += 1
            continue
        # Evidence must appear verbatim (whitespace-normalized) in the page text.
        if evidence.casefold() not in text_norm:
            dropped += 1
            continue
        # Statement must name at least one eligible extracted entity.
        statement_cf = statement.casefold()
        about = sorted(
            {
                canonical
                for alias, canonical in eligible_names.items()
                if alias and alias in statement_cf
            }
        )
        listed = [
            eligible_names.get(str(n).strip().casefold())
            for n in claim.get("entities", [])
        ]
        about = sorted(set(about) | {n for n in listed if n})
        if not about:
            dropped += 1
            continue
        try:
            confidence = min(max(float(claim.get("confidence", 1.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        claims.append(
            {
                "statement": statement,
                "stance": stance,
                "evidence": evidence,
                "entities": about,
                "confidence": confidence,
            }
        )
    return claims, dropped


def _parse_and_validate(
    raw: str, page_text: str
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict]:
    """Validate the model JSON against the ontology."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("model returned JSON but not an object")

    dropped = {"entities": 0, "relationships": 0, "claims": 0}
    entities: list[dict] = []
    # Unknown-type entities are excluded from the graph but kept as
    # candidate_type proposals for the governance review, not discarded.
    candidates: list[dict] = []
    seen_names: dict[str, str] = {}  # name -> type, for relationship checks
    for ent in data.get("entities", []):
        name = str(ent.get("name", "")).strip()
        etype = str(ent.get("type", "")).strip()
        if not name or not etype:
            dropped["entities"] += 1
            continue
        if etype not in ontology.ENTITY_TYPES:
            dropped["entities"] += 1
            candidates.append(
                {
                    "name": name,
                    "proposed_type": etype,
                    "description": str(ent.get("description", "")).strip(),
                }
            )
            continue
        if name in seen_names:
            continue
        aliases = [
            str(a).strip()
            for a in ent.get("aliases", [])
            if isinstance(a, (str, int)) and str(a).strip() and str(a).strip() != name
        ]
        salience = str(ent.get("salience", "")).strip()
        if salience not in ontology.SALIENCE_LEVELS:
            salience = ontology.DEFAULT_SALIENCE
        entities.append(
            {
                "name": name,
                "type": etype,
                "aliases": aliases,
                "description": str(ent.get("description", "")).strip(),
                "salience": salience,
            }
        )
        seen_names[name] = etype

    relationships: list[dict] = []
    for rel in data.get("relationships", []):
        source = str(rel.get("source", "")).strip()
        target = str(rel.get("target", "")).strip()
        rtype = str(rel.get("type", "")).strip()
        predicate = rel.get("predicate")
        spec = ontology.RELATION_TYPES.get(rtype)
        if (
            spec is None
            or source not in seen_names
            or target not in seen_names
            or source == target
            or not ontology.type_allowed(rtype, seen_names[source], seen_names[target])
            or (spec["needs_predicate"] and not str(predicate or "").strip())
        ):
            dropped["relationships"] += 1
            continue
        relationships.append(
            {
                "source": source,
                "type": rtype,
                "target": target,
                "predicate": str(predicate).strip() if spec["needs_predicate"] else None,
            }
        )

    raw_claims = data.get("claims", [])
    claims, dropped["claims"] = _validate_claims(
        raw_claims if isinstance(raw_claims, list) else [], entities, page_text
    )
    return entities, relationships, claims, candidates, dropped


def extract_capture(capture_id: int) -> dict:
    """Extract one capture and store the staged result."""
    capture = db.get_capture(capture_id)
    if capture is None:
        return {"capture_id": capture_id, "status": "missing"}

    page_text = capture["text"][:MAX_INPUT_CHARS]
    prompt = (
        f"URL: {capture['url']}\n"
        f"Title: {capture['title']}\n\n"
        f"Page text:\n{page_text}"
    )
    try:
        raw = llm.generate_json(llm.extraction_model(), SYSTEM_PROMPT, prompt)
        entities, relationships, claims, candidates, dropped = _parse_and_validate(
            raw, page_text
        )
    except Exception as exc:
        db.set_extraction_status(capture_id, "failed", f"{type(exc).__name__}: {exc}")
        return {"capture_id": capture_id, "status": "failed", "error": str(exc)}

    db.replace_staged_extraction(capture_id, entities, relationships, claims)
    db.replace_candidate_types(capture_id, candidates)
    db.set_extraction_status(capture_id, "extracted", None)
    return {
        "capture_id": capture_id,
        "status": "extracted",
        "entities": len(entities),
        "relationships": len(relationships),
        "claims": len(claims),
        "candidate_types": len(candidates),
        "dropped": dropped,
    }


def extract_pending(limit: int = 20) -> list[dict]:
    """Extract promoted-but-pending captures and retry previous failures.

    Staged captures are never extracted — promotion is the user's gesture.
    """
    results = []
    for status in ("pending", "failed"):
        for capture in db.captures_with_status(
            status, limit - len(results), evidence_status="promoted"
        ):
            results.append(extract_capture(capture["id"]))
    return results
