"""Extract entities and relationships from page text into SQLite staging."""

import json

from . import db, llm, ontology

# Keep the prompt bounded for long pages.
MAX_INPUT_CHARS = 12_000

SYSTEM_PROMPT = f"""You extract a knowledge graph from one web page's text.

{ontology.schema_for_prompt()}

Return ONLY a JSON object of this exact shape:
{{
  "entities": [
    {{
      "name": "most complete name used in the text",
      "type": "one of: {", ".join(ontology.ENTITY_TYPES)}",
      "aliases": ["other names/abbreviations used in the text"],
      "description": "one sentence describing this entity, grounded in the text"
    }}
  ],
  "relationships": [
    {{
      "source": "entity name (must appear in entities)",
      "type": "one of: {", ".join(ontology.RELATION_TYPES)}",
      "target": "entity name (must appear in entities)",
      "predicate": "short verb phrase — REQUIRED for RELATED_TO, omit otherwise"
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
  the pipeline, not by you."""


def _parse_and_validate(raw: str) -> tuple[list[dict], list[dict], dict]:
    """Validate the model JSON against the ontology."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("model returned JSON but not an object")

    dropped = {"entities": 0, "relationships": 0}
    entities: list[dict] = []
    seen_names: dict[str, str] = {}  # name -> type, for relationship checks
    for ent in data.get("entities", []):
        name = str(ent.get("name", "")).strip()
        etype = str(ent.get("type", "")).strip()
        if not name or etype not in ontology.ENTITY_TYPES:
            dropped["entities"] += 1
            continue
        if name in seen_names:
            continue
        aliases = [
            str(a).strip()
            for a in ent.get("aliases", [])
            if isinstance(a, (str, int)) and str(a).strip() and str(a).strip() != name
        ]
        entities.append(
            {
                "name": name,
                "type": etype,
                "aliases": aliases,
                "description": str(ent.get("description", "")).strip(),
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
    return entities, relationships, dropped


def extract_capture(capture_id: int) -> dict:
    """Extract one capture and store the staged result."""
    capture = db.get_capture(capture_id)
    if capture is None:
        return {"capture_id": capture_id, "status": "missing"}

    prompt = (
        f"URL: {capture['url']}\n"
        f"Title: {capture['title']}\n\n"
        f"Page text:\n{capture['text'][:MAX_INPUT_CHARS]}"
    )
    try:
        raw = llm.generate_json(llm.extraction_model(), SYSTEM_PROMPT, prompt)
        entities, relationships, dropped = _parse_and_validate(raw)
    except Exception as exc:
        db.set_extraction_status(capture_id, "failed", f"{type(exc).__name__}: {exc}")
        return {"capture_id": capture_id, "status": "failed", "error": str(exc)}

    db.replace_staged_extraction(capture_id, entities, relationships)
    db.set_extraction_status(capture_id, "extracted", None)
    return {
        "capture_id": capture_id,
        "status": "extracted",
        "entities": len(entities),
        "relationships": len(relationships),
        "dropped": dropped,
    }


def extract_pending(limit: int = 20) -> list[dict]:
    """Extract pending captures and retry previous failures."""
    results = []
    for status in ("pending", "failed"):
        for capture in db.captures_with_status(status, limit - len(results)):
            results.append(extract_capture(capture["id"]))
    return results
