"""パスと実験設定。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_RAW = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "lahman.duckdb"
SEMANTIC = ROOT / "semantic"
QUESTIONS = ROOT / "questions" / "questions.yaml"
RESULTS = ROOT / "results"
CACHE = ROOT / "cache"
API_USAGE = ROOT / "api_usage.json"

TABLES = ["People", "Batting", "Pitching", "Teams", "TeamsFranchises"]
CONDITIONS = ["raw", "comments", "context", "metrics"]

API_CALL_LIMIT = 100
DEFAULT_MODEL = "claude-sonnet-5-5"


def model() -> str:
    return os.environ.get("MODEL", DEFAULT_MODEL)
