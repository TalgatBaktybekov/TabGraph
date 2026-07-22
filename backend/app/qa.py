"""Answer questions: Cypher for lookups, claim synthesis for knowledge
questions, subgraph retrieval as the fallback."""

import re

from . import chunks, graph, llm, ontology, resolution

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
- Claim nodes (:Claim) hold verbatim statements extracted from pages, with
  properties statement, stance, evidence, confidence, captured_at. Edges:
  (c:Claim)-[:ABOUT]->(e:Entity) and (c:Claim)-[:ASSERTED_IN]->(p:Page).
  Specific numbers, amounts, dates, and outcomes usually live in claim
  statements — for "how much/how many/what did X say" questions, search
  claims about the relevant entities.
- Never return embedding properties.

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

Q: How much power does the Acme deal cover?
MATCH (c:Claim)-[:ABOUT]->(e:Entity)
WHERE toLower(e.canonical_name) CONTAINS 'acme'
   OR any(a IN e.aliases WHERE toLower(a) CONTAINS 'acme')
MATCH (c)-[:ASSERTED_IN]->(p:Page)
RETURN c.statement AS statement, p.url AS source

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
        "The query results come from a graph query that already encodes the"
        " question's conditions — treat them as the authoritative result and"
        " report them directly. Answer the user's question in a short, direct"
        " way using ONLY the query results provided. Mention source URLs when"
        " present. If the results do not actually contain the information the"
        " question asks for, reply with exactly NO_ANSWER.",
        f"Question: {question}\n\nQuery results:\n{rows}",
    ).strip()
    if not answer or answer.startswith("NO_ANSWER"):
        return None  # rows didn't answer the question; try the next path
    return {"path": "cypher", "answer": answer, "cypher": cypher, "rows": rows}


NEIGHBORHOOD_QUERY = """
MATCH (e:Entity) WHERE e.entity_id IN $ids
MATCH path = (e)-[*1..2]-(x)
UNWIND relationships(path) AS r
WITH DISTINCT r
MATCH (a)-[r]->(b)
RETURN coalesce(a.canonical_name, a.statement, a.url) AS source,
       type(r) AS relation,
       r.predicate AS predicate,
       coalesce(b.canonical_name, b.statement, b.url) AS target,
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


# --- Synthesis path (v1.1): answer from claims, with citations -----------

SYNTHESIS_TOP_K = 15

ROUTER_SYSTEM = """Classify a question asked over a personal knowledge graph
built from the user's browsing. Reply with exactly one word:
- "lookup": a precise, structured lookup a graph query can answer — who/where/
  which pages/what relations, listing or locating specific entities or pages.
- "synthesis": asks what is known/learned/true about a topic, asks for a
  comparison, opinion landscape, or anything a graph query can't express.
No other output."""


def _route(question: str) -> str:
    try:
        verdict = llm.generate_text(llm.qa_model(), ROUTER_SYSTEM, question)
        return "synthesis" if "synthesis" in verdict.lower() else "lookup"
    except Exception:
        return "lookup"


def _retrieve_claims(question: str, k: int = SYNTHESIS_TOP_K) -> list[dict]:
    question_vec = resolution.embed(question)
    query = f"""
    CALL db.index.vector.queryNodes('claim_embedding', $k, $vec)
    YIELD node, score
    MATCH (node)-[:{ontology.ASSERTED_IN}]->(p:Page)
    OPTIONAL MATCH (node)-[:{ontology.ABOUT}]->(e:Entity)
    WITH node, score, p, collect(e.entity_id) AS entity_ids
    RETURN node.claim_id AS claim_id, node.statement AS statement,
           node.stance AS stance, node.evidence AS evidence,
           p.title AS page_title, p.url AS page_url,
           p.captured_at AS captured_at, entity_ids, score
    ORDER BY score DESC
    """
    with graph.get_driver().session(default_access_mode="READ") as session:
        return session.run(query, k=k, vec=question_vec).data()


MAX_SYNTHESIS_FACTS = 40

def _entity_facts(entity_ids: list[int], limit: int = MAX_SYNTHESIS_FACTS) -> list[dict]:
    """Entity-to-entity edges touching the given entities (claims can't
    express relationship facts, so synthesis needs these too)."""
    if not entity_ids:
        return []
    with graph.get_driver().session(default_access_mode="READ") as session:
        return session.run(
            "MATCH (a:Entity)-[r]->(b:Entity)"
            " WHERE a.entity_id IN $ids OR b.entity_id IN $ids"
            " RETURN DISTINCT a.canonical_name AS source, type(r) AS relation,"
            "        r.predicate AS predicate, b.canonical_name AS target,"
            f"        r.{ontology.PROVENANCE_PROPERTY} AS provenance"
            " LIMIT $limit",
            ids=entity_ids,
            limit=limit,
        ).data()


def _matching_summaries(entity_ids: list[int]) -> list[dict]:
    if not entity_ids:
        return []
    with graph.get_driver().session(default_access_mode="READ") as session:
        return session.run(
            f"MATCH (e:Entity)-[:{ontology.MEMBER_OF_COMMUNITY}]->"
            f"(s:{ontology.COMMUNITY_SUMMARY_LABEL})"
            " WHERE e.entity_id IN $ids"
            " RETURN DISTINCT s.community_id AS community_id,"
            "        s.summary AS summary, s.generated_at AS generated_at",
            ids=entity_ids,
        ).data()


SYNTHESIS_SYSTEM = """You answer a question using ONLY the numbered items
(claims and graph facts, plus optional community summaries) retrieved from a
knowledge graph of the user's past browsing.

