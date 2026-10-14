# Shaky Ground

**Finds the science built on retracted research, not just the papers that cite it.**

Shaky Ground loads a citation graph into FalkorDB from public data (Retraction Watch via Crossref, OpenAlex, Semantic Scholar). Each citation is weighted by how much the citing paper *relies* on the cited one, and the risk of a chain is the product of those weights, computed in Cypher. An agent decides which branches to explore and returns every answer with its evidence.

Graph Hacks: Context for AI Agents (WeMakeDevs × FalkorDB), **Track 01: Agents That Act on Connected Data**.

> Work in progress during the hackathon (Oct 15–18, 2026).

## Run it locally

Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
# 1. FalkorDB, with a named volume so the graph survives restarts
docker compose up -d
# or, without Compose:
docker run -d --name shaky-falkordb -p 6379:6379 -p 3000:3000 \
  -v shaky-falkordb-data:/var/lib/falkordb/data falkordb/falkordb:latest

# 2. Settings: copy, then fill in your keys
cp .env.example .env

# 3. Python dependencies, then create the graph's indexes
uv sync
uv run python -m shaky.db

# 4. Load a paper, the papers it cites and the papers that cite it
#    (Lesné et al. 2006, Nature, retracted 2024: ~2,800 papers, ~30 OpenAlex credits)
uv run python -m shaky.load W2075481535
```

```bash
# 5. Evidence: the sentences in which each citer cites the paper (Semantic Scholar)
uv run python -m shaky.evidence W2075481535

# 6. Judge reliance from those sentences (Gemini: ~6 calls for Lesné's 754 sentences)
uv run python -m shaky.judge W2075481535 --provider gemini --batch 130
```

FalkorDB Browser: http://localhost:3000

Loads are idempotent (every write is a `MERGE` keyed by ID) and API responses are cached in `data/`, so a second run sends no requests and changes nothing.

## Graph model

```
(:Paper {id, doi, title, year, cited_by, type, is_review, venue, oa_url, retracted, refs})
(:Paper)-[:CITES {cls, reliance, contexts, intents, influential, evidence, why, judged_by, judged_at}]->(:Paper)
(:Retraction {record_id, date, nature, reason, notice_doi})-[:RETRACTS]->(:Paper)
```

- `Paper.id` is the OpenAlex work ID. `refs` lists the IDs of every work the paper cites; a `CITES` edge exists only when both papers are loaded, so the rest of `refs` is the frontier still to explore.
- `CITES.cls` is how much the citing paper relies on the cited one: `FOUNDATIONAL` 1.0, `SUPPORTING` 0.6, `BACKGROUND` 0.15, `CRITICAL` 0, `UNKNOWN` 0.3 (no evidence yet). A retraction notice citing the paper it retracts is `CRITICAL`.
- **How reliance is judged.** The model never picks a number. It reads only the citation sentences and answers yes/no questions: does the citing paper dispute it, fail to reproduce it, or mention the retraction? use its method or data? rely on its specific finding? Fixed rules turn the answers into a class (critical first; a statement backed by a list of 3+ references doesn't rest on any one of them), a table turns the class into a weight, and the code stores the deciding sentence verbatim as `CITES.evidence`, with `judged_by` and `judged_at`. A judged edge is never sent to the model again.
- `retracted` comes from OpenAlex or from a Retraction Watch notice. A paper with a `RETRACTS` edge is confirmed by Retraction Watch.
- No author names, institutions or countries are stored: Shaky Ground scores papers, never people.

Data: [Retraction Watch database](https://api.labs.crossref.org/data/retractionwatch) (via Crossref), [OpenAlex](https://openalex.org) and [Semantic Scholar](https://www.semanticscholar.org/product/api).

## Cypher queries and algorithms

*(to be filled in)*

## Use of AI coding assistants

An AI coding assistant was used during development. Details to follow.
