# TabGraph

TabGraph builds a personal knowledge graph from browser tabs. The Chrome extension captures readable page text, the FastAPI backend extracts entities and relationships, and Neo4j powers graph loading and question answering.

## Run

1. Start the backend from `backend/`.
2. Load the extension from `extension/` in Chrome.
3. Make sure `.env` includes `GOOGLE_API_KEY`, `NEO4J_PASSWORD`, or any model overrides you want.

### Backend

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
```

### Extension

Open Chrome's Extensions page, enable Developer mode, and load the `extension/` folder as an unpacked extension.

## What It Does

TabGraph turns browser tabs into a local knowledge graph. The extension first applies privacy checks and captures the main article text instead of the full page chrome (using vendored Readability.js). The backend stores each capture in SQLite, deduplicates it by URL plus text, extracts entities and relationships into staged rows, and resolves mentions to canonical entities with a two-pass strategy: exact normalized-name/alias matching first, then embedding similarity for near matches. If neither pass finds a safe merge, a new canonical entity is created and the decision is logged for review.

Staging stays separate from promotion on purpose, so extraction can be inspected and retried before anything is written into Neo4j. That graph layer is what makes cross-page queries practical: it preserves provenance, supports multi-hop relationships, and powers the QA path, which tries Cypher first and falls back to vector retrieval when needed.
