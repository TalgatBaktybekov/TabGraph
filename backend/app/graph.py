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
        session.run(
            "CREATE CONSTRAINT claim_id IF NOT EXISTS"
            f" FOR (c:{ontology.CLAIM_LABEL}) REQUIRE c.claim_id IS UNIQUE"
        )
        session.run(
            "CREATE CONSTRAINT claim_iri IF NOT EXISTS"
            f" FOR (c:{ontology.CLAIM_LABEL}) REQUIRE c.iri IS UNIQUE"
        )
        session.run(
            "CREATE VECTOR INDEX claim_embedding IF NOT EXISTS"
            f" FOR (c:{ontology.CLAIM_LABEL}) ON (c.embedding)"
            " OPTIONS {indexConfig: {"
            "  `vector.dimensions`: $dims,"
            "  `vector.similarity_function`: 'cosine'}}",
            dims=ontology.EMBEDDING_DIMENSIONS,
        )
        session.run(
            "CREATE CONSTRAINT community_id IF NOT EXISTS"
            f" FOR (s:{ontology.COMMUNITY_SUMMARY_LABEL})"
            " REQUIRE s.community_id IS UNIQUE"
        )


def upsert_page(capture) -> str:
    """Mirror one capture as a Page node; returns the canonical page URL."""
    page_url = capture["canonical_url"] or capture["url"]
    with get_driver().session() as session:
        session.run(
            "MERGE (p:Page {url: $url})"
            " SET p.title = $title, p.captured_at = $captured_at,"
            "     p.content_hash = $content_hash, p.original_url = $original_url,"
            "     p.iri = $iri, p.ontology_version = $version",
            url=page_url,
            title=capture["title"],
            captured_at=capture["captured_at"],
            content_hash=capture["content_hash"],
            original_url=capture["url"],
            iri=ontology.page_iri(page_url),
            version=ontology.ONTOLOGY_VERSION,
        )
    return page_url


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
            "      e.embedding = $embedding, e.iri = $iri,"
            "      e.ontology_version = $version",
            id=row["id"],
            name=row["canonical_name"],
            type=entity_type,
            aliases=json.loads(row["aliases"]),
            description=row["description"],
            embedding=json.loads(row["embedding"]),
            iri=row["iri"],
            version=ontology.ONTOLOGY_VERSION,
        )


def link_mention(page_url: str, entity_id: int, salience: str = "passing") -> None:
    with get_driver().session() as session:
        session.run(
            "MATCH (p:Page {url: $url}), (e:Entity {entity_id: $id})"
            f" MERGE (p)-[m:{ontology.MENTIONS}]->(e)"
            " SET m.salience = $salience",
            url=page_url,
            id=entity_id,
            salience=salience,
        )


