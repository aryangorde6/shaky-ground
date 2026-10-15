"""Settings, read from the environment (a .env file during development)."""
import os
from pathlib import Path

from dotenv import load_dotenv

# ENV_FILE picks another settings file, e.g. ENV_FILE=.env.cloud for the deployment's database
load_dotenv(os.getenv("ENV_FILE") or None)

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_USERNAME = os.getenv("FALKORDB_USERNAME") or None
FALKORDB_PASSWORD = os.getenv("FALKORDB_PASSWORD") or None
GRAPH_NAME = os.getenv("GRAPH_NAME", "shaky")

OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "")
S2_API_KEY = os.getenv("S2_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Whether the web UI and the MCP server may start work that writes to the graph and spends API credits
# and model calls (a foundation check, recomputing risk for another root). Off for a public deployment.
ALLOW_RUNS = os.getenv("ALLOW_RUNS", "1") == "1"
DEMO_ROOT = os.getenv("DEMO_ROOT", "W2075481535")     # what the UI opens on: Lesné et al. 2006, Nature

# Downloads and the API cache (gitignored). Defaults to shaky-ground/data.
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
