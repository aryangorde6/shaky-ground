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
```

FalkorDB Browser: http://localhost:3000

## Graph model

*(to be filled in)*

## Cypher queries and algorithms

*(to be filled in)*

## Use of AI coding assistants

An AI coding assistant was used during development. Details to follow.
