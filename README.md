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

FalkorDB Browser: http://localhost:3000

Loads are idempotent (every write is a `MERGE` keyed by ID) and API responses are cached in `data/`, so a second run sends no requests and changes nothing.

## Graph model

```
(:Paper {id, doi, title, year, cited_by, type, is_review, venue, oa_url, retracted, refs})
(:Paper)-[:CITES {cls, reliance}]->(:Paper)
(:Retraction {record_id, date, nature, reason, notice_doi})-[:RETRACTS]->(:Paper)
```

- `Paper.id` is the OpenAlex work ID. `refs` lists the IDs of every work the paper cites; a `CITES` edge exists only when both papers are loaded, so the rest of `refs` is the frontier still to explore.
- `CITES.cls` is how much the citing paper relies on the cited one: `FOUNDATIONAL` 1.0, `SUPPORTING` 0.6, `BACKGROUND` 0.15, `CRITICAL` 0, `UNKNOWN` 0.3 (no evidence yet). A retraction notice citing the paper it retracts is `CRITICAL`.
- `retracted` comes from OpenAlex or from a Retraction Watch notice. A paper with a `RETRACTS` edge is confirmed by Retraction Watch.
- No author names, institutions or countries are stored: Shaky Ground scores papers, never people.

Data: [Retraction Watch database](https://api.labs.crossref.org/data/retractionwatch) (via Crossref) and [OpenAlex](https://openalex.org).

## Cypher queries and algorithms

*(to be filled in)*

## Use of AI coding assistants

An AI coding assistant was used during development. Details to follow.
