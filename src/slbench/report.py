"""結果 JSONL から集計 CSV と report.md を作る。

report.md は自動生成の表に、手書きの考察（results/discussion.md があれば）を差し込んで作る。
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from . import config
from .grading import compare
from .questions import categories, load_questions

ERROR_LABEL = {"sql_error": "SQLエラー", "mismatch": "結果不一致", "unanswerable": "回答不能", "api_error": "APIエラー"}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.0f}% ({n}/{d})" if d else "-"


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _md_table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build(dry_run: bool = False) -> str:
    rdir = config.RESULTS / "dryrun" if dry_run else config.RESULTS
    main = read_jsonl(rdir / "main.jsonl")
    repro = read_jsonl(rdir / "repro.jsonl")
    smoke = read_jsonl(rdir / "smoke.jsonl")
    if not main:
        raise SystemExit(f"{rdir / 'main.jsonl'} がありません。先に run --phase main を実行してください")
    cats = categories()
    conds = config.CONDITIONS
    qs = {q.id: q for q in load_questions()}

    # 条件 × カテゴリの正答率
    acc = defaultdict(lambda: [0, 0])
    for r in main:
        for key in [(r["condition"], r["category"]), (r["condition"], "_all")]:
            acc[key][1] += 1
            acc[key][0] += int(r["correct"])
    acc_rows = []
    for cat in list(cats) + ["_all"]:
        label = cats.get(cat, "全体")
        acc_rows.append([label] + [_pct(*acc[(c, cat)]) for c in conds])
    _write_csv(rdir / "summary_accuracy.csv", ["category"] + conds,
               [[cat] + [f"{acc[(c, cat)][0]}/{acc[(c, cat)][1]}" for c in conds] for cat in list(cats) + ["_all"]])

    # metrics 条件: 定義内の質問だけの正答率
    inside = [r for r in main if r["condition"] == "metrics" and r.get("metrics_answerable", True)]
    outside = [r for r in main if r["condition"] == "metrics" and not r.get("metrics_answerable", True)]
    inside_acc = _pct(sum(r["correct"] for r in inside), len(inside))
    refusal = _pct(sum(r["error_type"] == "unanswerable" for r in outside), len(outside))
    false_refusal = sum(r["error_type"] == "unanswerable" for r in inside)

    # エラー種別
    err = defaultdict(int)
    for r in main:
        err[(r["condition"], r["error_type"] or "correct")] += 1
    etypes = ["correct", "sql_error", "mismatch", "unanswerable", "api_error"]
    err_rows = [[{"correct": "正解", **ERROR_LABEL}[e]] + [err[(c, e)] for c in conds] for e in etypes
                if any(err[(c, e)] for c in conds)]
    _write_csv(rdir / "summary_errors.csv", ["error_type"] + conds, [[e] + [err[(c, e)] for c in conds] for e in etypes])

    # トークン
    tok = defaultdict(lambda: [0, 0, 0])
    for r in main:
        t = tok[r["condition"]]
        t[0] += r.get("input_tokens") or 0
        t[1] += r.get("output_tokens") or 0
        t[2] += 1
    tok_rows = [[c, f"{tok[c][0] / tok[c][2]:,.0f}", f"{tok[c][1] / tok[c][2]:,.0f}", f"{tok[c][0]:,}", f"{tok[c][1]:,}"]
                for c in conds if tok[c][2]]
    _write_csv(rdir / "summary_tokens.csv", ["condition", "avg_input", "avg_output", "total_input", "total_output"],
               [[c, tok[c][0] / tok[c][2], tok[c][1] / tok[c][2], tok[c][0], tok[c][1]] for c in conds if tok[c][2]])

    # 質問別
    by_q = {(r["qid"], r["condition"]): r for r in main}
    q_rows = []
    for qid, q in qs.items():
        marks = []
        for c in conds:
            r = by_q.get((qid, c))
            marks.append("-" if r is None else ("○" if r["correct"] else "×" + ERROR_LABEL.get(r["error_type"], "")))
        q_rows.append([qid, cats[q.category], q.question] + marks)
    _write_csv(rdir / "per_question.csv", ["qid", "category", "question"] + conds,
               [[r[0], r[1], r[2]] + r[3:] for r in q_rows])

    # 再現性
    rep_rows, rep_csv = [], []
    same_result_n = same_correct_n = same_sql_n = 0
    for r2 in repro:
        r1 = by_q.get((r2["qid"], r2["condition"]))
        if not r1:
            continue
        if r1.get("pred_rows") is not None and r2.get("pred_rows") is not None:
            same_result = compare(r1["pred_rows"], r2["pred_rows"], ordered=True).match
        else:
            same_result = r1.get("error_type") == r2.get("error_type") and r1.get("pred_rows") == r2.get("pred_rows")
        same_sql = (r1.get("generated_sql") or "").strip() == (r2.get("generated_sql") or "").strip()
        same_correct = r1["correct"] == r2["correct"]
        same_result_n += same_result
        same_correct_n += same_correct
        same_sql_n += same_sql
        rep_rows.append([r2["qid"], r2["condition"], "○" if r1["correct"] else "×", "○" if r2["correct"] else "×",
                         "一致" if same_result else "不一致", "一致" if same_sql else "不一致"])
        rep_csv.append([r2["qid"], r2["condition"], r1["correct"], r2["correct"], same_result, same_sql])
    if rep_csv:
        _write_csv(rdir / "summary_repro.csv", ["qid", "condition", "trial1_correct", "trial2_correct",
                                                 "same_result", "same_sql"], rep_csv)

    # 誤り一覧
    wrong = [r for r in main if not r["correct"]]
    wrong_rows = [[r["qid"], r["condition"], ERROR_LABEL.get(r["error_type"], r["error_type"]),
                   (r.get("compare_detail") or r.get("error_detail") or "").replace("|", "/").replace("\n", " ")[:120]]
                  for r in sorted(wrong, key=lambda r: (r["qid"], conds.index(r["condition"])))]

    models = sorted({r.get("model") for r in main if r.get("model")})
    api_calls = sum(1 for r in main + repro + smoke if r.get("api_called"))
    usage = json.loads(config.API_USAGE.read_text()) if config.API_USAGE.exists() else {"count": 0}
    title = "# セマンティックレイヤー比較実験レポート" + ("（dry-run / モック）" if dry_run else "")
    smoke_line = ", ".join(f"{r['condition']}={'○' if r['correct'] else '×'}" for r in smoke) or "未実行"

    parts = [
        title,
        "",
        f"- モデル: {', '.join(models)}",
        f"- 質問: 20問（5カテゴリ × 4問）、条件: {', '.join(conds)}、各1回（本実験）",
        f"- Claude API 累計呼び出し: {usage.get('count', 0)} / {config.API_CALL_LIMIT}（api_usage.json）",
        f"- 疎通確認: {smoke_line}",
        "",
        "## 1. 条件 × カテゴリの正答率",
        "",
        _md_table(["カテゴリ"] + conds, acc_rows),
        "",
        f"metrics 条件のうち、定義内の質問（{len(inside)}問）に限った正答率: {inside_acc}。"
        f"定義外の質問（{len(outside)}問）で「回答不能」を返せた割合: {refusal}。"
        f"定義内なのに回答不能とした数: {false_refusal}。",
        "",
        "## 2. エラー種別",
        "",
        _md_table(["結果"] + conds, err_rows),
        "",
        "## 3. トークン量（1呼び出しあたり平均 / 本実験合計）",
        "",
        _md_table(["条件", "平均入力", "平均出力", "合計入力", "合計出力"], tok_rows),
        "",
        "## 4. 再現性（同一質問の2回目）",
        "",
        (_md_table(["質問", "条件", "1回目", "2回目", "実行結果", "SQL文字列"], rep_rows) + "\n\n"
         f"実行結果が一致: {same_result_n}/{len(rep_rows)}、正誤が一致: {same_correct_n}/{len(rep_rows)}、"
         f"SQL（metrics はコンパイル後SQL）が文字列として一致: {same_sql_n}/{len(rep_rows)}")
        if rep_rows else "未実行",
        "",
        "## 5. 質問別の結果",
        "",
        _md_table(["ID", "カテゴリ", "質問"] + conds, q_rows),
        "",
        "## 6. 誤りの一覧",
        "",
        _md_table(["ID", "条件", "種別", "詳細"], wrong_rows) if wrong_rows else "なし",
        "",
    ]
    disc = rdir / "discussion.md"
    if disc.exists():
        parts += [disc.read_text(encoding="utf-8").strip(), ""]
    md = "\n".join(parts)
    out = rdir / "report.md" if dry_run else config.ROOT / "report.md"
    out.write_text(md, encoding="utf-8")
    print(f"-> {out}（API 呼び出しを伴った行: {api_calls}）")
    return md
