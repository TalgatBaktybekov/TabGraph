"""Scheduled discovery jobs: bridges, tensions, and community summaries.

These run over already-promoted claims and entities — never at extraction
time — and only pairwise-compare with local embeddings; the LLM judge sees
at most MAX_JUDGED_PAIRS candidate pairs per run.
"""

import json
import math
from datetime import datetime
from urllib.parse import urlparse

from . import db, graph, llm, ontology

BRIDGE_SIMILARITY = 0.80
BRIDGES_PER_RUN = 10
TIME_GAP_CAP_DAYS = 30

TENSION_SIMILARITY = 0.75
MAX_JUDGED_PAIRS = 200

MIN_COMMUNITY_SIZE = 5
MAX_COMMUNITY_CLAIMS = 40


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _parse_when(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _load_claims() -> list[dict]:
    claims = []
    for row in db.all_claims():
        claims.append(
            {
                "id": row["id"],
                "statement": row["statement"],
                "evidence": row["evidence"],
                "page_url": row["page_url"],
                "captured_at": row["captured_at"],
                "embedding": json.loads(row["embedding"]),
                "entity_ids": set(json.loads(row["entity_ids"])),
            }
        )
    return claims


# --- Job 1: bridges -----------------------------------------------------

def _time_gap_weight(a: dict, b: dict) -> float:
    """Grows from 1.0 (same day) to 2.0 (TIME_GAP_CAP_DAYS+ apart)."""
    ta, tb = _parse_when(a["captured_at"]), _parse_when(b["captured_at"])
    if ta is None or tb is None:
        return 1.0
    days = min(abs((ta - tb).days), TIME_GAP_CAP_DAYS)
    return 1.0 + days / TIME_GAP_CAP_DAYS


def discover_bridges() -> dict:
    """Find non-obvious connections between claims from different pages."""
    claims = _load_claims()
    scored = []
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if a["page_url"] == b["page_url"]:
                continue
            similarity = _dot(a["embedding"], b["embedding"])
            if similarity < BRIDGE_SIMILARITY:
                continue
            source_distance = 1.5 if _domain(a["page_url"]) != _domain(b["page_url"]) else 1.0
            score = similarity * _time_gap_weight(a, b) * source_distance
            scored.append((score, similarity, a, b))
    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[:BRIDGES_PER_RUN]

    discovered_at = datetime.utcnow().isoformat(timespec="seconds")
    with graph.get_driver().session() as session:
        for score, similarity, a, b in top:
            session.run(
                f"MATCH (a:{ontology.CLAIM_LABEL} {{claim_id: $a}}),"
                f" (b:{ontology.CLAIM_LABEL} {{claim_id: $b}})"
                f" MERGE (a)-[r:{ontology.BRIDGE}]->(b)"
                " SET r.score = $score, r.similarity = $similarity,"
                "     r.discovered_at = $when",
                a=a["id"], b=b["id"],
                score=score, similarity=similarity, when=discovered_at,
            )
    db.record_job_run("discover_bridges", f"candidates={len(scored)} written={len(top)}")
    return {"job": "discover_bridges", "candidates": len(scored), "bridges": len(top)}


# --- Job 2: tensions ----------------------------------------------------

JUDGE_SYSTEM = """You compare two claims extracted from different web pages.
Classify their relationship as exactly one of:
- "agree": they assert compatible or mutually reinforcing things
- "disagree": they cannot both be true, or take opposing positions
- "unrelated": despite surface similarity, they talk past each other

Return ONLY a JSON object: {"verdict": "agree" | "disagree" | "unrelated"}"""


def discover_tensions() -> dict:
    """Find claim pairs where the user's sources disagree."""
    claims = _load_claims()
    candidates = []
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if a["page_url"] == b["page_url"]:
                continue
            if not (a["entity_ids"] & b["entity_ids"]):
                continue
            similarity = _dot(a["embedding"], b["embedding"])
            if similarity >= TENSION_SIMILARITY:
                candidates.append((similarity, a, b))
    candidates.sort(key=lambda item: item[0], reverse=True)
    candidates = candidates[:MAX_JUDGED_PAIRS]

    discovered_at = datetime.utcnow().isoformat(timespec="seconds")
    contradictions = 0
    for similarity, a, b in candidates:
        try:
            raw = llm.generate_json(
                llm.discovery_model(),
                JUDGE_SYSTEM,
                f'Claim A: "{a["statement"]}"\nClaim B: "{b["statement"]}"',
            )
            verdict = json.loads(raw).get("verdict")
        except Exception:
            continue
        if verdict != "disagree":
            continue
        with graph.get_driver().session() as session:
            # ON CREATE only: re-runs must not overwrite a user's confirm/dismiss.
            session.run(
                f"MATCH (a:{ontology.CLAIM_LABEL} {{claim_id: $a}}),"
                f" (b:{ontology.CLAIM_LABEL} {{claim_id: $b}})"
                f" MERGE (a)-[r:{ontology.CONTRADICTS}]->(b)"
                " ON CREATE SET r.review_status = 'proposed'"
                " SET r.similarity = $similarity, r.discovered_at = $when",
                a=a["id"], b=b["id"], similarity=similarity, when=discovered_at,
            )
        contradictions += 1
    db.record_job_run(
        "discover_tensions", f"judged={len(candidates)} contradictions={contradictions}"
    )
    return {
        "job": "discover_tensions",
        "judged_pairs": len(candidates),
        "contradictions": contradictions,
    }


# --- Job 3: community summaries ----------------------------------------

def _co_mention_graph() -> dict[tuple[int, int], int]:
    """Entity co-mention edges weighted by salience (primary=3, secondary=1)."""
    weights = {name: w for name, w in ontology.SALIENCE_WEIGHTS.items()}
    query = (
        f"MATCH (p:Page)-[m1:{ontology.MENTIONS}]->(a:Entity),"
        f" (p)-[m2:{ontology.MENTIONS}]->(b:Entity)"
        " WHERE a.entity_id < b.entity_id"
        " RETURN a.entity_id AS a, b.entity_id AS b,"
        "        m1.salience AS sa, m2.salience AS sb"
    )
    edges: dict[tuple[int, int], int] = {}
    with graph.get_driver().session(default_access_mode="READ") as session:
        for record in session.run(query):
            w = min(
                weights.get(record["sa"] or "passing", 0),
                weights.get(record["sb"] or "passing", 0),
            )
            if w <= 0:
                continue
            key = (record["a"], record["b"])
            edges[key] = edges.get(key, 0) + w
    return edges


def _detect_communities(edges: dict[tuple[int, int], int]) -> list[set[int]]:
    """Leiden if available (leidenalg + igraph), else networkx Louvain."""
    nodes = sorted({n for pair in edges for n in pair})
    if not nodes:
        return []
    try:
        import igraph
        import leidenalg

        index = {n: i for i, n in enumerate(nodes)}
        g = igraph.Graph(
            n=len(nodes),
            edges=[(index[a], index[b]) for a, b in edges],
        )
        g.es["weight"] = list(edges.values())
        partition = leidenalg.find_partition(
            g, leidenalg.ModularityVertexPartition, weights="weight"
        )
        return [{nodes[i] for i in community} for community in partition]
    except ImportError:
        import networkx as nx

        g = nx.Graph()
        for (a, b), w in edges.items():
            g.add_edge(a, b, weight=w)
        return [set(c) for c in nx.community.louvain_communities(g, weight="weight", seed=42)]


SUMMARY_SYSTEM = """You summarize one cluster of topics from a user's browsing
knowledge graph. You are given the cluster's entities and the claims captured
about them. Write a 3-5 sentence summary of what the user has learned about
this cluster, using ONLY the provided claims — do not add outside knowledge.
Plain prose, no markdown, no preamble."""


def summarize_communities() -> dict:
    """Leiden communities over the co-mention graph, summarized from claims."""
    edges = _co_mention_graph()
    communities = [c for c in _detect_communities(edges) if len(c) >= MIN_COMMUNITY_SIZE]

    claims = _load_claims()
    generated_at = datetime.utcnow().isoformat(timespec="seconds")
    written = 0
    with graph.get_driver().session() as session:
        # Summaries are regenerated, never edited.
        session.run(f"MATCH (s:{ontology.COMMUNITY_SUMMARY_LABEL}) DETACH DELETE s")
        for community in communities:
            community_claims = [
                c for c in claims if c["entity_ids"] & community
            ][:MAX_COMMUNITY_CLAIMS]
            if not community_claims:
                continue
            names = session.run(
                "MATCH (e:Entity) WHERE e.entity_id IN $ids"
                " RETURN e.canonical_name AS name",
                ids=sorted(community),
            ).value("name")
            claim_lines = "\n".join(f"- {c['statement']}" for c in community_claims)
            try:
                summary = llm.generate_text(
                    llm.discovery_model(),
                    SUMMARY_SYSTEM,
                    f"Entities: {', '.join(names)}\n\nClaims:\n{claim_lines}",
                ).strip()
            except Exception:
                continue
            if not summary:
                continue
            written += 1
            session.run(
                f"CREATE (s:{ontology.COMMUNITY_SUMMARY_LABEL} {{"
                "  community_id: $cid, summary: $summary,"
                "  generated_at: $when, size: $size,"
                "  ontology_version: $version}})"
                " WITH s"
                " UNWIND $ids AS eid"
                " MATCH (e:Entity {entity_id: eid})"
                f" MERGE (e)-[:{ontology.MEMBER_OF_COMMUNITY}]->(s)",
                cid=written,
                summary=summary,
                when=generated_at,
                size=len(community),
                version=ontology.ONTOLOGY_VERSION,
                ids=sorted(community),
            )
    db.record_job_run(
        "summarize_communities",
        f"communities={len(communities)} summarized={written}",
    )
    return {
        "job": "summarize_communities",
        "communities": len(communities),
        "summarized": written,
    }


# --- Scheduler ----------------------------------------------------------

JOB_INTERVALS_DAYS = {
    "discover_bridges": 7,
    "discover_tensions": 7,
    "summarize_communities": 30,
}
JOBS = {
    "discover_bridges": discover_bridges,
    "discover_tensions": discover_tensions,
    "summarize_communities": summarize_communities,
}


def run_due_jobs() -> list[dict]:
    """Run every discovery job whose interval has elapsed."""
    results = []
    now = datetime.utcnow()
    for name, interval_days in JOB_INTERVALS_DAYS.items():
        last = db.job_last_run(name)
        last_dt = _parse_when(last) if last else None
        if last_dt is not None and (now - last_dt).days < interval_days:
            continue
        try:
            results.append(JOBS[name]())
        except Exception as exc:
            results.append({"job": name, "error": f"{type(exc).__name__}: {exc}"})
    return results


# --- Surfacing (for the side panel) --------------------------------------

def recent_discoveries(limit: int = 10) -> dict:
    """Bridges and contradictions with both source pages, for the side panel.

    Dismissed tensions are filtered out; proposed ones carry claim IRIs so the
    panel can render confirm/dismiss controls.
    """
    pair_query = """
    MATCH (a:Claim)-[r:{rel}]->(b:Claim),
          (a)-[:ASSERTED_IN]->(pa:Page), (b)-[:ASSERTED_IN]->(pb:Page)
    {where}
    RETURN a.statement AS statement_a, a.evidence AS evidence_a,
           pa.title AS title_a, pa.url AS url_a, pa.captured_at AS captured_a,
           b.statement AS statement_b, b.evidence AS evidence_b,
           pb.title AS title_b, pb.url AS url_b, pb.captured_at AS captured_b,
           r.discovered_at AS discovered_at{extra}
    ORDER BY r.discovered_at DESC{order_extra}
    LIMIT $limit
    """
    with graph.get_driver().session(default_access_mode="READ") as session:
        bridges = session.run(
            pair_query.format(
                rel=ontology.BRIDGE, where="",
                extra=", r.score AS score", order_extra=", r.score DESC"
            ),
            limit=limit,
        ).data()
        tensions = session.run(
            pair_query.format(
                rel=ontology.CONTRADICTS,
                where="WHERE coalesce(r.review_status, 'proposed') <> 'dismissed'",
                extra=(
                    ", coalesce(r.review_status, 'proposed') AS review_status"
                    ", a.iri AS iri_a, b.iri AS iri_b"
                ),
                order_extra="",
            ),
            limit=limit,
        ).data()
        summaries = session.run(
            f"MATCH (s:{ontology.COMMUNITY_SUMMARY_LABEL})"
            f" OPTIONAL MATCH (e:Entity)-[:{ontology.MEMBER_OF_COMMUNITY}]->(s)"
            " WITH s, collect(e.canonical_name)[..8] AS members"
            " RETURN s.community_id AS community_id, s.summary AS summary,"
            "        s.generated_at AS generated_at, s.size AS size, members"
            " ORDER BY s.size DESC LIMIT $limit",
            limit=limit,
        ).data()
    return {
        "bridges": bridges,
        "tensions": tensions,
        "communities": summaries,
        "tension_review": db.tension_review_stats(),
    }


def review_tension(iri_a: str, iri_b: str, decision: str) -> dict:
    """Record the user's verdict on a proposed contradiction."""
    if decision not in ("confirmed", "dismissed"):
        return {"status": "invalid_decision", "decision": decision}
    with graph.get_driver().session() as session:
        result = session.run(
            f"MATCH (a:{ontology.CLAIM_LABEL} {{iri: $a}})"
            f"-[r:{ontology.CONTRADICTS}]-"
            f"(b:{ontology.CLAIM_LABEL} {{iri: $b}})"
            " SET r.review_status = $decision, r.reviewed_at = $when"
            " RETURN count(r) AS n",
            a=iri_a,
            b=iri_b,
            decision=decision,
            when=datetime.utcnow().isoformat(timespec="seconds"),
        ).single()
    if not result or result["n"] == 0:
        return {"status": "not_found"}
    db.log_tension_review(iri_a, iri_b, decision)
    return {"status": decision, "edges_updated": result["n"]}
