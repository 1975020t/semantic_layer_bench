"""採点: 生成 SQL の実行結果を正解 SQL の結果と比較する。

規則
- 列名は無視する。列順も無視する（正解の各列に対応する列を予測側から探す）。
- 予測側に余分な列があってもよい（正解の列がすべて含まれていれば可）。行数は一致が必要。
- 行順は ordered=True のときのみ比較する。
- 数値は abs_tol=1e-4（小数4桁程度）の誤差を許容する。整数と小数は区別しない。
"""
from __future__ import annotations

import datetime as dt
import decimal
import itertools
import math
from dataclasses import dataclass
from typing import Any, Sequence

ABS_TOL = 1e-4
REL_TOL = 1e-6

Row = Sequence[Any]


def norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, decimal.Decimal)):
        f = float(v)
        return f if not math.isnan(f) else None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return str(v).strip()


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    return a == b


def _sort_key(v: Any):
    # 数値は丸めた値でソートして、許容誤差内の値が同じ位置に来るようにする
    if v is None:
        return (0, 0.0, "")
    if isinstance(v, float):
        return (1, round(v, 3), "")
    return (2, 0.0, str(v))


def _rows_equal(gold: list[tuple], pred: list[tuple], ordered: bool) -> bool:
    if len(gold) != len(pred):
        return False
    if not ordered:
        gold = sorted(gold, key=lambda r: [_sort_key(x) for x in r])
        pred = sorted(pred, key=lambda r: [_sort_key(x) for x in r])
    return all(all(values_equal(a, b) for a, b in zip(g, p)) for g, p in zip(gold, pred))


def _column_multiset_match(gcol: list, pcol: list) -> bool:
    return _rows_equal([(x,) for x in gcol], [(x,) for x in pcol], ordered=False)


@dataclass
class Comparison:
    match: bool
    detail: str


def compare(gold_rows: list[Row], pred_rows: list[Row], ordered: bool) -> Comparison:
    g = [tuple(norm(v) for v in r) for r in gold_rows]
    p = [tuple(norm(v) for v in r) for r in pred_rows]
    if len(g) != len(p):
        return Comparison(False, f"行数不一致（正解 {len(g)} 行 / 生成 {len(p)} 行）")
    if not g:
        return Comparison(True, "ともに0行")
    ng, np_ = len(g[0]), len(p[0])
    if np_ < ng:
        return Comparison(False, f"列数不足（正解 {ng} 列 / 生成 {np_} 列）")

    gcols = [[r[i] for r in g] for i in range(ng)]
    pcols = [[r[j] for r in p] for j in range(np_)]
    # 正解の各列について、値の多重集合が一致する予測列を候補にする
    candidates = []
    for gc in gcols:
        cands = [j for j, pc in enumerate(pcols) if _column_multiset_match(gc, pc)]
        if not cands:
            return Comparison(False, "値が一致しない列がある")
        candidates.append(cands)
    for combo in itertools.islice(itertools.product(*candidates), 5000):
        if len(set(combo)) != len(combo):
            continue
        projected = [tuple(r[j] for j in combo) for r in p]
        if _rows_equal(g, projected, ordered):
            return Comparison(True, "一致" if np_ == ng else f"一致（余分な列 {np_ - ng} 列は無視）")
    return Comparison(False, "行の組合せまたは行順が一致しない" if ordered else "行の組合せが一致しない")
