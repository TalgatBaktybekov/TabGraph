"""SQLite storage for captures, staged extractions, entities, and merge logs."""

import hashlib
import json
import sqlite3
from pathlib import Path

from . import ontology, urlnorm

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tabgraph.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT NOT NULL,
    canonical_url     TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    text              TEXT NOT NULL DEFAULT '',
    raw_html          TEXT NOT NULL DEFAULT '',
    content_hash      TEXT NOT NULL,
    captured_at       TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    extraction_error  TEXT,
    evidence_status   TEXT NOT NULL DEFAULT 'staged',
    project_id        INTEGER REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    iri        TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id INTEGER NOT NULL REFERENCES captures(id),
    seq        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chunks_capture ON chunks(capture_id);

CREATE TABLE IF NOT EXISTS tension_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    iri_a      TEXT NOT NULL,
    iri_b      TEXT NOT NULL,
    decision   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS staged_entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    name        TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    aliases     TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    salience    TEXT NOT NULL DEFAULT 'passing',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_staged_entities_capture
    ON staged_entities(capture_id);

CREATE TABLE IF NOT EXISTS staged_relationships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    source_name TEXT NOT NULL,
    rel_type    TEXT NOT NULL,
    target_name TEXT NOT NULL,
    predicate   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_staged_relationships_capture
    ON staged_relationships(capture_id);

