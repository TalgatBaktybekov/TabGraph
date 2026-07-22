# tabkg schema

Predicate registry for the TabGraph knowledge graph (property graph in Neo4j;
this doc plays the role of a `tabkg-schema.ttl` until RDF export lands).
Source of truth for the runtime enums is [`backend/app/ontology.py`](backend/app/ontology.py);
this doc adds domain→range and public-vocabulary alignments. Schema changes
are logged in [MIGRATIONS.md](MIGRATIONS.md).

Namespace: `tabkg:` — node IRIs are `tabkg:page/{hash16}`,
`tabkg:entity/{uuid}`, `tabkg:claim/{uuid}`.

Current `ontology_version`: **1.3**

## Node types

| Label | Alignment | Key properties |
|---|---|---|
| `Page` | ≈ `schema:WebPage`, `prov:Entity` | `url` (canonical, unique), `original_url`, `title`, `captured_at`, `content_hash`, `iri` |
| `Project` | ≈ `schema:Project` | `name` (unique), `iri`; created at the promotion gesture |
| `Entity` (base) | — | `entity_id` (unique), `iri`, `canonical_name`, `entity_type`, `aliases[]`, `description`, `embedding` |
| `Entity:Person` | ≈ `schema:Person` | real, named individual human |
| `Entity:Organization` | ≈ `schema:Organization` | company, institution, team |
| `Entity:Concept` | ≈ `skos:Concept` | named idea, method, field |
| `Entity:Product` | ≈ `schema:Product` | product, tool, library, system |
| `Entity:Event` | ≈ `schema:Event` | named occurrence at a point in time |
| `Claim` | ≈ `schema:Claim` | `iri` (unique), `claim_id`, `statement`, `stance` (asserts/disputes/questions), `evidence` (verbatim quote), `captured_at`, `embedding`, `confidence` |
| `CommunitySummary` | — | `community_id` (unique), `summary`, `generated_at`, `size`; regenerated, never edited |

## Predicates

| Predicate | Domain → Range | Alignment / notes |
|---|---|---|
| `MENTIONS` | Page → Entity | ≈ `schema:mentions`; carries `salience` (primary/secondary/passing) |
| `WORKS_AT` | Person → Organization | ≈ `schema:worksFor` |
| `PART_OF` | * → * | ≈ `schema:isPartOf` |
| `CREATED_BY` | Product → Organization\|Person | ≈ `schema:creator` (inverse direction) |
| `RELATED_TO` | * → * | escape hatch; **requires** free-text `predicate` property |
| `ABOUT` | Claim → Entity (1..n) | ≈ `schema:about` |
| `ASSERTED_IN` | Claim → Page (exactly 1) | ≈ `prov:wasQuotedFrom`; the claim invariant — no Claim without a source Page |
| `BRIDGE` | Claim → Claim | discovery job; `score`, `similarity`, `discovered_at` |
| `CONTRADICTS` | Claim → Claim | discovery job (LLM-judged); `similarity`, `discovered_at`, `review_status` (proposed/confirmed/dismissed — machine proposes, user confirms; only confirmed belongs in reports by default) |
| `MEMBER_OF` | Entity → CommunitySummary | discovery job (Leiden/Louvain over co-mentions) |
| `IN_PROJECT` | Page → Project | ≈ `schema:isPartOf`; assigned at promotion |

Every fact edge (`WORKS_AT`, `PART_OF`, `RELATED_TO`, `CREATED_BY`) carries
`source_page_url` — ≈ `prov:wasDerivedFrom` pointing at the canonical page URL.

## Evidence lifecycle (SQLite layer)

Captures carry `evidence_status ∈ {staged, promoted, excluded}` — user
intent, distinct from the pipeline's `extraction_status`
(`pending → extracted → loaded`). Staged captures get chunks + embeddings
only (the `chunks` table powers recall); only promoted captures are
extracted and loaded into the graph, so **every Page node in Neo4j is
promoted evidence by construction**. Excluded captures leave recall
immediately; graph cleanup arrives with the deletion cascade.

## Invariants

- One canonical URL = one Page node; dedup hash = sha256(canonical_url + text).
- Claims: evidence quote must appear verbatim in the page text; statement
  must name ≥1 extracted primary/secondary entity; ≤8 per page.
- Entity resolution: exact normalized name/alias match → embedding ≥0.85 →
  new entity; every decision logged in `merge_log`.
- Unknown entity types are never written to the graph; they queue in
  `candidate_types` for the weekly governance review.
