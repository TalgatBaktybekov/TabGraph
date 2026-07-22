"""TabGraph API."""

import asyncio
import html
import json

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import chunks, db, discovery, extraction, graph, llm, qa, urlnorm

MIN_TEXT_LENGTH = 500
SCHEDULER_POLL_SECONDS = 6 * 3600  # jobs carry their own weekly/monthly intervals


async def _scheduler_loop():
    """Run due discovery jobs (weekly bridges/tensions, monthly summaries)."""
    while True:
        try:
            results = await asyncio.to_thread(discovery.run_due_jobs)
            for result in results:
                print(f"[TabGraph] discovery: {result}")
        except Exception as exc:
            print(f"[TabGraph] discovery scheduler error: {exc}")
        await asyncio.sleep(SCHEDULER_POLL_SECONDS)


async def _chunk_backfill():
    """Chunk any captures that predate the chunk layer."""
    try:
        results = await asyncio.to_thread(chunks.chunk_pending, 500)
        if results:
            print(f"[TabGraph] chunk backfill: {len(results)} captures")
    except Exception as exc:
        print(f"[TabGraph] chunk backfill error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    llm.load_env()
    db.init_db()
    try:
        graph.ensure_schema()
    except Exception as exc:
        print(f"[TabGraph] WARNING: Neo4j unavailable at startup: {exc}")
    scheduler = asyncio.create_task(_scheduler_loop())
    backfill = asyncio.create_task(_chunk_backfill())
    yield
    scheduler.cancel()
    backfill.cancel()


app = FastAPI(title="TabGraph", lifespan=lifespan)


class Capture(BaseModel):
    url: str
    title: str = ""
    text: str = ""
    html: str = ""  # raw page HTML; stored verbatim, never derived from
    timestamp: str


class Question(BaseModel):
    question: str


class PromoteBody(BaseModel):
    project: str | None = None


class PromoteUrl(BaseModel):
    url: str
    project: str | None = None


class TensionReview(BaseModel):
    iri_a: str
    iri_b: str
    decision: str  # confirmed | dismissed


class ProjectBody(BaseModel):
    name: str


def stage_capture(capture_id: int) -> None:
    """Cheap treatment for staged captures: chunks + embeddings only."""
    result = chunks.chunk_capture(capture_id)
    print(f"[TabGraph] chunking: {result}")


def run_pipeline(capture_id: int) -> None:
    """Full extraction + graph load for one promoted capture."""
    result = extraction.extract_capture(capture_id)
    print(f"[TabGraph] extraction: {result}")
    if result.get("status") == "extracted":
        result = graph.load_capture(capture_id)
        print(f"[TabGraph] load: {result}")


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
        raw_html=capture.html,
    )
    print(f"[TabGraph] Capture ingested: {capture_id}")
    if was_new:
        background.add_task(stage_capture, capture_id)
    return {"id": capture_id, "status": "stored" if was_new else "duplicate"}


def _promote(capture_id: int, project_name: str | None, background: BackgroundTasks):
    capture = db.get_capture(capture_id)
    if capture is None:
        return {"status": "not_found", "capture_id": capture_id}
    project_id = None
    project = (project_name or "").strip()
    if project:
        project_id = db.ensure_project(project)["id"]
    db.set_evidence_status(capture_id, "promoted", project_id)
    background.add_task(run_pipeline, capture_id)
    return {
        "status": "promoted",
        "capture_id": capture_id,
        "project": project or None,
    }


@app.post("/captures/{capture_id}/promote")
def promote_capture(capture_id: int, body: PromoteBody, background: BackgroundTasks):
    """The evidence gesture: full extraction + graph load, project assignment."""
    return _promote(capture_id, body.project, background)


@app.post("/captures/{capture_id}/exclude")
def exclude_capture(capture_id: int):
    """Remove a capture from recall and all future processing.

    Does not yet remove already-loaded graph data (deletion cascade is a
    v0.2 work item).
    """
    if db.get_capture(capture_id) is None:
        return {"status": "not_found", "capture_id": capture_id}
    db.set_evidence_status(capture_id, "excluded")
    return {"status": "excluded", "capture_id": capture_id}


@app.post("/promote_url")
def promote_url(body: PromoteUrl, background: BackgroundTasks):
    """Promote the latest capture of a URL (the extension's gesture)."""
    capture = db.latest_capture_by_canonical_url(urlnorm.canonicalize(body.url))
    if capture is None:
        return {"status": "not_captured", "url": body.url}
    return _promote(capture["id"], body.project, background)