CREATE TABLE IF NOT EXISTS candidate_types (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id    INTEGER NOT NULL REFERENCES captures(id),
    name          TEXT NOT NULL,
    proposed_type TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_candidate_types_capture
    ON candidate_types(capture_id);

CREATE TABLE IF NOT EXISTS entities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    iri            TEXT NOT NULL DEFAULT '',
    canonical_name TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    aliases        TEXT NOT NULL DEFAULT '[]',
    description    TEXT NOT NULL DEFAULT '',
    embedding      TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

CREATE TABLE IF NOT EXISTS staged_claims (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id INTEGER NOT NULL REFERENCES captures(id),
    statement  TEXT NOT NULL,
    stance     TEXT NOT NULL DEFAULT 'asserts',
    evidence   TEXT NOT NULL DEFAULT '',
    entities   TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_staged_claims_capture
    ON staged_claims(capture_id);

CREATE TABLE IF NOT EXISTS claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    iri         TEXT NOT NULL DEFAULT '',
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    page_url    TEXT NOT NULL,
    statement   TEXT NOT NULL,
    stance      TEXT NOT NULL DEFAULT 'asserts',
    evidence    TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL,
    embedding   TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    entity_ids  TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_capture ON claims(capture_id);

CREATE TABLE IF NOT EXISTS job_runs (
    job_name   TEXT PRIMARY KEY,
    last_run   TEXT NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS merge_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id        INTEGER NOT NULL,
    staged_name       TEXT NOT NULL,
    entity_type       TEXT NOT NULL,
    decision          TEXT NOT NULL,
    matched_entity_id INTEGER,
    score             REAL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def content_hash(url: str, text: str) -> str:
    """Return the dedupe hash for one capture."""
    return hashlib.sha256(f"{url}\n{text}".encode()).hexdigest()


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_captures_hash"
            " ON captures(content_hash)"
        )


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade older databases in place."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(captures)")}
    if "extraction_status" not in columns:
        conn.execute(
            "ALTER TABLE captures ADD COLUMN"
            " extraction_status TEXT NOT NULL DEFAULT 'pending'"
        )
    if "extraction_error" not in columns:
        conn.execute("ALTER TABLE captures ADD COLUMN extraction_error TEXT")

    staged_cols = {row["name"] for row in conn.execute("PRAGMA table_info(staged_entities)")}
    if staged_cols and "salience" not in staged_cols:
        conn.execute(
            "ALTER TABLE staged_entities ADD COLUMN"
            " salience TEXT NOT NULL DEFAULT 'passing'"
        )

    # v1.2: canonical URLs + raw HTML. Hashes are recomputed on the canonical
    # URL, so the unique index must be dropped first and dedupe re-run below.
    needs_rehash = "canonical_url" not in columns
    if needs_rehash:
        conn.execute(
            "ALTER TABLE captures ADD COLUMN canonical_url TEXT NOT NULL DEFAULT ''"
        )
    if "raw_html" not in columns:
        conn.execute(
            "ALTER TABLE captures ADD COLUMN raw_html TEXT NOT NULL DEFAULT ''"
        )
    if needs_rehash:
        conn.execute("DROP INDEX IF EXISTS uq_captures_hash")
        for row in conn.execute("SELECT id, url, text FROM captures").fetchall():
            canonical = urlnorm.canonicalize(row["url"])
            conn.execute(
                "UPDATE captures SET canonical_url = ?, content_hash = ?"
                " WHERE id = ?",
                (canonical, content_hash(canonical, row["text"]), row["id"]),
            )

    rows = conn.execute(
        "SELECT id, canonical_url, text FROM captures"
        " WHERE content_hash IS NULL OR content_hash = ''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE captures SET content_hash = ? WHERE id = ?",
            (content_hash(row["canonical_url"], row["text"]), row["id"]),
        )
    conn.execute(
        "DELETE FROM captures WHERE id NOT IN"
        " (SELECT MIN(id) FROM captures GROUP BY content_hash)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_captures_hash")

    # v1.2: stable IRIs on canonical entities and claims.
    for table, make_iri in (("entities", ontology.entity_iri), ("claims", ontology.claim_iri)):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "iri" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN iri TEXT NOT NULL DEFAULT ''"
            )
        for row in conn.execute(f"SELECT id FROM {table} WHERE iri = ''").fetchall():
            conn.execute(
                f"UPDATE {table} SET iri = ? WHERE id = ?", (make_iri(), row["id"])
            )

    # v1.3: evidence lifecycle. Captures processed under the old auto-extract
    # regime count as promoted (they are already in the graph); untouched ones
    # go to staged and wait for the user's gesture.
    if "evidence_status" not in columns:
        conn.execute(
            "ALTER TABLE captures ADD COLUMN"
            " evidence_status TEXT NOT NULL DEFAULT 'staged'"
        )
        conn.execute(
            "UPDATE captures SET evidence_status = 'promoted'"
            " WHERE extraction_status != 'pending'"
        )
    if "project_id" not in columns:
        conn.execute(
            "ALTER TABLE captures ADD COLUMN"
            " project_id INTEGER REFERENCES projects(id)"
        )
    # v1.3: pipeline status rename — 'promoted' now names the user's evidence
    # gesture; SQLite -> Neo4j is 'loaded'.
    conn.execute(
        "UPDATE captures SET extraction_status = 'loaded'"
        " WHERE extraction_status = 'promoted'"
    )
    conn.execute(
        "UPDATE captures SET extraction_status = 'failed_load'"
        " WHERE extraction_status = 'failed_promotion'"
    )


def insert_capture(
    url: str, title: str, text: str, captured_at: str, raw_html: str = ""
) -> tuple[int, bool]:
    """Store one capture and return (capture_id, was_new)."""
    canonical = urlnorm.canonicalize(url)
    hash_ = content_hash(canonical, text)
    with get_connection() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO captures"
                " (url, canonical_url, title, text, raw_html, content_hash, captured_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (url, canonical, title, text, raw_html, hash_, captured_at),
            )
            return cur.lastrowid, True
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id, raw_html FROM captures WHERE content_hash = ?", (hash_,)
            ).fetchone()
            # Raw capture is sacred: backfill HTML onto a pre-v1.2 duplicate.
            if raw_html and not row["raw_html"]:
                conn.execute(
                    "UPDATE captures SET raw_html = ? WHERE id = ?",
                    (raw_html, row["id"]),
                )
            return row["id"], False