HARD RULE: every substantive sentence of your answer must cite at least one
item by its number in square brackets, e.g. [2] or [1][4]. Sentences that
are pure transitions need no citation. Never cite a number that was not
provided, and never use outside knowledge. If the items don't answer the
question, reply with exactly NO_ANSWER.

Be direct and concise. Plain prose, no markdown headers."""


def synthesis_path(question: str) -> dict | None:
    """Answer knowledge questions from claims, with per-sentence citations."""
    try:
        claims = _retrieve_claims(question)
    except Exception:
        return None
    if not claims:
        return None

    seed_entities = sorted({eid for c in claims for eid in c["entity_ids"]})
    summaries = _matching_summaries(seed_entities)
    facts = _entity_facts(seed_entities)

    # One numbered space for claims and graph facts so both are citable.
    entries = [
        {
            "statement": c["statement"],
            "page_title": c["page_title"],
            "page_url": c["page_url"],
            "captured_at": c["captured_at"],
        }
        for c in claims
    ]
    lines = []
    for i, c in enumerate(claims, 1):
        stance = f" (page {c['stance']} this)" if c["stance"] != "asserts" else ""
        lines.append(
            f"[{i}] {c['statement']}{stance}"
            f" — from \"{c['page_title']}\", {c['captured_at']}"
        )
    context = "Claims:\n" + "\n".join(lines)
    if facts:
        fact_lines = []
        for i, f in enumerate(facts, len(claims) + 1):
            relation = f["relation"]
            if f["predicate"]:
                relation += f" ({f['predicate']})"
            rendered = f"{f['source']} —{relation}—> {f['target']}"
            fact_lines.append(f"[{i}] {rendered}")
            entries.append(
                {
                    "statement": rendered,
                    "page_title": f["provenance"],
                    "page_url": f["provenance"],
                    "captured_at": None,
                }
            )
        context += (
            "\n\nGraph facts (relationships between entities):\n"
            + "\n".join(fact_lines)
        )
    if summaries:
        context += "\n\nCommunity summaries (background, cite claims not these):\n"
        context += "\n".join(f"- {s['summary']}" for s in summaries)

    answer = llm.generate_text(
        llm.qa_model(), SYNTHESIS_SYSTEM, f"Question: {question}\n\n{context}"
    ).strip()
    if not answer or answer.startswith("NO_ANSWER"):
        return None  # let ask() fall through to the subgraph path

    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer) if 0 < int(n) <= len(entries)}
    citations = [
        {"n": i, **entry}
        for i, entry in enumerate(entries, 1)
        if i in cited
    ]
    related = [
        dict(entry)
        for i, entry in enumerate(entries, 1)
        if i not in cited
    ]
    return {
        "path": "synthesis",
        "answer": answer,
        "citations": citations,
        "related": related,
        "community_summaries": summaries,
    }


def ask(question: str) -> dict:
    """Route: text-to-Cypher (lookups) → synthesis (knowledge) → subgraph →
    chunk-level recall (works over staged pages the graph hasn't seen)."""
    route = _route(question)
    if route == "lookup":
        result = try_cypher_path(question)
        if result is not None:
            return result
    result = synthesis_path(question)
    if result is not None:
        return result
    result = subgraph_path(question)
    if result["facts_used"] == 0:
        recalled = chunks.recall(question)
        if recalled is not None:
            return recalled
    return result
