"""Vercel serverless entrypoint.

Vercel rewrites every /api/* request here (see vercel.json) and preserves the
original path, so the FastAPI app routes it exactly as it does locally. This
file only puts the backend package on the import path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.main import app  # noqa: E402,F401  (exported as the ASGI handler)
