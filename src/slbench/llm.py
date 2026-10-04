"""Claude API 呼び出しのラッパー（唯一の呼び出し口）。

- 累計呼び出し回数を api_usage.json に永続化し、上限（100回）に達したら BudgetExceeded で停止する。
  回数は「API にリクエストを送る直前」に加算して保存する（失敗した呼び出しも1回と数える）。
  このファイルはプログラムからリセットしない。
- 応答は (条件, 質問ID, 試行番号) をキーに cache/live/ に保存し、再実行では API を呼ばない。
- SDK の自動リトライは無効（max_retries=0）。リトライが隠れて回数を消費しないようにするため。
- dry_run=True のときは API を使わずモック応答を返す（cache/mock/ に保存、回数は加算しない）。
- 本番呼び出しは allow_live=True を明示したときだけ行う（CLI が確認後に渡す）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import config

DEFAULT_BASE_URL = "https://api.anthropic.com"
MAX_TOKENS = 4096

# temperature などのサンプリング指定を受け付けるモデル（Claude 4.7 以降の Opus、Sonnet 5 系、Fable 系は 400 を返す）
SAMPLING_OK_PREFIXES = ("claude-sonnet-4-6", "claude-sonnet-4-5", "claude-opus-4-6", "claude-opus-4-5",
                        "claude-haiku-4-5", "claude-3")


class BudgetExceeded(RuntimeError):
    pass


class LiveCallNotAllowed(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    cached: bool          # キャッシュから返したか
    api_called: bool      # この呼び出しで実際に API を叩いたか
    mock: bool
    stop_reason: str | None = None
    request_params: dict | None = None
    prompt_hash: str = ""


# --- 予算管理 ----------------------------------------------------------------------

def read_usage(path: Path | None = None) -> dict:
    path = path or config.API_USAGE
    if not path.exists():
        return {"limit": config.API_CALL_LIMIT, "count": 0, "calls": []}
    return json.loads(path.read_text(encoding="utf-8"))


def remaining(path: Path | None = None) -> int:
    u = read_usage(path)
    return max(0, min(u.get("limit", config.API_CALL_LIMIT), config.API_CALL_LIMIT) - u["count"])


def _reserve_call(meta: dict, path: Path | None = None) -> int:
    """呼び出し1回分を予約して保存する。上限到達なら例外。"""
    path = path or config.API_USAGE
    u = read_usage(path)
    limit = min(u.get("limit", config.API_CALL_LIMIT), config.API_CALL_LIMIT)
    if u["count"] >= limit:
        raise BudgetExceeded(f"Claude API の累計呼び出しが上限 {limit} 回に達しています（api_usage.json）")
    u["count"] += 1
    u["calls"].append({"n": u["count"], "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       **meta, "status": "sent"})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return u["count"]


def _mark_call(n: int, status: str, path: Path | None = None) -> None:
    path = path or config.API_USAGE
    u = read_usage(path)
    for c in u["calls"]:
        if c["n"] == n:
            c["status"] = status
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --- キャッシュ --------------------------------------------------------------------

def cache_path(condition: str, qid: str, trial: int, mock: bool, root: Path | None = None) -> Path:
    root = root or config.CACHE
    return root / ("mock" if mock else "live") / condition / f"{qid}__t{trial}.json"


def prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\x00" + user).encode()).hexdigest()[:16]


# --- リクエスト組み立て -------------------------------------------------------------

def request_params(model: str) -> dict:
    """モデルごとに受け付けられるパラメータで温度0相当の設定を作る。"""
    params: dict = {"model": model, "max_tokens": MAX_TOKENS}
    if model.startswith(SAMPLING_OK_PREFIXES):
        params["temperature"] = 0
    thinking = os.environ.get("THINKING", "off").lower()
    if thinking == "off" and model.startswith("claude-sonnet-5-5"):
        # Sonnet 5.5 は temperature を受け付けない。思考を止める最小設定は between_tools
        params["thinking"] = {"type": "between_tools"}
    elif thinking == "adaptive":
        params["thinking"] = {"type": "adaptive"}
    return params


def _api_call(system: str, user: str, params: dict):
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LiveCallNotAllowed("環境変数 ANTHROPIC_API_KEY が設定されていません")
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=os.environ.get("BENCH_ANTHROPIC_BASE_URL", DEFAULT_BASE_URL),
        max_retries=0,
        timeout=300.0,
    )
    return client.messages.create(system=system, messages=[{"role": "user", "content": user}], **params)


def call_llm(
    *,
    system: str,
    user: str,
    condition: str,
    qid: str,
    trial: int,
    dry_run: bool,
    allow_live: bool = False,
    mock_fn: Callable[[], str] | None = None,
    cache_root: Path | None = None,
    usage_path: Path | None = None,
    api_fn: Callable = _api_call,
) -> LLMResponse:
    model = config.model()
    ph = prompt_hash(system, user)
    cpath = cache_path(condition, qid, trial, dry_run, cache_root)
    if cpath.exists():
        d = json.loads(cpath.read_text(encoding="utf-8"))
        d.update(cached=True, api_called=False)
        return LLMResponse(**d)

    if dry_run:
        text = mock_fn() if mock_fn else ""
        resp = LLMResponse(text=text, input_tokens=len(system + user) // 3, output_tokens=len(text) // 3,
                           model=f"mock({model})", cached=False, api_called=False, mock=True,
                           stop_reason="end_turn", request_params=request_params(model), prompt_hash=ph)
    else:
        if not allow_live:
            raise LiveCallNotAllowed("本番呼び出しは確認後（--yes）にのみ許可されます")
        params = request_params(model)
        n = _reserve_call({"condition": condition, "qid": qid, "trial": trial, "model": model}, usage_path)
        try:
            msg = api_fn(system, user, params)
        except Exception as e:
            _mark_call(n, f"error: {type(e).__name__}: {e}"[:300], usage_path)
            raise
        _mark_call(n, "ok", usage_path)
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        resp = LLMResponse(text=text, input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
                           model=msg.model, cached=False, api_called=True, mock=False,
                           stop_reason=msg.stop_reason, request_params=params, prompt_hash=ph)

    cpath.parent.mkdir(parents=True, exist_ok=True)
    d = asdict(resp)
    d.update(cached=False, api_called=False)
    cpath.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return resp
