"""FalkorDB connection and schema."""
from falkordb import FalkorDB, Graph

from . import config

# Exact-match (range) indexes. Every import MERGEs papers by id, so this index turns
# each MERGE into an index lookup instead of a scan of all papers.
INDEXES = [
    ("Paper", "id"),          # OpenAlex work ID, e.g. W2075481535
    ("Paper", "doi"),         # look papers up by DOI
    ("Retraction", "record_id"),
]


def graph() -> Graph:
    db = FalkorDB(
        host=config.FALKORDB_HOST,
        port=config.FALKORDB_PORT,
        username=config.FALKORDB_USERNAME,
        password=config.FALKORDB_PASSWORD,
    )
    return db.select_graph(config.GRAPH_NAME)


def existing_indexes(g: Graph) -> set[tuple[str, str]]:
    rows = g.query("CALL db.indexes() YIELD label, properties").result_set
    return {(label, prop) for label, props in rows for prop in props}


def ensure_schema(g: Graph) -> list[tuple[str, str]]:
    """Create any missing indexes. Safe to run any number of times."""
    have = existing_indexes(g)
    created = []
    for label, prop in INDEXES:
        if (label, prop) not in have:
            g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            created.append((label, prop))
    return created


if __name__ == "__main__":
    g = graph()
    created = ensure_schema(g)
    print(f"graph '{config.GRAPH_NAME}': created {created or 'nothing'}; "
          f"indexes now {sorted(existing_indexes(g))}")
