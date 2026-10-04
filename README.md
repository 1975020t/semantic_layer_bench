# semantic_layer_bench

LLM にデータ分析（自然言語 → SQL）をさせるとき、**セマンティック情報の与え方**で正答率がどう変わるかを比べる実験基盤です。
データは Lahman Baseball Database、実行エンジンは DuckDB、LLM は Claude API。結果は [report.md](report.md)。

## 比較する4条件

すべて同じ質問・同じモデル。変えるのは LLM に渡す情報と出力形式だけです（`src/slbench/prompts.py`）。

| 条件 | LLM に渡すもの | LLM の出力 |
|---|---|---|
| `raw` | テーブル名・カラム名・型 | SQL |
| `comments` | raw + テーブル・カラムの説明文（`semantic/comments.yaml`。DuckDB の `COMMENT ON` にも反映） | SQL |
| `context` | comments + 用語集・業務ルール（`semantic/glossary.md`）+ 例示SQL（`semantic/examples.yaml`） | SQL |
| `metrics` | メトリクス定義（`semantic/metrics.yaml`）+ 用語集 + 例示（同じ質問のメトリクスクエリ版） | `{measures, dimensions, filters, order_by, limit}` の JSON。自作コンパイラが SQL を生成 |

- `metrics` にも用語集を渡すのは、`context` との差を「SQL を書くか、宣言的メトリクスを選ぶか」に絞るためです。
- 例示には評価用の質問と同じものを入れていません（`tests/test_questions.py` で検査）。

## セットアップ

Docker は使いません。Python 3.11 以上と pip だけで動きます。

```bash
pip install -r requirements.txt
python bench.py fetch     # data/raw/ に CSV を取得
python bench.py ingest    # data/lahman.duckdb を作成
python -m pytest -q       # 単体テスト（API を使わない）
```

### データの取得と手動配置

`scripts/fetch_data.py` は次の順に取得を試します。

