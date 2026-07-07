"""SQLite storage for captures, staged extractions, entities, and merge logs."""

import hashlib
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tabgraph.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    text              TEXT NOT NULL DEFAULT '',
    content_hash      TEXT NOT NULL,
    captured_at       TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    extraction_error  TEXT
);

CREATE TABLE IF NOT EXISTS staged_entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    name        TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    aliases     TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS entities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    aliases        TEXT NOT NULL DEFAULT '[]',
    description    TEXT NOT NULL DEFAULT '',
    embedding      TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

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

    rows = conn.execute(
        "SELECT id, url, text FROM captures"
        " WHERE content_hash IS NULL OR content_hash = ''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE captures SET content_hash = ? WHERE id = ?",
            (content_hash(row["url"], row["text"]), row["id"]),
        )
    conn.execute(
        "DELETE FROM captures WHERE id NOT IN"
        " (SELECT MIN(id) FROM captures GROUP BY content_hash)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_captures_hash")


def insert_capture(url: str, title: str, text: str, captured_at: str) -> tuple[int, bool]:
    """Store one capture and return (capture_id, was_new)."""
    hash_ = content_hash(url, text)
    with get_connection() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO captures (url, title, text, content_hash, captured_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (url, title, text, hash_, captured_at),
            )
            return cur.lastrowid, True
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM captures WHERE content_hash = ?", (hash_,)
            ).fetchone()
            return row["id"], False


def get_capture(capture_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()


def captures_with_status(status: str, limit: int = 50) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM captures WHERE extraction_status = ?"
            " ORDER BY id LIMIT ?",
            (status, limit),
        ).fetchall()


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
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        latest = conn.execute(
            "SELECT url, captured_at FROM captures ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "captures": count,
        "by_status": by_status,
        "canonical_entities": entities,
        "latest": dict(latest) if latest else None,
    }


def replace_staged_extraction(
    capture_id: int, entities: list[dict], relationships: list[dict]
) -> None:
    """Replace the staged extraction for one capture."""
    with get_connection() as conn:
        conn.execute("DELETE FROM staged_entities WHERE capture_id = ?", (capture_id,))
        conn.execute(
            "DELETE FROM staged_relationships WHERE capture_id = ?", (capture_id,)
        )
        conn.executemany(
            "INSERT INTO staged_entities"
            " (capture_id, name, entity_type, aliases, description)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (
                    capture_id,
                    e["name"],
                    e["type"],
                    json.dumps(e.get("aliases", [])),
                    e.get("description", ""),
                )
                for e in entities
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


def review_rows(limit: int = 100) -> list[dict]:
    """Return review data for captures, staged rows, and merge decisions."""
    with get_connection() as conn:
        captures = conn.execute(
            "SELECT id, url, title, length(text) AS text_len, captured_at,"
            " extraction_status, extraction_error FROM captures"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for cap in captures:
            ents, rels = staged_for_capture(cap["id"])
            merges = conn.execute(
                "SELECT * FROM merge_log WHERE capture_id = ? ORDER BY id",
                (cap["id"],),
            ).fetchall()
            result.append(
                {
                    "capture": dict(cap),
                    "entities": [dict(e) for e in ents],
                    "relationships": [dict(r) for r in rels],
                    "merges": [dict(m) for m in merges],
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
            " (canonical_name, entity_type, aliases, description, embedding)"
            " VALUES (?, ?, ?, ?, ?)",
            (
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