def get_capture(capture_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()


def captures_with_status(
    status: str, limit: int = 50, evidence_status: str | None = None
) -> list[sqlite3.Row]:
    query = "SELECT * FROM captures WHERE extraction_status = ?"
    params: list = [status]
    if evidence_status is not None:
        query += " AND evidence_status = ?"
        params.append(evidence_status)
    query += " ORDER BY id LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def set_evidence_status(
    capture_id: int, status: str, project_id: int | None = None
) -> None:
    with get_connection() as conn:
        if project_id is None:
            conn.execute(
                "UPDATE captures SET evidence_status = ? WHERE id = ?",
                (status, capture_id),
            )
        else:
            conn.execute(
                "UPDATE captures SET evidence_status = ?, project_id = ?"
                " WHERE id = ?",
                (status, project_id, capture_id),
            )


def latest_capture_by_canonical_url(canonical_url: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM captures WHERE canonical_url = ?"
            " ORDER BY id DESC LIMIT 1",
            (canonical_url,),
        ).fetchone()


def ensure_project(name: str) -> sqlite3.Row:
    """Return the project named `name`, creating it if needed."""
    name = name.strip()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO projects (iri, name) VALUES (?, ?)",
                (ontology.project_iri(), name),
            )
            row = conn.execute(
                "SELECT * FROM projects WHERE name = ?", (name,)
            ).fetchone()
        return row


def get_project(project_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()


def list_projects() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT p.id, p.iri, p.name, p.created_at, COUNT(c.id) AS captures"
            " FROM projects p LEFT JOIN captures c ON c.project_id = p.id"
            " GROUP BY p.id ORDER BY p.name"
        ).fetchall()
    return [dict(r) for r in rows]


def replace_chunks(capture_id: int, chunk_rows: list[dict]) -> int:
    with get_connection() as conn:
        conn.execute("DELETE FROM chunks WHERE capture_id = ?", (capture_id,))
        conn.executemany(
            "INSERT INTO chunks (capture_id, seq, text, embedding)"
            " VALUES (?, ?, ?, ?)",
            [
                (capture_id, c["seq"], c["text"], json.dumps(c["embedding"]))
                for c in chunk_rows
            ],
        )
        return len(chunk_rows)


def captures_without_chunks(limit: int = 50) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM captures c WHERE NOT EXISTS"
            " (SELECT 1 FROM chunks ch WHERE ch.capture_id = c.id)"
            " AND c.evidence_status != 'excluded'"
            " ORDER BY c.id LIMIT ?",
            (limit,),
        ).fetchall()


