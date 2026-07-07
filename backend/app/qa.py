"""Answer questions with Cypher first, then a subgraph fallback."""

import re

from . import graph, llm, ontology, resolution

CYPHER_SYSTEM = f"""You translate a user's question into ONE read-only Cypher query
for a Neo4j 5 knowledge graph built from the user's browsing.

{ontology.schema_for_prompt()}

Graph specifics:
- Every entity node has label :Entity plus its type label, e.g. (:Entity:Person).
- Entity properties: entity_id, canonical_name, entity_type, aliases (list of
  strings), description. Match names case-insensitively and generously, e.g.
  toLower(e.canonical_name) CONTAINS toLower('...') OR any alias matches.
- Page properties: url, title, captured_at.
- RELATED_TO edges carry `predicate`; every fact edge carries `source_page_url`.

Examples:

Q: Where does Ada Lovelace work?
MATCH (p:Person)-[:WORKS_AT]->(o:Organization)
WHERE toLower(p.canonical_name) CONTAINS 'ada lovelace'
   OR any(a IN p.aliases WHERE toLower(a) CONTAINS 'ada lovelace')
RETURN o.canonical_name AS organization

Q: Which pages mention Neo4j?
MATCH (pg:Page)-[:MENTIONS]->(e:Entity)
WHERE toLower(e.canonical_name) CONTAINS 'neo4j'
   OR any(a IN e.aliases WHERE toLower(a) CONTAINS 'neo4j')
RETURN pg.url AS url, pg.title AS title

Q: What products did Google create?
MATCH (pr:Product)-[:CREATED_BY]->(c)
WHERE toLower(c.canonical_name) CONTAINS 'google'
RETURN pr.canonical_name AS product, pr.description AS description

Q: What is connected to knowledge graphs?
MATCH (e:Entity)-[r]-(n:Entity)
WHERE toLower(e.canonical_name) CONTAINS 'knowledge graph'
RETURN n.canonical_name AS name, type(r) AS relation, r.predicate AS predicate,
       r.source_page_url AS source
LIMIT 25

Rules: output ONLY the Cypher query, no fences, no commentary. Read-only:
MATCH/WHERE/RETURN/WITH/UNWIND/ORDER/LIMIT only. Always RETURN named columns.
Add LIMIT 50 unless the question implies fewer."""

# Anything that could write, administer, or call procedures is rejected.
FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|CALL|FOREACH|"
    r"apoc|dbms)\b",
    re.IGNORECASE,
)

VECTOR_TOP_K = 5
MAX_FACTS = 200


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _jsonable(value):
    """Convert Neo4j driver values to JSON-friendly data."""
    try:
        from neo4j.graph import Node, Relationship
    except ImportError:
        return value
    if isinstance(value, Node):
        props = {k: v for k, v in value.items() if k != "embedding"}
        return {"labels": list(value.labels), **props}
    if isinstance(value, Relationship):
        return {"type": value.type, **dict(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _run_readonly(cypher: str) -> list[dict]:
    with graph.get_driver().session(default_access_mode="READ") as session:
        result = session.run(cypher, timeout=15)
        return [
            {k: _jsonable(v) for k, v in record.items()}
            for record in result
        ][:50]


def try_cypher_path(question: str) -> dict | None:
    """Try the Cypher path first."""
    cypher = _strip_fences(
        llm.generate_text(llm.qa_model(), CYPHER_SYSTEM, question)
    )
    if not cypher or FORBIDDEN.search(cypher):
        return None
    try:
        rows = _run_readonly(cypher)
    except Exception:
        return None
    if not rows:
        return None

    answer = llm.generate_text(
        llm.qa_model(),
        "Answer the user's question in a short, direct way using ONLY the"
        " query results provided. Mention source URLs when present.",
        f"Question: {question}\n\nQuery results:\n{rows}",
    )
    return {"path": "cypher", "answer": answer.strip(), "cypher": cypher, "rows": rows}


NEIGHBORHOOD_QUERY = """
MATCH (e:Entity) WHERE e.entity_id IN $ids
MATCH path = (e)-[*1..2]-(x)
UNWIND relationships(path) AS r
WITH DISTINCT r
MATCH (a)-[r]->(b)
RETURN coalesce(a.canonical_name, a.url) AS source,
       type(r) AS relation,
       r.predicate AS predicate,
       coalesce(b.canonical_name, b.url) AS target,
       r.source_page_url AS provenance
LIMIT $max_facts
"""


def subgraph_path(question: str) -> dict:
    question_vec = resolution.embed(question)
    with graph.get_driver().session(default_access_mode="READ") as session:
        hits = session.run(
            "CALL db.index.vector.queryNodes('entity_embedding', $k, $vec)"
            " YIELD node, score"
            " RETURN node.entity_id AS id, node.canonical_name AS name, score",
            k=VECTOR_TOP_K,
            vec=question_vec,
        ).data()
        facts = session.run(
            NEIGHBORHOOD_QUERY,
            ids=[h["id"] for h in hits],
            max_facts=MAX_FACTS,
        ).data()

    if not facts:
        return {
            "path": "subgraph",
            "answer": "The graph doesn't contain anything related to that yet"
                      " — browse some pages on the topic first.",
            "retrieved_entities": hits,
            "facts_used": 0,
        }

    lines = []
    for f in facts:
        relation = f["relation"]
        if f["predicate"]:
            relation += f" ({f['predicate']})"
        line = f"- {f['source']} -{relation}-> {f['target']}"
        if f["provenance"]:
            line += f" [source: {f['provenance']}]"
        lines.append(line)
    fact_lines = "\n".join(lines)
    answer = llm.generate_text(
        llm.qa_model(),
        "Answer the user's question using ONLY the graph facts provided"
        " (edges from a knowledge graph of the user's browsing; MENTIONS"
        " means a page mentioned an entity). Be direct; cite source URLs"
        " when they support the answer. If the facts don't answer it, say so.",
        f"Question: {question}\n\nGraph facts:\n{fact_lines}",
    )
    return {
        "path": "subgraph",
        "answer": answer.strip(),
        "retrieved_entities": hits,
        "facts_used": len(facts),
    }


def ask(question: str) -> dict:
    result = try_cypher_path(question)
    if result is not None:
        return result
    return subgraph_path(question)
