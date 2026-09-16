"""Tests run from the repository root or anywhere else, with no database and
without importing app.main (which needs DB_PASS and APP_SECRET)."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
