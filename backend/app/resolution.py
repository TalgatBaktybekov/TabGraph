"""Resolve staged entity mentions to canonical entity ids."""

import json
import os

from . import db

_model = None


def similarity_threshold() -> float:
    return 0.85


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed(text: str) -> list[float]:
    """Embed one string with normalized vectors."""
    vector = _get_model().encode(text, normalize_embeddings=True)
    return vector.tolist()


def embedding_text(name: str, entity_type: str, description: str) -> str:
    return f"{name} ({entity_type}): {description}".strip()


def normalize(name: str) -> str:
    return " ".join(name.casefold().split())


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def resolve(capture_id: int, staged: dict) -> int:
    """Resolve one staged mention and return the canonical entity id."""
    name = staged["name"]
    entity_type = staged["type"]
    mention_names = [name, *staged.get("aliases", [])]
    candidates = db.entities_of_type(entity_type)

    mention_norms = {normalize(n) for n in mention_names}
    for cand in candidates:
        cand_names = [cand["canonical_name"], *json.loads(cand["aliases"])]
        if mention_norms & {normalize(n) for n in cand_names}:
            _record_aliases(cand, mention_names)
            db.log_merge(capture_id, name, entity_type, "exact", cand["id"], None)
            return cand["id"]

    mention_vec = embed(embedding_text(name, entity_type, staged.get("description", "")))
    best_id, best_row, best_score = None, None, 0.0
    for cand in candidates:
        score = _dot(mention_vec, json.loads(cand["embedding"]))
        if score > best_score:
            best_id, best_row, best_score = cand["id"], cand, score
    if best_id is not None and best_score >= similarity_threshold():
        _record_aliases(best_row, mention_names)
        db.log_merge(capture_id, name, entity_type, "embedding", best_id, best_score)
        return best_id

    entity_id = db.insert_entity(
        canonical_name=name,
        entity_type=entity_type,
        aliases=staged.get("aliases", []),
        description=staged.get("description", ""),
        embedding=mention_vec,
    )
    db.log_merge(
        capture_id,
        name,
        entity_type,
        "new",
        entity_id,
        best_score if best_id is not None else None,
    )
    return entity_id


def _record_aliases(entity_row, mention_names: list[str]) -> None:
    """Add new aliases from the mention to the matched entity."""
    known = {
        normalize(n)
        for n in [entity_row["canonical_name"], *json.loads(entity_row["aliases"])]
    }
    for n in mention_names:
        if normalize(n) not in known:
            db.add_entity_alias(entity_row["id"], n)
