"""Chunk-level evidence layer: recall over everything captured.

Staged captures get chunks + embeddings only (cheap, no LLM); this powers
"where did I read X" before — and independently of — the knowledge graph.
Chunks live in SQLite and are searched brute-force, which is fine at
personal-corpus scale (same approach as the discovery jobs).
"""

import json

from . import db, llm, resolution

CHUNK_WORDS = 200
CHUNK_OVERLAP = 40
RECALL_TOP_K = 6
# Below this cosine similarity a chunk isn't about the question at all.
MIN_RECALL_SCORE = 0.30

RECALL_SYSTEM = """You answer a question using ONLY the numbered excerpts from
pages the user has read. Be direct and concise. For every excerpt you draw on,
cite its number in square brackets, e.g. [2]. If the excerpts don't contain
the answer, say so plainly — never fill gaps with outside knowledge."""


def chunk_text(text: str) -> list[str]:
    """Split page text into overlapping word-window chunks."""
    words = text.split()
    if not words:
        return []
    step = CHUNK_WORDS - CHUNK_OVERLAP
    chunks = []
    for start in range(0, len(words), step):
        chunks.append(" ".join(words[start : start + CHUNK_WORDS]))
        if start + CHUNK_WORDS >= len(words):
            break
    return chunks


def chunk_capture(capture_id: int) -> dict:
    """(Re)build chunks + embeddings for one capture."""
    capture = db.get_capture(capture_id)
    if capture is None:
        return {"capture_id": capture_id, "status": "missing"}
    pieces = chunk_text(capture["text"])
    rows = [
        {"seq": i, "text": piece, "embedding": resolution.embed(piece)}
        for i, piece in enumerate(pieces)
    ]
    db.replace_chunks(capture_id, rows)
    return {"capture_id": capture_id, "status": "chunked", "chunks": len(rows)}


def chunk_pending(limit: int = 50) -> list[dict]:
    """Chunk captures that have no chunks yet (startup backfill + retries)."""
    return [chunk_capture(c["id"]) for c in db.captures_without_chunks(limit)]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def search(question: str, k: int = RECALL_TOP_K) -> list[dict]:
    """Top chunks for a question over all non-excluded captures."""
    question_vec = resolution.embed(question)
    scored = []
    for row in db.chunks_with_pages():
        score = _dot(question_vec, json.loads(row["embedding"]))
        if score < MIN_RECALL_SCORE:
            continue
        scored.append(
            {
                "score": score,
                "text": row["text"],
                "url": row["canonical_url"] or row["url"],
                "title": row["title"],
                "captured_at": row["captured_at"],
                "capture_id": row["capture_id"],
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:k]


def recall(question: str) -> dict | None:
    """Vector RAG over chunks; cites page URL + access date. None if no hits."""
    hits = search(question)
    if not hits:
        return None
    lines = [
        f"[{i}] {h['text']}\n    — \"{h['title']}\" ({h['url']}, read {h['captured_at']})"
        for i, h in enumerate(hits, 1)
    ]
    answer = llm.generate_text(
        llm.qa_model(),
        RECALL_SYSTEM,
        f"Question: {question}\n\nExcerpts:\n" + "\n".join(lines),
    ).strip()
    return {
        "path": "recall",
        "answer": answer,
        "chunks": [
            {k: v for k, v in h.items() if k != "text"}
            | {"snippet": h["text"][:200]}
            for h in hits
        ],
    }
