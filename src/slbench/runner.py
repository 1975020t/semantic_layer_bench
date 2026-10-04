"""実験の実行: 質問×条件ごとに LLM を呼び、SQL を実行して採点し、1呼び出し1行の JSONL を書く。"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import config, llm, mock
from .grading import compare, norm
from .ingest import connect
from .metrics_compiler import CompileError, Compiler, Unanswerable
from .prompts import build_system, build_user, extract_json, extract_sql
from .questions import Question, load_questions, load_smoke, repro_ids

PHASES = {"smoke": 0, "main": 1, "repro": 2}  # phase -> trial 番号（キャッシュキーの一部）
QUERY_TIMEOUT_S = 60
MAX_ROWS_KEPT = 200


@dataclass
class Task:
    phase: str
    condition: str
    question: Question

    @property
    def trial(self) -> int:
        return PHASES[self.phase]


def tasks_for(phase: str) -> list[Task]:
    if phase == "smoke":
        qs = [load_smoke()]
    elif phase == "main":
        qs = load_questions()
    elif phase == "repro":
        ids = repro_ids()
        qs = [q for q in load_questions() if q.id in ids]
    else:
        raise ValueError(phase)
    return [Task(phase, c, q) for q in qs for c in config.CONDITIONS]


def planned_live_calls(phases: list[str]) -> list[Task]:
    """キャッシュがなく、実際に API を呼ぶことになるタスク。"""
    return [t for p in phases for t in tasks_for(p)
            if not llm.cache_path(t.condition, t.question.id, t.trial, mock=False).exists()]


def run_query(con: duckdb.DuckDBPyConnection, sql: str, timeout_s: int = QUERY_TIMEOUT_S) -> list[tuple]:
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()


def _safe_connection() -> duckdb.DuckDBPyConnection:
    con = connect(read_only=True)
    con.execute("SET enable_external_access = false")  # LLM の SQL がファイル読み書きをしないように
    return con


def evaluate(task: Task, resp: llm.LLMResponse, con, compiler: Compiler, gold_rows: list[tuple]) -> dict:
    q = task.question
    rec: dict = {
        "phase": task.phase, "condition": task.condition, "qid": q.id, "category": q.category,
        "trial": task.trial, "model": resp.model, "mock": resp.mock, "cached": resp.cached,
        "api_called": resp.api_called, "stop_reason": resp.stop_reason,
        "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
        "metrics_answerable": q.metrics_answerable, "ordered": q.ordered,
        "response_text": resp.text, "generated_sql": None, "metrics_query": None,
        "correct": False, "error_type": None, "error_detail": None, "compare_detail": None,
        "n_rows_gold": len(gold_rows), "n_rows_pred": None, "pred_rows": None,
    }
    try:
        if task.condition == "metrics":
            mq = extract_json(resp.text)
            rec["metrics_query"] = mq
            sql = compiler.compile(mq)
        else:
            sql = extract_sql(resp.text)
            if not sql:
                raise ValueError("SQL が抽出できません")
        rec["generated_sql"] = sql
    except Unanswerable as e:
        rec.update(error_type="unanswerable", error_detail=str(e))
        return rec
    except (CompileError, ValueError) as e:
        rec.update(error_type="sql_error", error_detail=f"compile: {e}")
        return rec
    try:
        rows = run_query(con, sql)
    except Exception as e:  # duckdb.Error, interrupt
        rec.update(error_type="sql_error", error_detail=f"{type(e).__name__}: {e}"[:500])
        return rec
    rec["n_rows_pred"] = len(rows)
    rec["pred_rows"] = [[norm(v) for v in r] for r in rows[:MAX_ROWS_KEPT]]
    cmp = compare(gold_rows, rows, q.ordered)
    rec["compare_detail"] = cmp.detail
    rec["correct"] = cmp.match
    if not cmp.match:
        rec["error_type"] = "mismatch"
    return rec


def results_dir(dry_run: bool) -> Path:
    return config.RESULTS / "dryrun" if dry_run else config.RESULTS


def run_phase(phase: str, *, dry_run: bool, allow_live: bool = False, log=print) -> list[dict]:
    con = _safe_connection()
    gold_con = connect(read_only=True)
    compiler = Compiler()
    gold_cache: dict[str, list[tuple]] = {}
    systems = {c: build_system(c, gold_con) for c in config.CONDITIONS}
    out: list[dict] = []
    for t in tasks_for(phase):
        q = t.question
        if q.id not in gold_cache:
            gold_cache[q.id] = gold_con.execute(q.gold_sql).fetchall()
        try:
            resp = llm.call_llm(
                system=systems[t.condition], user=build_user(q.question),
                condition=t.condition, qid=q.id, trial=t.trial, dry_run=dry_run, allow_live=allow_live,
                mock_fn=lambda t=t: mock.respond(t.condition, t.question, t.trial),
            )
        except llm.BudgetExceeded:
            raise
        except Exception as e:  # API エラー: 再試行はせず記録して次へ
            rec = {"phase": phase, "condition": t.condition, "qid": q.id, "category": q.category,
                   "trial": t.trial, "correct": False, "error_type": "api_error",
                   "error_detail": f"{type(e).__name__}: {e}"[:500], "api_called": True,
                   "input_tokens": 0, "output_tokens": 0, "metrics_answerable": q.metrics_answerable}
            out.append(rec)
            log(f"  {phase} {t.condition:8s} {q.id}: API エラー {rec['error_detail']}")
            if isinstance(e, llm.LiveCallNotAllowed) or not dry_run:
                # 本番では最初の API エラーで止める（同じ原因で残りの回数を浪費しないため）
                _write(phase, out, dry_run)
                raise RuntimeError(f"API エラーのため {phase} を中断しました: {rec['error_detail']}") from e
            continue
        rec = evaluate(t, resp, con, compiler, gold_cache[q.id])
        out.append(rec)
        mark = "OK " if rec["correct"] else f"NG({rec['error_type']})"
        src = "cache" if resp.cached else ("mock" if resp.mock else "API")
        log(f"  {phase} {t.condition:8s} {q.id}: {mark} [{src}] in={rec['input_tokens']} out={rec['output_tokens']}")
    _write(phase, out, dry_run)
    return out


def _write(phase: str, out: list[dict], dry_run: bool) -> None:
    d = results_dir(dry_run)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{phase}.jsonl").open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
