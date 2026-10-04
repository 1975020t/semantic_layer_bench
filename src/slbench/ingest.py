"""data/raw/*.csv を DuckDB（data/lahman.duckdb）に取り込み、comments.yaml を COMMENT ON で反映する。"""
from __future__ import annotations

import duckdb
import yaml

from . import config


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def load_comments() -> dict:
    return yaml.safe_load((config.SEMANTIC / "comments.yaml").read_text(encoding="utf-8"))


def apply_comments(con: duckdb.DuckDBPyConnection, comments: dict) -> None:
    for table, spec in comments["tables"].items():
        con.execute(f'COMMENT ON TABLE "{table}" IS {_q(spec["comment"])}')
        for col, text in spec.get("columns", {}).items():
            con.execute(f'COMMENT ON COLUMN "{table}"."{col}" IS {_q(str(text))}')


def ingest(db_path=config.DB_PATH) -> None:
    missing = [t for t in config.TABLES if not (config.DATA_RAW / f"{t}.csv").exists()]
    if missing:
        raise SystemExit(f"CSV がありません: {missing}。先に python scripts/fetch_data.py を実行（README参照）")
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    for t in config.TABLES:
        path = config.DATA_RAW / f"{t}.csv"
        con.execute(
            f'CREATE TABLE "{t}" AS SELECT * FROM read_csv({_q(str(path))}, header=true, '
            f"sample_size=-1, nullstr='')"
        )
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  {t}: {n:,} rows")
    apply_comments(con, load_comments())
    con.close()
    print(f"-> {db_path}")


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(config.DB_PATH), read_only=read_only)
