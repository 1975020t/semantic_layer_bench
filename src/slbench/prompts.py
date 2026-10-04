"""条件別のプロンプト生成。

4条件とも同じ質問文を user メッセージで渡し、system に条件ごとの情報を入れる。
  raw      : テーブル名・カラム名・型
  comments : raw + テーブル・カラムの説明文（comments.yaml）
  context  : comments + 用語集・業務ルール（glossary.md）+ 例示SQL（examples.yaml の sql）
  metrics  : メトリクス定義（metrics.yaml）+ 用語集 + 例示（examples.yaml の metrics）。
             SQL は書かせず、メトリクスクエリの JSON だけを返させる。
"""
from __future__ import annotations

import json
import re

import duckdb
import yaml

from . import config
from .ingest import load_comments
from .metrics_compiler import describe_for_prompt
from .questions import load_examples

SQL_RULES = """あなたは DuckDB の SQL に精通したデータアナリストです。
下のデータベース情報を使い、ユーザーの質問に答える SQL を1つ書いてください。
- DuckDB の SQL 方言で書く
- 質問が求める列だけを返す。数値は丸めない
- 出力は ```sql で始まるコードブロック1つだけにし、説明は書かない"""

METRICS_RULES = """あなたはセマンティックレイヤー（宣言的メトリクス）を使うデータアナリストです。
SQL は書かず、下のメトリクス定義だけを使って質問に答える「メトリクスクエリ」を JSON で1つ返してください。
JSON の形式:
{
  "measures": ["<measure名>", ...],        // 1つ以上。すべて同じ metric view のもの
  "dimensions": ["<dimension名>", ...],    // GROUP BY する次元（不要なら空配列）
  "filters": [{"field": "<dimension または measure 名>", "op": "=", "value": 2019}],
                                           // op は = != > >= < <= in not_in between
                                           // in/not_in は配列、between は [下限, 上限]
                                           // measure への条件は集計後の条件（HAVING）になる
  "order_by": [{"field": "<名前>", "direction": "asc" または "desc"}],
  "limit": 5                               // 不要なら null
}
- 名前は定義にあるものだけを使う。式や計算は書けない
- 定義にある measure と dimension の組合せで答えられない質問には
  {"unanswerable": true, "reason": "<理由>"} を返す
- 出力は ```json で始まるコードブロック1つだけにし、説明は書かない"""


def _schema(con: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[str, str]]]:
    rows = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {t: [] for t in config.TABLES}
    for t, c, ty in rows:
        if t in out:
            out[t].append((c, ty))
    return out


def schema_text(con: duckdb.DuckDBPyConnection, with_comments: bool) -> str:
    comments = load_comments()["tables"] if with_comments else {}
    lines = ["# テーブル定義"]
    for table, cols in _schema(con).items():
        spec = comments.get(table, {})
        lines.append(f"\n## {table}" + (f" -- {spec['comment']}" if spec.get("comment") else ""))
        for c, ty in cols:
            desc = spec.get("columns", {}).get(c)
            lines.append(f"- {c} {ty}" + (f" -- {desc}" if desc else ""))
    return "\n".join(lines)


def glossary_text() -> str:
    return (config.SEMANTIC / "glossary.md").read_text(encoding="utf-8").strip()


def examples_sql_text() -> str:
    parts = ["# 例示（質問と SQL）"]
    for e in load_examples():
        if e.get("sql"):
            parts.append(f"質問: {e['question']}\n```sql\n{e['sql'].strip()}\n```")
    return "\n\n".join(parts)


def examples_metrics_text() -> str:
    parts = ["# 例示（質問とメトリクスクエリ）"]
    for e in load_examples():
        parts.append(f"質問: {e['question']}\n```json\n{json.dumps(e['metrics'], ensure_ascii=False)}\n```")
    return "\n\n".join(parts)


def build_system(condition: str, con: duckdb.DuckDBPyConnection) -> str:
    if condition == "raw":
        return f"{SQL_RULES}\n\n{schema_text(con, with_comments=False)}"
    if condition == "comments":
        return f"{SQL_RULES}\n\n{schema_text(con, with_comments=True)}"
    if condition == "context":
        return "\n\n".join([SQL_RULES, schema_text(con, with_comments=True), glossary_text(), examples_sql_text()])
    if condition == "metrics":
        return "\n\n".join(
            [METRICS_RULES, "# メトリクス定義\n" + describe_for_prompt(), glossary_text(), examples_metrics_text()]
        )
    raise ValueError(f"unknown condition: {condition}")


def build_user(question: str) -> str:
    return f"質問: {question}"


# --- 応答のパース ----------------------------------------------------------------

def extract_sql(text: str) -> str:
    m = re.search(r"```sql\s*(.*?)```", text, re.S | re.I) or re.search(r"```\s*(.*?)```", text, re.S)
    sql = (m.group(1) if m else text).strip()
    return sql.rstrip(";").strip()


def extract_json(text: str) -> dict:
    m = re.search(r"```json\s*(.*?)```", text, re.S | re.I) or re.search(r"```\s*(.*?)```", text, re.S)
    body = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        obj = yaml.safe_load(body)  # 末尾カンマ等の軽微な崩れを許容
    if not isinstance(obj, dict):
        raise ValueError("JSON オブジェクトではありません")
    return obj
