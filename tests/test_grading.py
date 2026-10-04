"""採点ロジックの単体テスト。"""
import decimal

from slbench.grading import compare


def test_exact_match():
    assert compare([("a", 1)], [("a", 1)], ordered=True).match


def test_column_names_and_order_ignored():
    assert compare([("a", 1), ("b", 2)], [(1, "a"), (2, "b")], ordered=True).match


def test_extra_columns_allowed():
    r = compare([("a",)], [("a", 1.083, 650)], ordered=False)
    assert r.match and "余分な列" in r.detail


def test_missing_column_fails():
    assert not compare([("a", 1)], [("a",)], ordered=False).match


def test_row_order_only_when_required():
    gold = [("a", 3), ("b", 2)]
    pred = [("b", 2), ("a", 3)]
    assert compare(gold, pred, ordered=False).match
    assert not compare(gold, pred, ordered=True).match


def test_numeric_tolerance():
    assert compare([(0.31274900398406374,)], [(0.31275,)], ordered=False).match       # 小数4桁程度は許容
    assert not compare([(0.3127,)], [(0.3137,)], ordered=False).match
    assert compare([(5,)], [(5.0,)], ordered=False).match                             # int と float
    assert compare([(decimal.Decimal("1.50000"),)], [(1.5,)], ordered=False).match


def test_rounded_rate_fails():
    # 指示に反して丸めた値は不一致（ERA 2.5287 vs 2.53）
    assert not compare([(2.528700906344411,)], [(2.53,)], ordered=False).match


def test_row_count_mismatch():
    r = compare([("a",), ("b",)], [("a",), ("b",), ("c",)], ordered=False)
    assert not r.match and "行数" in r.detail


def test_duplicate_rows_matter():
    # 二重計上で行が重複したら不一致（多重集合として比較）
    assert not compare([("a", 1), ("b", 1)], [("a", 1), ("a", 1)], ordered=False).match


def test_null_handling():
    assert compare([(None, 1)], [(None, 1)], ordered=False).match
    assert not compare([(None,)], [(0,)], ordered=False).match


def test_column_permutation_with_same_values():
    # 値の多重集合が同じ列が2つあっても、行の対応で正しい割当を探す
    gold = [(1, 2), (2, 1)]
    pred = [(2, 1), (1, 2)]
    assert compare(gold, pred, ordered=True).match


def test_empty_results():
    assert compare([], [], ordered=False).match
    assert not compare([("a",)], [], ordered=False).match


def test_string_vs_number_not_equal():
    assert not compare([("2019",)], [(2019,)], ordered=False).match
