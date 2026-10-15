# Deploying the live demo

The live demo is the read-only web UI on a Hugging Face Space, reading a FalkorDB Cloud instance
that holds a snapshot of the local graph. Nothing on the live site spends API credits or model calls
(`ALLOW_RUNS=0` is set in the Dockerfile).

## 1. Snapshot the local graph

```bash
uv run python -m shaky.snapshot export data/snapshot.jsonl.gz
```

The snapshot keeps every node, edge and property the UI reads and drops each paper's raw reference
list, which only the loaders use. On Oct 15: 9,043 papers and 109,774 citations, 1.7 MB gzipped;
restored, the graph uses 21 MB (the free Cloud tier allows 100 MB). The file stays on your machine
(`data/` is gitignored).

## 2. FalkorDB Cloud

1. In the FalkorDB Cloud console, create a free instance and note its host, port, username and password.
2. Put them in `.env.cloud` (gitignored) in this folder:

   ```
   FALKORDB_HOST=<host>
   FALKORDB_PORT=<port>
   FALKORDB_USERNAME=<username>
   FALKORDB_PASSWORD=<password>
   GRAPH_NAME=shaky
   ```

3. Restore the snapshot and check it:

   ```bash
   ENV_FILE=.env.cloud uv run python -m shaky.snapshot restore data/snapshot.jsonl.gz
   ENV_FILE=.env.cloud uv run python -m shaky.preflight --snapshot data/snapshot.jsonl.gz
   ```

The free tier keeps no data across a restart, stops after a day without use and is deleted after
seven. If preflight fails on counts, restore again (`--replace` overwrites).

## 3. Hugging Face Space

1. Create a Space with the **Docker** SDK (blank template).
2. Clone it next to this folder and copy the app in:

   ```bash
   git clone https://huggingface.co/spaces/<you>/shaky-ground ../shaky-space
   deploy/make-space.sh ../shaky-space
   cd ../shaky-space && git add -A && git commit -m "Shaky Ground web UI" && git push
   ```

3. In the Space's settings, add the four `FALKORDB_*` values as **secrets** (not variables), plus
   `GRAPH_NAME=shaky`.

## 4. Before recording and before submitting

```bash
ENV_FILE=.env.cloud uv run python -m shaky.preflight --snapshot data/snapshot.jsonl.gz --url https://<you>-shaky-ground.hf.space
```

Every line should say PASS: FalkorDB answers, the counts and the demo paper's tiers match the
snapshot, the graph fits the free tier, and the site serves the page and the same answer.