def chunks_with_pages() -> list[sqlite3.Row]:
    """All chunks of non-excluded captures, joined with page metadata."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT ch.id, ch.capture_id, ch.seq, ch.text, ch.embedding,"
            " c.url, c.canonical_url, c.title, c.captured_at, c.project_id"
            " FROM chunks ch JOIN captures c ON c.id = ch.capture_id"
            " WHERE c.evidence_status != 'excluded'"
        ).fetchall()


def log_tension_review(iri_a: str, iri_b: str, decision: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tension_reviews (iri_a, iri_b, decision) VALUES (?, ?, ?)",
            (iri_a, iri_b, decision),
        )


def tension_review_stats() -> dict:
    """Confirm/dismiss counts; dismissals are the precision training signal."""
    with get_connection() as conn:
        counts = {
            row["decision"]: row["n"]
            for row in conn.execute(
                "SELECT decision, COUNT(*) AS n FROM tension_reviews"
                " GROUP BY decision"
            )
        }
    confirmed = counts.get("confirmed", 0)
    dismissed = counts.get("dismissed", 0)
    reviewed = confirmed + dismissed
    return {
        "confirmed": confirmed,
        "dismissed": dismissed,
        "proposal_precision": round(confirmed / reviewed, 3) if reviewed else None,
    }


def set_extraction_status(capture_id: int, status: str, error: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE captures SET extraction_status = ?, extraction_error = ?"
            " WHERE id = ?",
            (status, error, capture_id),
        )


def capture_stats() -> dict:
    """Return counts and the latest capture for GET /status."""
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        by_status = {
            row["extraction_status"]: row["n"]
            for row in conn.execute(
                "SELECT extraction_status, COUNT(*) AS n FROM captures"
                " GROUP BY extraction_status"
            )
        }
        by_evidence = {
            row["evidence_status"]: row["n"]
            for row in conn.execute(
                "SELECT evidence_status, COUNT(*) AS n FROM captures"
                " GROUP BY evidence_status"
            )
        }
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        latest = conn.execute(
            "SELECT url, captured_at FROM captures ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "captures": count,
        "by_status": by_status,
        "by_evidence": by_evidence,
        "canonical_entities": entities,
        "claims": claims,
        "latest": dict(latest) if latest else None,
    }


def replace_staged_extraction(
    capture_id: int,
    entities: list[dict],
    relationships: list[dict],
    claims: list[dict] | None = None,
) -> None:
    """Replace the staged extraction for one capture."""
    with get_connection() as conn:
        conn.execute("DELETE FROM staged_entities WHERE capture_id = ?", (capture_id,))
        conn.execute(
            "DELETE FROM staged_relationships WHERE capture_id = ?", (capture_id,)
        )
        conn.execute("DELETE FROM staged_claims WHERE capture_id = ?", (capture_id,))
        conn.executemany(
            "INSERT INTO staged_entities"
            " (capture_id, name, entity_type, aliases, description, salience)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    capture_id,
                    e["name"],
                    e["type"],
                    json.dumps(e.get("aliases", [])),
                    e.get("description", ""),
                    e.get("salience", "passing"),
                )
                for e in entities
            ],
        )
        conn.executemany(
            "INSERT INTO staged_claims"
            " (capture_id, statement, stance, evidence, entities, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    capture_id,
                    c["statement"],
                    c["stance"],
                    c["evidence"],
                    json.dumps(c.get("entities", [])),
                    c.get("confidence", 1.0),
                )
                for c in (claims or [])
            ],
        )
        conn.executemany(
            "INSERT INTO staged_relationships"
            " (capture_id, source_name, rel_type, target_name, predicate)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (
                    capture_id,
                    r["source"],
                    r["type"],
                    r["target"],
                    r.get("predicate"),
                )
                for r in relationships
            ],
        )


def staged_for_capture(capture_id: int) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    with get_connection() as conn:
        ents = conn.execute(
            "SELECT * FROM staged_entities WHERE capture_id = ? ORDER BY id",
            (capture_id,),
        ).fetchall()
        rels = conn.execute(
            "SELECT * FROM staged_relationships WHERE capture_id = ? ORDER BY id",
            (capture_id,),
        ).fetchall()
    return ents, rels


def staged_claims_for_capture(capture_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM staged_claims WHERE capture_id = ? ORDER BY id",
            (capture_id,),
        ).fetchall()


def _norm_statement(statement: str) -> str:
    return " ".join(statement.casefold().split())


def replace_claims_for_capture(capture_id: int, claims: list[dict]) -> list[dict]:
    """Replace promoted claims for one capture.

    Claims whose statement survives re-extraction keep their IRI, so graph
    edges hanging off the Claim node (BRIDGE, CONTRADICTS) survive
    re-promotion. Returns [{"id": ..., "iri": ...}, ...] in input order.
    """
    with get_connection() as conn:
        old = conn.execute(
            "SELECT statement, iri FROM claims WHERE capture_id = ?", (capture_id,)
        ).fetchall()
        old_iris = {
            _norm_statement(row["statement"]): row["iri"] for row in old if row["iri"]
        }
        conn.execute("DELETE FROM claims WHERE capture_id = ?", (capture_id,))
        out = []
        for c in claims:
            iri = old_iris.pop(_norm_statement(c["statement"]), None) or ontology.claim_iri()
            cur = conn.execute(
                "INSERT INTO claims"
                " (iri, capture_id, page_url, statement, stance, evidence,"
                "  captured_at, embedding, confidence, entity_ids)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iri,
                    capture_id,
                    c["page_url"],
                    c["statement"],
                    c["stance"],
                    c["evidence"],
                    c["captured_at"],
                    json.dumps(c["embedding"]),
                    c.get("confidence", 1.0),
                    json.dumps(c.get("entity_ids", [])),
                ),
            )
            out.append({"id": cur.lastrowid, "iri": iri})
        return out


def replace_candidate_types(capture_id: int, candidates: list[dict]) -> None:
    """Replace the unknown-type entity proposals staged for one capture."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM candidate_types WHERE capture_id = ?", (capture_id,)
        )
        conn.executemany(
            "INSERT INTO candidate_types (capture_id, name, proposed_type, description)"
            " VALUES (?, ?, ?, ?)",
            [
                (capture_id, c["name"], c["proposed_type"], c.get("description", ""))
                for c in candidates
            ],
        )


