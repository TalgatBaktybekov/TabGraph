"""Promote staged captures into Neo4j."""

import json
import os

from neo4j import GraphDatabase

from . import db, ontology, resolution


def _uri() -> str:
    return os.environ.get("NEO4J_URI", "bolt://localhost:7687")


_driver = None


def get_driver():
    global _driver
    if _driver is None:
        from . import llm

        llm.load_env() 
        _driver = GraphDatabase.driver(
            _uri(),
            auth=(
                os.environ.get("NEO4J_USERNAME", "neo4j"),
                os.environ["NEO4J_PASSWORD"],
            ),
        )
    return _driver


def ensure_schema() -> None:
    """Create the Neo4j schema."""
    with get_driver().session() as session:
        session.run(
            "CREATE CONSTRAINT page_url IF NOT EXISTS"
            " FOR (p:Page) REQUIRE p.url IS UNIQUE"
        )
        session.run(
            "CREATE CONSTRAINT entity_id IF NOT EXISTS"
            " FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE"
        )
        session.run(
            "CREATE VECTOR INDEX entity_embedding IF NOT EXISTS"
            " FOR (e:Entity) ON (e.embedding)"
            " OPTIONS {indexConfig: {"
            "  `vector.dimensions`: $dims,"
            "  `vector.similarity_function`: 'cosine'}}",
            dims=ontology.EMBEDDING_DIMENSIONS,
        )


def upsert_page(capture) -> None:
    with get_driver().session() as session:
        session.run(
            "MERGE (p:Page {url: $url})"
            " SET p.title = $title, p.captured_at = $captured_at,"
            "     p.content_hash = $content_hash",
            url=capture["url"],
            title=capture["title"],
            captured_at=capture["captured_at"],
            content_hash=capture["content_hash"],
        )


def upsert_entity(entity_id: int) -> None:
    """Mirror one canonical entity into the graph."""
    row = db.get_entity(entity_id)
    entity_type = row["entity_type"]
    if entity_type not in ontology.ENTITY_TYPES:
        raise ValueError(f"unknown entity type: {entity_type}")
    with get_driver().session() as session:
        session.run(
            f"MERGE (e:{ontology.ENTITY_BASE_LABEL} {{entity_id: $id}})"
            f" SET e:{entity_type},"
            "      e.canonical_name = $name, e.entity_type = $type,"
            "      e.aliases = $aliases, e.description = $description,"
            "      e.embedding = $embedding",
            id=row["id"],
            name=row["canonical_name"],
            type=entity_type,
            aliases=json.loads(row["aliases"]),
            description=row["description"],
            embedding=json.loads(row["embedding"]),
        )


def link_mention(page_url: str, entity_id: int) -> None:
    with get_driver().session() as session:
        session.run(
            "MATCH (p:Page {url: $url}), (e:Entity {entity_id: $id})"
            f" MERGE (p)-[:{ontology.MENTIONS}]->(e)",
            url=page_url,
            id=entity_id,
        )


def link_fact(
    source_id: int, rel_type: str, target_id: int,
    predicate: str | None, source_page_url: str,
) -> None:
    if rel_type not in ontology.RELATION_TYPES:
        raise ValueError(f"unknown relation type: {rel_type}")
    with get_driver().session() as session:
        session.run(
            "MATCH (a:Entity {entity_id: $src}), (b:Entity {entity_id: $tgt})"
            f" MERGE (a)-[r:{rel_type} {{{ontology.PROVENANCE_PROPERTY}: $url}}]->(b)"
            " SET r.predicate = $predicate",
            src=source_id,
            tgt=target_id,
            url=source_page_url,
            predicate=predicate,
        )


def promote_capture(capture_id: int) -> dict:
    """Promote one extracted capture into Neo4j."""
    capture = db.get_capture(capture_id)
    if capture is None:
        return {"capture_id": capture_id, "status": "missing"}
    if capture["extraction_status"] not in ("extracted", "promoted"):
        return {"capture_id": capture_id, "status": "not_extracted"}

    staged_entities, staged_rels = db.staged_for_capture(capture_id)
    try:
        upsert_page(capture)

        name_to_id: dict[str, int] = {}
        for staged in staged_entities:
            entity_id = resolution.resolve(
                capture_id,
                {
                    "name": staged["name"],
                    "type": staged["entity_type"],
                    "aliases": json.loads(staged["aliases"]),
                    "description": staged["description"],
                },
            )
            name_to_id[staged["name"]] = entity_id
            upsert_entity(entity_id)
            link_mention(capture["url"], entity_id)

        edges = 0
        for rel in staged_rels:
            src = name_to_id.get(rel["source_name"])
            tgt = name_to_id.get(rel["target_name"])
            if src is None or tgt is None or src == tgt:
                continue
            link_fact(src, rel["rel_type"], tgt, rel["predicate"], capture["url"])
            edges += 1
    except Exception as exc:
        db.set_extraction_status(
            capture_id, "failed_promotion", f"{type(exc).__name__}: {exc}"
        )
        return {"capture_id": capture_id, "status": "failed", "error": str(exc)}

    db.set_extraction_status(capture_id, "promoted", None)
    return {
        "capture_id": capture_id,
        "status": "promoted",
        "entities": len(name_to_id),
        "fact_edges": edges,
    }


def promote_extracted(limit: int = 20) -> list[dict]:
    """Promote extracted captures and retry failed promotions."""
    results = []
    for status in ("extracted", "failed_promotion"):
        for capture in db.captures_with_status(status, limit - len(results)):
            results.append(promote_capture(capture["id"]))
    return results
