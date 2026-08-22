import sys
from pathlib import Path

# Make the existing backend package importable from the Vercel Python function.
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from server import app  # noqa: E402,F401