def candidate_type_summary(limit: int = 50) -> list[dict]:
    """Aggregate candidate-type proposals for the weekly governance review."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT proposed_type, COUNT(*) AS n,"
            " GROUP_CONCAT(DISTINCT name) AS names"
            " FROM candidate_types GROUP BY proposed_type"
            " ORDER BY n DESC, proposed_type LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def all_claims() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM claims ORDER BY id").fetchall()


def job_last_run(job_name: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_run FROM job_runs WHERE job_name = ?", (job_name,)
        ).fetchone()
        return row["last_run"] if row else None


def record_job_run(job_name: str, detail: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO job_runs (job_name, last_run, detail)"
            " VALUES (?, datetime('now'), ?)"
            " ON CONFLICT(job_name) DO UPDATE SET"
            "  last_run = datetime('now'), detail = excluded.detail",
            (job_name, detail),
        )


def review_rows(limit: int = 100) -> list[dict]:
    """Return review data for captures, staged rows, and merge decisions."""
    with get_connection() as conn:
        captures = conn.execute(
            "SELECT id, url, title, length(text) AS text_len, captured_at,"
            " extraction_status, extraction_error, evidence_status, project_id"
            " FROM captures ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for cap in captures:
            ents, rels = staged_for_capture(cap["id"])
            claims = staged_claims_for_capture(cap["id"])
            merges = conn.execute(
                "SELECT * FROM merge_log WHERE capture_id = ? ORDER BY id",
                (cap["id"],),
            ).fetchall()
            candidates = conn.execute(
                "SELECT * FROM candidate_types WHERE capture_id = ? ORDER BY id",
                (cap["id"],),
            ).fetchall()
            result.append(
                {
                    "capture": dict(cap),
                    "entities": [dict(e) for e in ents],
                    "relationships": [dict(r) for r in rels],
                    "claims": [dict(c) for c in claims],
                    "merges": [dict(m) for m in merges],
                    "candidates": [dict(c) for c in candidates],
                }
            )
    return result


def entities_of_type(entity_type: str) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM entities WHERE entity_type = ?", (entity_type,)
        ).fetchall()


def get_entity(entity_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()


def insert_entity(
    canonical_name: str,
    entity_type: str,
    aliases: list[str],
    description: str,
    embedding: list[float],
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO entities"
            " (iri, canonical_name, entity_type, aliases, description, embedding)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                ontology.entity_iri(),
                canonical_name,
                entity_type,
                json.dumps(aliases),
                description,
                json.dumps(embedding),
            ),
        )
        return cur.lastrowid


def add_entity_alias(entity_id: int, alias: str) -> None:
    """Add a new alias to an existing entity when needed."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT aliases FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        aliases = json.loads(row["aliases"])
        if alias not in aliases:
            aliases.append(alias)
            conn.execute(
                "UPDATE entities SET aliases = ? WHERE id = ?",
                (json.dumps(aliases), entity_id),
            )


def log_merge(
    capture_id: int,
    staged_name: str,
    entity_type: str,
    decision: str,
    matched_entity_id: int | None,
    score: float | None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO merge_log"
            " (capture_id, staged_name, entity_type, decision, matched_entity_id, score)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (capture_id, staged_name, entity_type, decision, matched_entity_id, score),
        )
