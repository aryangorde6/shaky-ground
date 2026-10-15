# Deploying the live demo

The live demo is the read-only web UI on [Railway](https://railway.com), reading a FalkorDB Cloud
instance that holds a snapshot of the local graph. Both run in the US East region (Virginia), because
one page view makes many small queries. Nothing on the live site spends API credits or model calls
(`ALLOW_RUNS=0` is set in the Dockerfile).

Live: https://shaky-ground-production.up.railway.app

## 1. Snapshot the local graph

```bash
uv run python -m shaky.snapshot export data/snapshot.jsonl.gz
```

The snapshot keeps every node, edge and property the UI reads and drops each paper's raw reference
list, which only the loaders use. On Oct 15: 10,852 papers and 147,434 citations, 2.0 MB gzipped;
restored, the graph uses 29 MB (the free Cloud tier allows 100 MB). The file stays on your machine
(`data/` is gitignored).

## 2. FalkorDB Cloud

1. In the FalkorDB Cloud console, create a free instance (region `us-east-1`, next to Railway's
   US East) and note its host, port, username and password.
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

## 3. Railway

1. New project → **GitHub Repository** → paste `https://github.com/aryangorde6/shaky-ground`. A public
   repository deploys without connecting a GitHub account. Railway builds the `Dockerfile`.
2. In the service's **Variables**, add `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`,
   `FALKORDB_PASSWORD`, `GRAPH_NAME=shaky` and `PORT=7860`.
3. In **Settings → Networking**, generate a domain for port 7860. Keep **Serverless** off: the service
   then never sleeps, so no visitor waits for a cold start.
4. Without a GitHub connection Railway does not deploy new commits by itself: use **Check for updates**
   in the service's settings, then redeploy.

The server connects to FalkorDB on first use. If the database can't be reached, the page still loads
and the API answers 503 with that reason, instead of the container crashing.

**Keep-alive.** The free Cloud instance stops after a day without use. `.github/workflows/keep-alive.yml`
reads the graph through the live site every 3 hours, and a failed run (site or graph down) shows up in
the repository's Actions tab.

## 4. Before recording and before submitting

```bash
ENV_FILE=.env.cloud uv run python -m shaky.preflight --snapshot data/snapshot.jsonl.gz --url https://shaky-ground-production.up.railway.app
```

Every line should say PASS: FalkorDB answers, the counts and the demo paper's tiers match the
snapshot, the graph fits the free tier, and the site serves the page and the same answer.
