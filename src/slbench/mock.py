"""--dry-run 用のモック応答。API を使わずにパイプライン全体（抽出→実行→採点→集計）を通すためのもの。

SQL 条件は正解 SQL を、metrics 条件は模範メトリクスクエリを返す。ただし採点や集計の分岐を
確認できるよう、(条件, 質問ID) のハッシュで決まる一部の質問ではわざと誤った応答を返す。
"""
from __future__ import annotations

import hashlib
import json

from .questions import Question


def _bucket(condition: str, qid: str) -> int:
    return int(hashlib.sha256(f"{condition}:{qid}".encode()).hexdigest(), 16) % 10


def respond(condition: str, q: Question, trial: int = 1) -> str:
    b = _bucket(condition, q.id)
    if condition == "metrics":
        if not q.metrics_answerable or q.reference_metrics is None:
            return '```json\n{"unanswerable": true, "reason": "mock: 定義外"}\n```'
        query = dict(q.reference_metrics)
        if b == 0:
            query["measures"] = ["undefined_measure"]  # コンパイルエラー
        elif b == 1 and query.get("limit"):
            query["limit"] = query["limit"] + 1        # 結果不一致
        return "```json\n" + json.dumps(query, ensure_ascii=False) + "\n```"
    sql = q.gold_sql.strip()
    if b == 0:
        sql = "SELEC " + sql                           # SQL エラー
    elif b == 1:
        sql = f"SELECT * FROM ({sql}) AS t LIMIT 0"    # 結果不一致
    elif b == 2 and trial >= 2:
        sql = f"SELECT * FROM ({sql}) AS t LIMIT 0"    # 再現性の不一致
    return f"```sql\n{sql}\n```"
