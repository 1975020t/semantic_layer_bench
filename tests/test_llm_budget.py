"""API ラッパーの上限・キャッシュ・モックのテスト。実 API は呼ばない（api_fn を偽物に差し替え）。"""
import json
from types import SimpleNamespace

import pytest

from slbench import llm


def fake_msg(text="```sql\nSELECT 1\n```"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=11, output_tokens=7),
                           model="fake-model", stop_reason="end_turn")


@pytest.fixture
def paths(tmp_path):
    return {"cache_root": tmp_path / "cache", "usage_path": tmp_path / "api_usage.json"}


def call(paths, qid="q01", trial=1, dry_run=False, allow_live=True, api_fn=None, **kw):
    return llm.call_llm(system="sys", user="user", condition="raw", qid=qid, trial=trial, dry_run=dry_run,
                        allow_live=allow_live, api_fn=api_fn or (lambda s, u, p: fake_msg()), **paths, **kw)


def test_counts_and_persists(paths):
    r = call(paths)
    assert r.api_called and not r.cached and r.input_tokens == 11
    u = json.loads(paths["usage_path"].read_text())
    assert u["count"] == 1 and u["calls"][0]["status"] == "ok"


def test_cache_hit_does_not_call_api(paths):
    call(paths)
    def boom(*a):
        raise AssertionError("API を呼んではいけない")
    r = call(paths, api_fn=boom)
    assert r.cached and not r.api_called
    assert json.loads(paths["usage_path"].read_text())["count"] == 1


def test_cache_key_includes_trial(paths):
    call(paths, trial=1)
    call(paths, trial=2)
    assert json.loads(paths["usage_path"].read_text())["count"] == 2


def test_budget_exceeded_stops(paths):
    paths["usage_path"].write_text(json.dumps({"limit": 100, "count": 100, "calls": []}))
    with pytest.raises(llm.BudgetExceeded):
        call(paths)


def test_limit_cannot_be_raised_above_100(paths):
    paths["usage_path"].write_text(json.dumps({"limit": 1000, "count": 100, "calls": []}))
    with pytest.raises(llm.BudgetExceeded):
        call(paths)


def test_failed_call_still_counted_and_not_retried(paths):
    n = {"calls": 0}
    def fail(*a):
        n["calls"] += 1
        raise RuntimeError("500")
    with pytest.raises(RuntimeError):
        call(paths, api_fn=fail)
    u = json.loads(paths["usage_path"].read_text())
    assert n["calls"] == 1 and u["count"] == 1 and u["calls"][0]["status"].startswith("error")


def test_dry_run_uses_mock_and_never_counts(paths):
    def boom(*a):
        raise AssertionError("dry-run で API を呼んではいけない")
    r = call(paths, dry_run=True, allow_live=False, api_fn=boom, mock_fn=lambda: "```sql\nSELECT 2\n```")
    assert r.mock and "SELECT 2" in r.text
    assert not paths["usage_path"].exists()
    assert (paths["cache_root"] / "mock" / "raw" / "q01__t1.json").exists()


def test_live_requires_explicit_permission(paths):
    with pytest.raises(llm.LiveCallNotAllowed):
        call(paths, allow_live=False)
    assert not paths["usage_path"].exists()


def test_request_params_per_model(monkeypatch):
    monkeypatch.delenv("THINKING", raising=False)
    p = llm.request_params("claude-sonnet-4-6")
    assert p["temperature"] == 0 and "thinking" not in p
    p = llm.request_params("claude-sonnet-5-5")
    assert "temperature" not in p and p["thinking"] == {"type": "between_tools"}
    p = llm.request_params("claude-opus-5-5")
    assert "temperature" not in p and "thinking" not in p
