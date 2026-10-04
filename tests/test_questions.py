"""質問セットの検査: 正解SQLが実行でき、模範メトリクスクエリの結果と一致し、例示と重複しないこと。

data/lahman.duckdb が必要（python bench.py fetch && python bench.py ingest）。なければ skip。
"""
import pytest

from slbench import config
from slbench.grading import compare
from slbench.metrics_compiler import Compiler
from slbench.questions import categories, load_examples, load_questions, load_smoke, repro_ids

pytestmark = pytest.mark.skipif(not config.DB_PATH.exists(), reason="data/lahman.duckdb がありません")

QS = load_questions()


@pytest.fixture(scope="module")
def con():
    from slbench.ingest import connect
    c = connect(read_only=True)
    yield c
    c.close()


def test_question_set_shape():
    assert len(QS) == 20 and len({q.id for q in QS}) == 20
    cats = categories()
    for cat in cats:
        assert sum(q.category == cat for q in QS) == 4
    assert sum(not q.metrics_answerable for q in QS) == 2
    assert set(repro_ids()) <= {q.id for q in QS} and len(repro_ids()) == 4


def test_examples_do_not_overlap_eval_questions():
    eval_texts = {q.question for q in QS} | {load_smoke().question}
    for e in load_examples():
        assert e["question"] not in eval_texts


@pytest.mark.parametrize("q", QS + [load_smoke()], ids=lambda q: q.id)
def test_gold_sql_runs_and_metrics_reference_agrees(con, q):
    gold = con.execute(q.gold_sql).fetchall()
    assert gold, "正解が空"
    if q.metrics_answerable:
        assert q.reference_metrics
        pred = con.execute(Compiler().compile(q.reference_metrics)).fetchall()
        r = compare(gold, pred, q.ordered)
        assert r.match, r.detail


def test_ordered_answers_have_no_ties_at_top(con):
    # 順序を採点する質問で、並べ替えキーに同値があると正解が一意に決まらない
    for q in QS:
        if not q.ordered:
            continue
        rows = con.execute(q.gold_sql).fetchall()
        assert len(set(rows)) == len(rows), q.id


def test_examples_sql_and_metrics_agree(con):
    comp = Compiler()
    for e in load_examples():
        if not e.get("sql"):
            continue
        gold = con.execute(e["sql"]).fetchall()
        pred = con.execute(comp.compile(e["metrics"])).fetchall()
        assert compare(gold, pred, ordered=False).match, e["question"]