def upsert_claim(
    iri: str,
    claim_id: int,
    statement: str,
    stance: str,
    evidence: str,
    captured_at: str,
    embedding: list[float],
    confidence: float,
    page_url: str,
    entity_ids: list[int],
) -> None:
    """Write one Claim node with its ABOUT and ASSERTED_IN edges.

    Keyed on the stable IRI so re-promotion updates the node in place and
    edges hanging off it (BRIDGE, CONTRADICTS) survive.
    """
    with get_driver().session() as session:
        session.run(
            f"MERGE (c:{ontology.CLAIM_LABEL} {{iri: $iri}})"
            " SET c.claim_id = $id, c.statement = $statement, c.stance = $stance,"
            "     c.evidence = $evidence, c.captured_at = $captured_at,"
            "     c.embedding = $embedding, c.confidence = $confidence,"
            "     c.ontology_version = $version"
            " WITH c"
            " MATCH (p:Page {url: $url})"
            f" MERGE (c)-[:{ontology.ASSERTED_IN}]->(p)"
            " WITH c"
            " UNWIND $entity_ids AS eid"
            " MATCH (e:Entity {entity_id: eid})"
            f" MERGE (c)-[:{ontology.ABOUT}]->(e)",
            iri=iri,
            id=claim_id,
            statement=statement,
            stance=stance,
            evidence=evidence,
            captured_at=captured_at,
            embedding=embedding,
            confidence=confidence,
            version=ontology.ONTOLOGY_VERSION,
            url=page_url,
            entity_ids=entity_ids,
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


def link_project(page_url: str, project) -> None:
    """Attach a Page to its Project (created at the promotion gesture)."""
    with get_driver().session() as session:
        session.run(
            f"MERGE (pr:{ontology.PROJECT_LABEL} {{name: $name}})"
            " SET pr.iri = $iri, pr.ontology_version = $version"
            " WITH pr MATCH (p:Page {url: $url})"
            f" MERGE (p)-[:{ontology.IN_PROJECT}]->(pr)",
            name=project["name"],
            iri=project["iri"],
            version=ontology.ONTOLOGY_VERSION,
            url=page_url,
        )


def load_capture(capture_id: int) -> dict:
    """Load one extracted capture into Neo4j."""
    capture = db.get_capture(capture_id)
    if capture is None:
        return {"capture_id": capture_id, "status": "missing"}
    if capture["extraction_status"] not in ("extracted", "loaded"):
        return {"capture_id": capture_id, "status": "not_extracted"}

    staged_entities, staged_rels = db.staged_for_capture(capture_id)
    try:
        page_url = upsert_page(capture)
        if capture["project_id"]:
            project = db.get_project(capture["project_id"])
            if project is not None:
                link_project(page_url, project)

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
            link_mention(page_url, entity_id, staged["salience"])

        edges = 0
        for rel in staged_rels:
            src = name_to_id.get(rel["source_name"])
            tgt = name_to_id.get(rel["target_name"])
            if src is None or tgt is None or src == tgt:
                continue
            link_fact(src, rel["rel_type"], tgt, rel["predicate"], page_url)
            edges += 1

        claims = load_claims(capture, name_to_id, page_url)
    except Exception as exc:
        db.set_extraction_status(
            capture_id, "failed_load", f"{type(exc).__name__}: {exc}"
        )
        return {"capture_id": capture_id, "status": "failed", "error": str(exc)}

    db.set_extraction_status(capture_id, "loaded", None)
    return {
        "capture_id": capture_id,
        "status": "loaded",
        "entities": len(name_to_id),
        "fact_edges": edges,
        "claims": claims,
    }


def load_claims(capture, name_to_id: dict[str, int], page_url: str) -> int:
    """Promote staged claims for one capture into SQLite + Neo4j."""
    staged = db.staged_claims_for_capture(capture["id"])
    rows = []
    for claim in staged:
        entity_ids = sorted(
            {
                name_to_id[name]
                for name in json.loads(claim["entities"])
                if name in name_to_id
            }
        )
        if not entity_ids:  # ABOUT is 1..n; a claim with no resolvable entity is invalid
            continue
        rows.append(
            {
                "page_url": page_url,
                "statement": claim["statement"],
                "stance": claim["stance"],
                "evidence": claim["evidence"],
                "captured_at": capture["captured_at"],
                "embedding": resolution.embed(claim["statement"]),
                "confidence": claim["confidence"],
                "entity_ids": entity_ids,
            }
        )
    claim_recs = db.replace_claims_for_capture(capture["id"], rows)
    # Drop only Claim nodes that no longer exist for this page; surviving
    # statements keep their IRI (and their BRIDGE/CONTRADICTS edges).
    with get_driver().session() as session:
        session.run(
            f"MATCH (c:{ontology.CLAIM_LABEL})-[:{ontology.ASSERTED_IN}]->"
            "(p:Page {url: $url})"
            " WHERE c.iri IS NULL OR NOT c.iri IN $iris"
            " DETACH DELETE c",
            url=page_url,
            iris=[rec["iri"] for rec in claim_recs],
        )
    for rec, row in zip(claim_recs, rows):
        upsert_claim(
            rec["iri"],
            rec["id"],
            row["statement"],
            row["stance"],
            row["evidence"],
            row["captured_at"],
            row["embedding"],
            row["confidence"],
            row["page_url"],
            row["entity_ids"],
        )
    return len(rows)


def load_extracted(limit: int = 20) -> list[dict]:
    """Load extracted captures and retry failed loads."""
    results = []
    for status in ("extracted", "failed_load"):
        for capture in db.captures_with_status(
            status, limit - len(results), evidence_status="promoted"
        ):
            results.append(load_capture(capture["id"]))
    return results
