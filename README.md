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

TabGraph turns browser tabs into a local knowledge graph. The extension first applies privacy checks and captures the main article text (vendored Readability.js) plus the raw HTML. Passive captures land as **staged** evidence: stored verbatim, deduplicated by canonical URL plus text, chunked and embedded for recall — no LLM touches them. The "Save as evidence" gesture in the popup (with an optional project) **promotes** a page: the backend then extracts entities and relationships into staged rows and resolves mentions to canonical entities with a two-pass strategy: exact normalized-name/alias matching first, then embedding similarity for near matches. If neither pass finds a safe merge, a new canonical entity is created and the decision is logged for review.

Staging stays separate from promotion on purpose, so extraction can be inspected and retried before anything is written into Neo4j. That graph layer is what makes cross-page queries practical: it preserves provenance, supports multi-hop relationships, and powers the QA path, which tries Cypher first and falls back to vector retrieval when needed.

## Schema governance

Node/predicate registry with public-vocabulary alignments: [tabkg-schema.md](tabkg-schema.md).
Every schema change is logged in [MIGRATIONS.md](MIGRATIONS.md) with the
competency question that motivated it; derived nodes carry `ontology_version`.
Raw capture (cleaned text + raw HTML) is stored verbatim — the knowledge layer
is always re-generatable via `POST /extract` + `POST /promote`.

## Ontology

- Shared schema for extraction, validation, and graph loading.
- Entity types: Person, Organization, Concept, Product, Event.
- Relation types: WORKS_AT, PART_OF, RELATED_TO, CREATED_BY.
- Graph shape: Page nodes, canonical entities with aliases/description/embedding, and fact edges with source-page provenance. MENTIONS edges carry a salience level (primary/secondary/passing).

### Claims & Discovery layer (v1.1)

Where knowledge actually lives: each page also yields 0–8 **Claim** nodes — substantive, self-contained statements the page asserts about a primary/secondary entity, with a verbatim evidence quote, a stance (asserts/disputes/questions), and an embedding. Claims are validated hard: the statement must name an extracted entity and the evidence must appear verbatim in the page text. Claims connect via `ABOUT` (→ entities, 1..n) and `ASSERTED_IN` (→ page, exactly one). Extraction stays a single combined LLM call per page, and claims are only attempted on pages with ≥ 800 chars and ≥ 1 primary entity.

Three scheduled discovery jobs run over promoted claims (a background loop in the API triggers them when due; `POST /discover/{bridges,tensions,communities}` runs them on demand):

- **discover_bridges** (weekly) — high-similarity claim pairs from different pages, scored up for time gap and cross-domain sources, written as `BRIDGE` edges and surfaced in the side panel as "connections you may have missed".
- **discover_tensions** (weekly) — claim pairs sharing an ABOUT entity get an LLM judge (≤ 200 pairs/run); disagreements become `CONTRADICTS` edges, surfaced as "your sources disagree".
- **summarize_communities** (monthly) — Leiden (or Louvain fallback) communities over the salience-weighted entity co-mention graph; each community ≥ 5 entities gets a 3–5 sentence `CommunitySummary` written from that community's claims only. Regenerated, never edited.

QA routes four ways and always reports which path answered: text-to-Cypher for precise lookups → **synthesis** for knowledge questions (top-15 claims by vector search + matching community summaries; every substantive sentence must cite a claim, rendered with page title/date/link; unused claims listed as "related things you read") → subgraph fallback → **chunk recall** over everything staged (vector RAG citing page URL + access date — works before any page is promoted). Proposed contradictions carry a machine-proposes/user-confirms review flow: confirm/dismiss buttons in the side panel, with proposal precision tracked in `GET /discoveries`.

## Eval

`backend/eval/labels.yaml` holds the gold set, including expected claims (statement + stance) for gold pages. `make eval` (from `backend/`) reports entity and claim precision/recall plus a vague-claim rate (target < 10%).
