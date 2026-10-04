"""Lahman Baseball Database の CSV を data/raw/ に取得する。

取得元を順に試す:
  1. SABR 配布の CSV（baseballdatabank 形式の GitHub ミラー）
  2. PyPI の pylahman パッケージ（SABR 配布データを Parquet で同梱。2026-01-08 版）
     → wheel を展開し Parquet を CSV に変換して data/raw/ に置く

どちらも取得できない場合は README の「手動でデータを置く」手順を表示して終了する。

データは CC BY-SA 3.0（SABR / Sean Lahman）。リポジトリには含めない。
"""
from __future__ import annotations

import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
TABLES = ["People", "Batting", "Pitching", "Teams", "TeamsFranchises"]

GITHUB_CSV = "https://raw.githubusercontent.com/chadwickbureau/baseballdatabank/master/core/{table}.csv"
PYLAHMAN_WHEEL = (
    "https://files.pythonhosted.org/packages/6a/7c/"
    "079c043375e5582b78b6b52239a298f413f9794efd37a8e5f235070f19e3/"
    "pylahman-0.7.0-py3-none-any.whl"
)
PYLAHMAN_SHA256 = "a20f97a76d6414dbdf58d4e40f1d9e80dcef6f27eb0be913198f4c29778269d4"

MANUAL = """
自動取得できませんでした。以下のいずれかで CSV を手動で置いてください（README「手動でデータを置く」参照）:
  - https://sabr.org/lahman-database/ から CSV 版 zip をダウンロードし、
    {tables}.csv を data/raw/ に置く
  - または pip download pylahman==0.7.0 --no-deps で wheel を取得し、
    python scripts/fetch_data.py --wheel <wheelのパス> を実行する
"""


def _get(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (fixed URLs)
        return r.read()


def try_github() -> bool:
    try:
        blobs = {t: _get(GITHUB_CSV.format(table=t)) for t in TABLES}
    except Exception as e:  # network / 404
        print(f"[github] 取得失敗: {e}")
        return False
    for t, b in blobs.items():
        (RAW / f"{t}.csv").write_bytes(b)
    print("[github] CSV を取得しました")
    return True


def from_wheel(blob: bytes) -> None:
    import hashlib

    import duckdb

    digest = hashlib.sha256(blob).hexdigest()
    if digest != PYLAHMAN_SHA256:
        print(f"[pylahman] 警告: sha256 が想定と異なります ({digest})")
    tmp = ROOT / "data" / "_download"
    tmp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for t in TABLES:
            pq = tmp / f"{t}.parquet"
            pq.write_bytes(z.read(f"pylahman/data/{t}.parquet"))
            out = RAW / f"{t}.csv"
            duckdb.sql(f"COPY (SELECT * FROM read_parquet('{pq}')) TO '{out}' (HEADER, DELIMITER ',')")
    print("[pylahman] Parquet から CSV を生成しました")


def try_pypi() -> bool:
    try:
        blob = _get(PYLAHMAN_WHEEL, timeout=120)
    except Exception as e:
        print(f"[pylahman] 取得失敗: {e}")
        return False
    from_wheel(blob)
    return True


def main(argv: list[str]) -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    if "--wheel" in argv:
        from_wheel(Path(argv[argv.index("--wheel") + 1]).read_bytes())
    elif all((RAW / f"{t}.csv").exists() for t in TABLES) and "--force" not in argv:
        print("data/raw/ に CSV が揃っています（再取得は --force）")
    elif not (try_github() or try_pypi()):
        print(MANUAL.format(tables=", ".join(TABLES)))
        return 1
    for t in TABLES:
        p = RAW / f"{t}.csv"
        print(f"  {p.relative_to(ROOT)}  {p.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