@app.get("/projects")
def projects():
    return {"projects": db.list_projects()}


@app.post("/projects")
def create_project(body: ProjectBody):
    row = db.ensure_project(body.name)
    return {"id": row["id"], "name": row["name"], "iri": row["iri"]}


@app.post("/extract")
def extract(limit: int = 20):
    return {"results": extraction.extract_pending(limit)}


@app.post("/load")
def load(limit: int = 20):
    return {"results": graph.load_extracted(limit)}


@app.post("/chunk")
def chunk(limit: int = 50):
    return {"results": chunks.chunk_pending(limit)}


@app.post("/recall")
def recall(question: Question):
    result = chunks.recall(question.question)
    return result or {
        "path": "recall",
        "answer": "Nothing captured yet matches that.",
        "chunks": [],
    }


@app.post("/ask")
def ask(question: Question):
    return qa.ask(question.question)


@app.post("/discover/bridges")
def run_bridges():
    return discovery.discover_bridges()


@app.post("/discover/tensions")
def run_tensions():
    return discovery.discover_tensions()


@app.post("/discover/communities")
def run_communities():
    return discovery.summarize_communities()


@app.get("/discoveries")
def discoveries(limit: int = 10):
    return discovery.recent_discoveries(limit)


@app.post("/tensions/review")
def tensions_review(body: TensionReview):
    return discovery.review_tension(body.iri_a, body.iri_b, body.decision)


@app.get("/status")
def status():
    return {"ok": True, **db.capture_stats()}


STATUS_COLORS = {
    "pending": "#888",
    "extracted": "#c80",
    "loaded": "#2a7",
    "failed": "#c33",
    "failed_load": "#c33",
}

EVIDENCE_COLORS = {"staged": "#888", "promoted": "#46c", "excluded": "#c33"}


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
        + f" — {_esc(stats['canonical_entities'])} canonical entities"
        + f" — {_esc(stats['claims'])} claims</p>",
    ]
    candidates = db.candidate_type_summary()
    if candidates:
        parts.append(
            "<h3>Candidate entity types (governance queue)</h3>"
            "<table><tr><th>proposed type</th><th>count</th><th>entities</th></tr>"
        )
        for c in candidates:
            parts.append(
                f"<tr><td>{_esc(c['proposed_type'])}</td><td>{_esc(c['n'])}</td>"
                f"<td>{_esc(c['names'])}</td></tr>"
            )
        parts.append("</table>")
    for item in db.review_rows(limit):
        cap = item["capture"]
        color = STATUS_COLORS.get(cap["extraction_status"], "#888")
        ev_color = EVIDENCE_COLORS.get(cap["evidence_status"], "#888")
        parts.append(
            f"<details><summary><span class='badge' style='background:{ev_color}'>"
            f"{_esc(cap['evidence_status'])}</span> "
            f"<span class='badge' style='background:{color}'>"
            f"{_esc(cap['extraction_status'])}</span> "
            f"<strong>#{cap['id']}</strong> {_esc(cap['title'])[:80]} "
            f"<small>({_esc(cap['url'])[:90]}, {_esc(cap['text_len'])} chars)</small></summary>"
        )
        if cap["extraction_error"]:
            parts.append(f"<p class='err'>{_esc(cap['extraction_error'])}</p>")
        if item["entities"]:
            parts.append("<h4>Entities</h4><table><tr><th>name</th><th>type</th>"
                         "<th>salience</th><th>aliases</th><th>description</th></tr>")
            for e in item["entities"]:
                aliases = ", ".join(json.loads(e["aliases"]))
                parts.append(
                    f"<tr><td>{_esc(e['name'])}</td><td>{_esc(e['entity_type'])}</td>"
                    f"<td>{_esc(e['salience'])}</td>"
                    f"<td>{_esc(aliases)}</td><td>{_esc(e['description'])}</td></tr>"
                )
            parts.append("</table>")
        if item["claims"]:
            parts.append("<h4>Claims</h4><table><tr><th>statement</th>"
                         "<th>stance</th><th>evidence</th><th>entities</th></tr>")
            for c in item["claims"]:
                names = ", ".join(json.loads(c["entities"]))
                parts.append(
                    f"<tr><td>{_esc(c['statement'])}</td><td>{_esc(c['stance'])}</td>"
                    f"<td>{_esc(c['evidence'])}</td><td>{_esc(names)}</td></tr>"
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
