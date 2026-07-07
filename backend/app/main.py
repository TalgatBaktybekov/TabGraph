"""TabGraph API."""

import html
import json

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import db, extraction, graph, llm, qa

MIN_TEXT_LENGTH = 500


@asynccontextmanager
async def lifespan(app: FastAPI):
    llm.load_env()
    db.init_db()
    try:
        graph.ensure_schema()
    except Exception as exc:
        print(f"[TabGraph] WARNING: Neo4j unavailable at startup: {exc}")
    yield


app = FastAPI(title="TabGraph", lifespan=lifespan)


class Capture(BaseModel):
    url: str
    title: str = ""
    text: str = ""
    timestamp: str


class Question(BaseModel):
    question: str


def process_capture(capture_id: int) -> None:
    """Run extraction and promotion for one capture."""
    result = extraction.extract_capture(capture_id)
    print(f"[TabGraph] extraction: {result}")
    if result.get("status") == "extracted":
        result = graph.promote_capture(capture_id)
        print(f"[TabGraph] promotion: {result}")


@app.post("/ingest")
def ingest(capture: Capture, background: BackgroundTasks):
    if len(capture.text) < MIN_TEXT_LENGTH:
        return {
            "status": "skipped_short",
            "detail": f"text has {len(capture.text)} chars, minimum is {MIN_TEXT_LENGTH}",
        }
    print(f"[TabGraph] Ingesting capture: {capture.url}")
    capture_id, was_new = db.insert_capture(
        url=capture.url,
        title=capture.title,
        text=capture.text,
        captured_at=capture.timestamp,
    )
    print(f"[TabGraph] Capture ingested: {capture_id}")
    if was_new:
        background.add_task(process_capture, capture_id)
    return {"id": capture_id, "status": "stored" if was_new else "duplicate"}


@app.post("/extract")
def extract(limit: int = 20):
    return {"results": extraction.extract_pending(limit)}


@app.post("/promote")
def promote(limit: int = 20):
    return {"results": graph.promote_extracted(limit)}


@app.post("/ask")
def ask(question: Question):
    return qa.ask(question.question)


@app.get("/status")
def status():
    return {"ok": True, **db.capture_stats()}


STATUS_COLORS = {
    "pending": "#888",
    "extracted": "#c80",
    "promoted": "#2a7",
    "failed": "#c33",
    "failed_promotion": "#c33",
}


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


@app.get("/review", response_class=HTMLResponse)
def review(limit: int = 100):
    stats = db.capture_stats()
    parts = [
        "<!doctype html><meta charset='utf-8'><title>TabGraph review</title>",
        "<style>body{font-family:system-ui;margin:2rem auto;max-width:60rem}"
        "details{border:1px solid #ddd;border-radius:6px;margin:.5rem 0;padding:.5rem 1rem}"
        "summary{cursor:pointer} .badge{padding:1px 8px;border-radius:9px;color:#fff;font-size:.8rem}"
        "table{border-collapse:collapse;margin:.5rem 0}td,th{border:1px solid #ddd;"
        "padding:2px 8px;font-size:.85rem;text-align:left}.err{color:#c33}</style>",
        f"<h1>TabGraph review</h1><p>{_esc(stats['captures'])} captures — "
        + ", ".join(f"{_esc(k)}: {_esc(v)}" for k, v in stats["by_status"].items())
        + f" — {_esc(stats['canonical_entities'])} canonical entities</p>",
    ]
    for item in db.review_rows(limit):
        cap = item["capture"]
        color = STATUS_COLORS.get(cap["extraction_status"], "#888")
        parts.append(
            f"<details><summary><span class='badge' style='background:{color}'>"
            f"{_esc(cap['extraction_status'])}</span> "
            f"<strong>#{cap['id']}</strong> {_esc(cap['title'])[:80]} "
            f"<small>({_esc(cap['url'])[:90]}, {_esc(cap['text_len'])} chars)</small></summary>"
        )
        if cap["extraction_error"]:
            parts.append(f"<p class='err'>{_esc(cap['extraction_error'])}</p>")
        if item["entities"]:
            parts.append("<h4>Entities</h4><table><tr><th>name</th><th>type</th>"
                         "<th>aliases</th><th>description</th></tr>")
            for e in item["entities"]:
                aliases = ", ".join(json.loads(e["aliases"]))
                parts.append(
                    f"<tr><td>{_esc(e['name'])}</td><td>{_esc(e['entity_type'])}</td>"
                    f"<td>{_esc(aliases)}</td><td>{_esc(e['description'])}</td></tr>"
                )
            parts.append("</table>")
        if item["relationships"]:
            parts.append("<h4>Relationships</h4><table><tr><th>source</th>"
                         "<th>type</th><th>target</th><th>predicate</th></tr>")
            for r in item["relationships"]:
                parts.append(
                    f"<tr><td>{_esc(r['source_name'])}</td><td>{_esc(r['rel_type'])}</td>"
                    f"<td>{_esc(r['target_name'])}</td><td>{_esc(r['predicate'])}</td></tr>"
                )
            parts.append("</table>")
        if item["merges"]:
            parts.append("<h4>Resolution decisions</h4><table><tr><th>mention</th>"
                         "<th>decision</th><th>entity id</th><th>score</th></tr>")
            for m in item["merges"]:
                score = f"{m['score']:.3f}" if m["score"] is not None else ""
                parts.append(
                    f"<tr><td>{_esc(m['staged_name'])}</td><td>{_esc(m['decision'])}</td>"
                    f"<td>{_esc(m['matched_entity_id'])}</td><td>{score}</td></tr>"
                )
            parts.append("</table>")
        if not (item["entities"] or item["relationships"]):
            parts.append("<p><em>Nothing staged.</em></p>")
        parts.append("</details>")
    return "".join(parts)