1. baseballdatabank 形式の CSV（GitHub ミラー）
2. PyPI の [pylahman](https://pypi.org/project/pylahman/) 0.7.0 の wheel（SABR 配布データ 2026-01-08 版を Parquet で同梱、1871〜2025年）。展開して CSV に変換します

どちらも取れない環境では、次のどちらかで手動で置いてください。

- <https://sabr.org/lahman-database/> から CSV 版をダウンロードし、`People.csv`, `Batting.csv`, `Pitching.csv`, `Teams.csv`, `TeamsFranchises.csv` を `data/raw/` に置く
- 別の環境で `pip download pylahman==0.7.0 --no-deps` した wheel を持ち込み、`python scripts/fetch_data.py --wheel <wheel のパス>` を実行する

そのあと `python bench.py ingest` を実行します。データ本体（`data/raw/`, `data/*.duckdb`）は git 管理しません。

### ライセンス

Lahman Baseball Database は SABR / Sean Lahman により [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) で提供されています。
このリポジトリにはデータ本体を含めず、取得スクリプトのみを置いています。データから作った成果物を配布する場合は CC BY-SA の表示・継承条件に従ってください。

## 実行手順

```bash
# 1) モックで全体を通す（API を使わない。結果は results/dryrun/）
python bench.py run --phase all --dry-run
python bench.py report --dry-run

# 2) 予定回数の確認（API は呼ばない）
python bench.py plan

# 3) 本番（確認後に --yes を付ける）
export ANTHROPIC_API_KEY=...        # コミットしない
export MODEL=claude-sonnet-5-5      # 省略時の既定
python bench.py run --phase smoke --yes   # 疎通確認   4回（各条件1問）
python bench.py run --phase main  --yes   # 本実験    80回（20問 × 4条件）
python bench.py run --phase repro --yes   # 再現性確認 16回（4問 × 4条件 × 追加1回）
python bench.py report                    # results/*.csv と report.md を生成
```

`run` は本番時に必ず「残り回数」と「予定回数（キャッシュ済みを除く）」を表示し、`--yes` がなければ止まります。予定が残りを超える場合も止まります。

## Claude API 上限の仕組み（累計100回）

すべての呼び出しは `src/slbench/llm.py` の `call_llm()` だけを通ります。

- **累計回数の永続化**: `api_usage.json` に回数と各呼び出しの記録（条件・質問・試行・時刻・成否）を保存します。リクエストを送る**直前**に加算して書き込むので、失敗した呼び出しやプロセスの中断も1回として数えます。上限（100）に達すると `BudgetExceeded` 例外で止まります。ファイルを書き換えて上限を100より大きくしても、コード側で100に制限されます。このファイルはプログラムからリセットしません（git で管理）。
- **キャッシュ**: 応答を `(条件, 質問ID, 試行番号)` をキーに `cache/live/<条件>/<質問ID>__t<試行>.json` に保存し、再実行では API を呼びません（git で管理し、コンテナが変わっても再利用できるようにしています）。試行番号は 疎通=0、本実験=1、再現性=2。
- **再試行なし**: SDK の自動リトライを `max_retries=0` で無効化しています。SQL がエラーでも LLM には聞き直しません（1質問1回。エラーは不正解として記録）。API エラーが出たらそのフェーズを中断します（同じ原因で残り回数を浪費しないため）。
- **モックモード**: `--dry-run` では API を使わず、正解SQL / 模範メトリクスクエリ（一部はわざと誤り）を返すモックで全体を通します。回数は加算せず、`cache/mock/` と `results/dryrun/` を使います。テストもすべて API を使いません。
- **本番は明示的な許可が必要**: `call_llm(allow_live=True)` のときだけ API を呼びます。CLI は `--yes` のときだけこれを渡します。

### モデルと temperature

- モデルは環境変数 `MODEL`（既定 `claude-sonnet-5-5`）。API キーは `ANTHROPIC_API_KEY`。接続先は `https://api.anthropic.com` に固定しています（`BENCH_ANTHROPIC_BASE_URL` で変更可。環境に別の `ANTHROPIC_BASE_URL` があっても使いません）。
- `temperature=0` は、それを受け付けるモデル（Sonnet 4.6 など）では必ず送ります。**Sonnet 5 系・Opus 4.7 以降は temperature を指定すると 400 エラーになる**ため送りません。代わりに Sonnet 5.5 では `thinking: {type: "between_tools"}`（拡張思考なし）を指定します（`THINKING=adaptive` で思考ありに変更可）。実際に送ったパラメータはキャッシュと結果ログに記録します。

## 採点

`src/slbench/grading.py`。生成SQL（metrics はコンパイル後SQL）の実行結果を正解SQLの結果と比較します。

- 列名と列順は無視（正解の各列に値が一致する列を予測側から探す）。予測側に余分な列があっても可、行数は一致が必要
- 行順は質問が順序を求める場合（`ordered: true`）のみ比較
- 数値は絶対誤差 1e-4 まで許容（整数と小数は区別しない）
- 記録: 正誤、エラー種別（SQLエラー / 結果不一致 / 回答不能 / APIエラー）、入出力トークン数、生成SQL、メトリクスクエリ、応答全文
- 再現性: 同一質問の1回目と2回目で、実行結果・正誤・SQL文字列が一致したか

## 質問セット

`questions/questions.yaml` に20問（5カテゴリ × 4問）。正解SQLは手書きし、実行して確認しています（`tests/test_questions.py` が実行・模範メトリクスとの一致・同順位がないことを検査）。

| カテゴリ | 例 |
|---|---|
| 単純集計 | 2019年の本塁打トップ5、2023年の打撃記録のある選手数 |
| 派生指標 | 規定打席以上の打率トップ5、規定投球回以上の防御率・WHIP、チームOPS |
| 結合・粒度の罠 | 移籍選手の stint 合算、teamID と franchID（エンゼルス CAL/ANA/LAA）、リーグ打率を率の平均で出す誤り |
| 曖昧な表現 | 「最強打者」「安定した投手」「最も成功した球団」「長打力」（正解の解釈は `semantic/glossary.md`） |
| 時系列・比較 | 年代別の打率と本塁打、年別K/9、前年比、リーグ別の1試合平均得点 |

q16（ISO = 長打力）と q19（前年比。ウィンドウ関数が必要）は `metrics.yaml` の定義では表せないため、metrics 条件では「回答不能」が適切な応答になります。

## ファイル構成

```
bench.py                    CLI（fetch / ingest / plan / run / report）
scripts/fetch_data.py       データ取得
src/slbench/
  ingest.py                 CSV → DuckDB、COMMENT ON
  prompts.py                条件別プロンプトと応答のパース
  metrics_compiler.py       メトリクスクエリ JSON → SQL
  llm.py                    API ラッパー（上限・キャッシュ・モック）
  mock.py                   --dry-run 用のモック応答
  runner.py                 実行と採点、JSONL 出力
  grading.py                結果比較
  report.py                 集計 CSV と report.md
semantic/                   comments.yaml, glossary.md, examples.yaml, metrics.yaml
questions/questions.yaml    質問・カテゴリ・正解SQL
tests/                      単体テスト（API を使わない）
results/                    1呼び出し1行の生ログ（smoke/main/repro.jsonl）と集計CSV
api_usage.json              API 累計呼び出し回数
cache/live/                 API 応答キャッシュ
```

## Databricks（Metric Views / Genie）への持ち込み

| このリポジトリ | Databricks |
|---|---|
| DuckDB `data/lahman.duckdb` の各テーブル | Unity Catalog のテーブル（例 `main.lahman.batting`）。CSV を Volume に置き `read_files` で取り込む |
| `semantic/comments.yaml`（`COMMENT ON TABLE/COLUMN`） | `COMMENT ON TABLE` / `ALTER TABLE ... ALTER COLUMN ... COMMENT`。Catalog Explorer の説明文としてそのまま Genie にも渡る |
| `semantic/metrics.yaml` の `metric_views` の各要素 | Metric View 1つ。`name`/`comment` を除いた `version, source, joins, dimensions, measures` を `CREATE VIEW main.lahman.<name> WITH METRICS LANGUAGE YAML AS $$ ... $$` の本体にする。`source` の `lahman.main.X` は `<catalog>.<schema>.x` に置換 |
| join の `on: source.teamID = teams.teamID AND ...` | Metric Views の `joins`（many-to-one の star schema）と同じ書式 |
| 数字で始まる列のバッククォート（`` source.`2B` ``） | Databricks SQL の識別子そのまま（コンパイラは DuckDB 用に `"2B"` へ変換） |
| measure（`SUM(...)/SUM(...)` の率指標） | Metric View の measure。問い合わせ時は `SELECT player_id, MEASURE(ops) FROM view GROUP BY ALL` で、粒度に応じて再集計される |
| メトリクスクエリ JSON（measures/dimensions/filters/order_by/limit） | `SELECT <dims>, MEASURE(<m>) ... WHERE <dimフィルタ> GROUP BY ALL HAVING ... ORDER BY ... LIMIT` に対応 |
| `semantic/glossary.md`（用語集・業務ルール） | Genie space の General instructions / 用語の説明（または measure・dimension の `comment`, `synonyms`） |
| `semantic/examples.yaml`（質問と SQL） | Genie space の Example SQL queries（trusted assets） |
| 4条件の比較 | Genie にテーブルだけ / コメント付き / instructions+example SQL / Metric Views を与えた構成に対応 |

違いとして、このコンパイラは「measures は1つの metric view から」「measure を参照する measure は展開済みの式で書く」「ウィンドウ関数なし」という Metric Views の基本形だけを実装しています。
