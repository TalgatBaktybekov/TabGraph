# Ontology migrations

Every derived node carries an `ontology_version` property. Each schema change
gets an entry here recording the rationale and the competency question (CQ)
that motivated it. **No CQ, no change.**

Re-extraction is the universal migration tool: raw captures (cleaned text +
raw HTML) are stored verbatim in SQLite, so every knowledge-layer structure
can be regenerated with `POST /extract` + `POST /promote`.

---

## v1.0 — initial schema (retroactive entry)

Page / Entity (Person, Organization, Concept, Product, Event) / MENTIONS with
salience / fact edges (WORKS_AT, PART_OF, RELATED_TO+predicate, CREATED_BY)
with `source_page_url` provenance. SQLite staging separated from Neo4j
promotion so extraction can be inspected and retried.

- CQs: "what do I know about entity X", "which pages mention X".

## v1.1 — claims & discovery layer (retroactive entry)

Claim nodes (statement, stance, verbatim evidence, embedding) with ABOUT and
ASSERTED_IN edges; BRIDGE / CONTRADICTS edges and CommunitySummary nodes
written by scheduled discovery jobs; three-way QA routing with citations.

- CQs: "what did that page say about X, exactly", "summarize everything I've
  read about X with citations", "where do my sources disagree".

## v1.3 — evidence lifecycle: staged/promoted split (current)

Motivated by plan v2 (evidence engine repositioning): passive capture must be
cheap and consent-preserving; full extraction is an explicit user gesture.

1. **`evidence_status` on captures** (`staged | promoted | excluded`,
   default `staged`). Ingest now only chunks + embeds (no LLM). Extraction
   and graph load run only for promoted captures, triggered by
   `POST /captures/{id}/promote` (with optional project) or the extension's
   "Save as evidence" button (`POST /promote_url`). `excluded` removes a
   capture from recall and future processing (graph cleanup of
   already-loaded data waits for the deletion cascade).
   - Backfill: captures processed under the old auto-extract regime →
     `promoted`; untouched `pending` ones → `staged`.
2. **Pipeline status rename.** "Promoted" now names the user's evidence
   gesture, so the SQLite→Neo4j step is renamed: `extraction_status`
   `promoted`→`loaded`, `failed_promotion`→`failed_load`; endpoint
   `POST /promote`→`POST /load`; `graph.promote_*`→`graph.load_*`.
3. **Chunk layer** (`chunks` table; `app/chunks.py`). Word-window chunks
   (~200 words, 40 overlap) with local embeddings for every non-excluded
   capture; brute-force vector search. New `POST /recall` endpoint and a
   recall fallback in `/ask` when the graph has nothing.
   - CQs: "where did I read X", "what was I reading last week" — the
     recall hook now works before (and without) the knowledge graph.
4. **Projects first-class** (`projects` table; `Project` node, `IN_PROJECT`
   edge from Page). Assigned at the promotion gesture; free-text creates.
   - CQ: "what was I reading for project P".
5. **`review_status` on CONTRADICTS** (`proposed | confirmed | dismissed`).
   Jobs write `proposed` (never overwriting a user verdict on re-run);
   `POST /tensions/review` records confirm/dismiss, logged in
   `tension_reviews` for the proposal-precision metric (exposed in
   `GET /discoveries`). Dismissed pairs disappear from the panel.
   - CQ: "which sources contradict each other" — with the machine-proposes/
     user-confirms contract from plan v2 delta 4.

Deliberately skipped: plan delta 5 (evidence/inference typed answer
segments). The synthesis path keeps its hard claims-only rule; model
inference stays out until it earns its way in.

## v1.2 — evidence-layer hardening

Motivated by the v0.1→v1.0 plan's standing rules; no new CQs, this pass
protects the ability to answer future ones.

1. **Raw HTML stored verbatim** (`captures.raw_html`). Cleaned text alone is
   lossy and cannot be backfilled later. The extension now ships
   `document.documentElement.outerHTML` with every capture; duplicates of
   pre-v1.2 captures backfill the column.
2. **URL canonicalization** (`captures.canonical_url`, `app/urlnorm.py`).
   Tracking params (`utm_*`, `gclid`, `fbclid`, ...), fragments, default
   ports stripped; host lowercased. Dedup hashes and Page node keys use the
   canonical URL; the original lands on `Page.original_url`.
   - CQ protected: "which sites do I keep coming back to" (one page must be
     one node regardless of the link that led there).
3. **Stable IRIs** (`tabkg:entity/{uuid}`, `tabkg:claim/{uuid}`,
   `tabkg:page/{hash16}`). Claim nodes are now MERGEd on `iri`, and
   re-promotion preserves the IRI of any claim whose statement survives
   re-extraction. This fixes a real defect: previously re-promotion deleted
   and re-minted all of a page's Claim nodes, silently destroying attached
   BRIDGE/CONTRADICTS edges.
4. **`ontology_version` stamped** on Page, Entity, Claim, and
   CommunitySummary nodes at write time.
5. **`candidate_type` escape** (`candidate_types` table). Entities whose
   proposed type is outside the enum are parked for the weekly governance
   review (visible at the top of `GET /review`) instead of being silently
   dropped.

### Migration notes (applied automatically at startup by `db._migrate`)

- Columns added: `captures.canonical_url`, `captures.raw_html`,
  `entities.iri`, `claims.iri` (IRIs backfilled with fresh UUIDs).
- All capture content hashes are recomputed on the canonical URL; captures
  that now collide are deduped keeping the lowest id (same behavior as the
  original hash migration).
- Neo4j side effects on first re-promotion of a page:
  - Claim nodes that predate IRIs are deleted and recreated once (their
    discovery edges are lost this one time; stable thereafter).
  - Pages whose canonical URL differs from the raw URL get a new Page node
    under the canonical key. Stale raw-URL Page nodes can be cleaned with:
    `MATCH (p:Page) WHERE p.original_url IS NULL AND NOT EXISTS { MATCH (p2:Page) WHERE p2.original_url = p.url } DETACH DELETE p`
    — or simply re-promote everything and delete Pages lacking `iri`:
    `MATCH (p:Page) WHERE p.iri IS NULL DETACH DELETE p`
