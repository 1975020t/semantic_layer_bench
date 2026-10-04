"""実験の入口。

  python bench.py fetch                    # data/raw/ に Lahman CSV を取得
  python bench.py ingest                   # data/lahman.duckdb を作成（COMMENT ON も反映）
  python bench.py check                    # 正解SQLと模範メトリクスクエリを実行して確認
  python bench.py plan [--phase all]       # API 残り回数と予定回数を表示（API は呼ばない）
  python bench.py run --phase main --dry-run          # モックで実行（API を使わない）
  python bench.py run --phase smoke --yes             # 本番実行（予定回数を表示し --yes で確認済みとする）
  python bench.py report [--dry-run]       # 集計 CSV と report.md を生成

phase: smoke（疎通 4回） / main（本実験 80回） / repro（再現性 16回） / all
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from slbench import config, llm, runner  # noqa: E402

PHASE_ORDER = ["smoke", "main", "repro"]


def _phases(p: str) -> list[str]:
    return PHASE_ORDER if p == "all" else [p]


def cmd_plan(phases: list[str]) -> int:
    planned = runner.planned_live_calls(phases)
    rem = llm.remaining()
    print(f"モデル: {config.model()}  リクエスト設定: {llm.request_params(config.model())}")
    print(f"API 残り回数: {rem} / {config.API_CALL_LIMIT}")
    for p in phases:
        n = sum(1 for t in planned if t.phase == p)
        print(f"  {p:6s}: 予定 {n} 回（全 {len(runner.tasks_for(p))} 件、残りはキャッシュ済み）")
    print(f"合計予定: {len(planned)} 回 → 実行後の残り {rem - len(planned)} 回")
    return 0 if len(planned) <= rem else 2


def cmd_run(phases: list[str], dry_run: bool, yes: bool) -> int:
    if not dry_run:
        code = cmd_plan(phases)
        if code:
            print("予定回数が残り回数を超えるため中止します")
            return code
        if not yes:
            print("本番呼び出しには確認が必要です。内容を確認のうえ --yes を付けて再実行してください")
            return 3
    for p in phases:
        print(f"[{p}] {'dry-run' if dry_run else 'live'}")
        recs = runner.run_phase(p, dry_run=dry_run, allow_live=not dry_run)
        ok = sum(r["correct"] for r in recs)
        print(f"[{p}] 正解 {ok}/{len(recs)}  API 残り {llm.remaining()} 回")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("ingest")
    sub.add_parser("check")
    pp = sub.add_parser("plan")
    pp.add_argument("--phase", default="all", choices=PHASE_ORDER + ["all"])
    rp = sub.add_parser("run")
    rp.add_argument("--phase", required=True, choices=PHASE_ORDER + ["all"])
    rp.add_argument("--dry-run", action="store_true", help="API を使わずモックで実行")
    rp.add_argument("--yes", action="store_true", help="予定回数を確認済みとして本番実行する")
    rep = sub.add_parser("report")
    rep.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "fetch":
        return subprocess.call([sys.executable, str(config.ROOT / "scripts" / "fetch_data.py")])
    if a.cmd == "ingest":
        from slbench.ingest import ingest
        ingest()
        return 0
    if a.cmd == "check":
        return subprocess.call([sys.executable, "-m", "pytest", "-q", "tests/test_questions.py"], cwd=config.ROOT)
    if a.cmd == "plan":
        return cmd_plan(_phases(a.phase))
    if a.cmd == "run":
        return cmd_run(_phases(a.phase), a.dry_run, a.yes)
    if a.cmd == "report":
        from slbench.report import build
        build(dry_run=a.dry_run)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
