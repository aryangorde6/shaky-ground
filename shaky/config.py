"""Settings, read from the environment (a .env file during development)."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_USERNAME = os.getenv("FALKORDB_USERNAME") or None
FALKORDB_PASSWORD = os.getenv("FALKORDB_PASSWORD") or None
GRAPH_NAME = os.getenv("GRAPH_NAME", "shaky")

OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "")
S2_API_KEY = os.getenv("S2_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
