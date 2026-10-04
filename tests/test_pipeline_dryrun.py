"""モックモードで実行→採点→集計まで通るか（API は呼ばない。data/lahman.duckdb が必要）。"""
import pytest

from slbench import config, llm, report, runner

pytestmark = pytest.mark.skipif(not config.DB_PATH.exists(), reason="data/lahman.duckdb がありません")


def test_dry_run_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(config, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(config, "API_USAGE", tmp_path / "api_usage.json")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def boom(*a):
        raise AssertionError("dry-run で API を呼んではいけない")
    monkeypatch.setattr(llm, "_api_call", boom)

    for phase in ["smoke", "main", "repro"]:
        recs = runner.run_phase(phase, dry_run=True, log=lambda *_: None)
        assert recs and not any(r["api_called"] for r in recs)
    main = report.read_jsonl(tmp_path / "results" / "dryrun" / "main.jsonl")
    assert len(main) == 80
    # モックは基本的に正解を返すので、大半は正解になる。定義外の2問は metrics で回答不能
    assert sum(r["correct"] for r in main) >= 50
    assert {r["qid"] for r in main if r["condition"] == "metrics" and r["error_type"] == "unanswerable"} == {"q16", "q19"}
    md = report.build(dry_run=True)
    assert "条件 × カテゴリの正答率" in md and "再現性" in md
    assert not (tmp_path / "api_usage.json").exists()
    # 本番用の予定回数は 4 + 80 + 16 = 100（キャッシュなしの状態）
    assert len(runner.planned_live_calls(["smoke", "main", "repro"])) == 100
