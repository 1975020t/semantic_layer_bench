"""宣言的メトリクスのコンパイラ。

LLM が返す JSON（メトリクスクエリ）を semantic/metrics.yaml の定義から SQL に変換する。

クエリ形式:
    {
      "measures":   ["ops", "plate_appearances"],          # 1つ以上。すべて同じ metric view に属すること
      "dimensions": ["player_id"],                          # GROUP BY する次元（0個以上）
      "filters": [                                          # dimension への条件は WHERE、measure への条件は HAVING
        {"field": "year", "op": "=", "value": 2019},
        {"field": "plate_appearances", "op": ">=", "value": 502}
      ],
      "order_by": [{"field": "ops", "direction": "desc"}],
      "limit": 1
    }
    回答不能のとき: {"unanswerable": true, "reason": "..."}

op は = != > >= < <= in not_in between。in/not_in の value は配列、between は [下限, 上限]。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import config

OPS = {"=", "!=", "<>", ">", ">=", "<", "<=", "in", "not_in", "between"}


class CompileError(Exception):
    """定義にない measure/dimension、形式の誤りなど。"""


class Unanswerable(Exception):
    """LLM が回答不能と判断した。"""


@dataclass
class MetricView:
    name: str
    source: str
    joins: list[dict]
    dimensions: dict[str, dict]
    measures: dict[str, dict]
    comment: str = ""
    filter: str | None = None
    raw: dict = field(default_factory=dict)


def _duckdb_expr(expr: str) -> str:
    """Databricks 流のバッククォート識別子を DuckDB のダブルクォートに変換する。"""
    return re.sub(r"`([^`]+)`", r'"\1"', expr.strip())


def load_views(path: Path | None = None) -> dict[str, MetricView]:
    path = path or config.SEMANTIC / "metrics.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    views: dict[str, MetricView] = {}
    for v in spec["metric_views"]:
        views[v["name"]] = MetricView(
            name=v["name"],
            source=v["source"],
            # PyYAML（YAML 1.1）は `on:` を真偽値 True のキーとして読むので戻す
            joins=[{("on" if k is True else k): x for k, x in j.items()} for j in v.get("joins", []) or []],
            dimensions={d["name"]: d for d in v.get("dimensions", [])},
            measures={m["name"]: m for m in v.get("measures", [])},
            comment=v.get("comment", ""),
            filter=v.get("filter"),
            raw=v,
        )
    return views


def _literal(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise CompileError(f"値の型が不正: {v!r}")


class Compiler:
    def __init__(self, views: dict[str, MetricView] | None = None):
        self.views = views or load_views()
        self.measure_index: dict[str, list[str]] = {}
        for v in self.views.values():
            for m in v.measures:
                self.measure_index.setdefault(m, []).append(v.name)

    # --- view 解決 ---------------------------------------------------------------
    def _resolve_view(self, q: dict) -> MetricView:
        measures = q.get("measures") or []
        if not measures:
            raise CompileError("measures が空です（1つ以上必要）")
        if q.get("metric_view"):
            name = q["metric_view"]
            if name not in self.views:
                raise CompileError(f"未定義の metric view: {name}")
            return self.views[name]
        candidates: set[str] | None = None
        for m in measures:
            if m not in self.measure_index:
                raise CompileError(f"未定義の measure: {m}")
            s = set(self.measure_index[m])
            candidates = s if candidates is None else candidates & s
        if not candidates:
            raise CompileError(f"measures が単一の metric view に収まりません: {measures}")
        if len(candidates) > 1:
            raise CompileError(f"metric view が一意に決まりません（metric_view を指定）: {sorted(candidates)}")
        return self.views[candidates.pop()]

    # --- コンパイル -------------------------------------------------------------
    def compile(self, q: dict) -> str:
        if not isinstance(q, dict):
            raise CompileError("クエリは JSON オブジェクトである必要があります")
        if q.get("unanswerable"):
            raise Unanswerable(q.get("reason", ""))
        unknown_keys = set(q) - {"metric_view", "measures", "dimensions", "filters", "order_by", "limit", "reason"}
        if unknown_keys:
            raise CompileError(f"未知のキー: {sorted(unknown_keys)}")
        view = self._resolve_view(q)
        dims = list(q.get("dimensions") or [])
        measures = list(q.get("measures") or [])
        for d in dims:
            if d not in view.dimensions:
                raise CompileError(f"未定義の dimension（{view.name}）: {d}")
        for m in measures:
            if m not in view.measures:
                raise CompileError(f"measure {m} は {view.name} にありません")

        def expr_of(name: str) -> tuple[str, str]:
            if name in view.dimensions:
                return "dimension", _duckdb_expr(view.dimensions[name]["expr"])
            if name in view.measures:
                return "measure", _duckdb_expr(view.measures[name]["expr"])
            raise CompileError(f"未定義のフィールド（{view.name}）: {name}")

        where: list[str] = [f"({_duckdb_expr(view.filter)})"] if view.filter else []
        having: list[str] = []
        for f in q.get("filters") or []:
            if not isinstance(f, dict) or not {"field", "op"} <= set(f):
                raise CompileError(f"filter の形式が不正: {f!r}")
            kind, e = expr_of(f["field"])
            op = str(f["op"]).lower()
            if op not in OPS:
                raise CompileError(f"未対応の演算子: {f['op']}")
            val = f.get("value")
            if op in ("in", "not_in"):
                if not isinstance(val, list) or not val:
                    raise CompileError("in / not_in の value は空でない配列")
                cond = f"{e} {'IN' if op == 'in' else 'NOT IN'} ({', '.join(_literal(x) for x in val)})"
            elif op == "between":
                if not isinstance(val, list) or len(val) != 2:
                    raise CompileError("between の value は [下限, 上限]")
                cond = f"{e} BETWEEN {_literal(val[0])} AND {_literal(val[1])}"
            else:
                cond = f"{e} {'<>' if op == '!=' else op} {_literal(val)}"
            (where if kind == "dimension" else having).append(cond)

        select = [f'{expr_of(n)[1]} AS "{n}"' for n in dims + measures]

        order: list[str] = []
        for o in q.get("order_by") or []:
            if isinstance(o, str):
                o = {"field": o}
            name = o.get("field")
            direction = str(o.get("direction", "asc")).upper()
            if direction not in ("ASC", "DESC"):
                raise CompileError(f"direction は asc/desc: {direction}")
            if name in dims or name in measures:
                order.append(f'"{name}" {direction}')
            else:
                order.append(f"{expr_of(name)[1]} {direction}")

        # 使われている join だけを含める（不要な join による行の重複を避ける）
        used_text = " ".join(select + where + having + order)
        joins = []
        for j in view.joins:
            if re.search(rf"\b{re.escape(j['name'])}\.", used_text):
                joins.append(f"LEFT JOIN {j['source']} AS {j['name']} ON {_duckdb_expr(j['on'])}")

        sql = [f"SELECT {', '.join(select)}", f"FROM {view.source} AS source"] + joins
        if where:
            sql.append("WHERE " + " AND ".join(where))
        if dims:
            sql.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims))))
        if having:
            sql.append("HAVING " + " AND ".join(having))
        if order:
            sql.append("ORDER BY " + ", ".join(order))
        if q.get("limit") is not None:
            lim = q["limit"]
            if not isinstance(lim, int) or lim < 0:
                raise CompileError(f"limit は 0 以上の整数: {lim!r}")
            sql.append(f"LIMIT {lim}")
        return "\n".join(sql)


def describe_for_prompt(views: dict[str, MetricView] | None = None) -> str:
    """LLM に渡すメトリクス定義の説明（YAML そのものを渡す）。"""
    views = views or load_views()
    out = []
    for v in views.values():
        out.append(f"## metric view: {v.name}\n{v.comment}\n")
        out.append("dimensions:")
        for d in v.dimensions.values():
            out.append(f"  - {d['name']}: {d.get('comment', '')}")
        out.append("measures:")
        for m in v.measures.values():
            out.append(f"  - {m['name']}: {m.get('comment', '')}")
        out.append("")
    return "\n".join(out)
