"""メトリクスコンパイラの単体テスト（小さな合成データを使う。API・本データ不要）。"""
import duckdb
import pytest

from slbench.metrics_compiler import CompileError, Compiler, Unanswerable, load_views


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    c.execute("ATTACH ':memory:' AS lahman")
    c.execute("USE lahman")
    c.execute('CREATE TABLE People (playerID VARCHAR, nameFirst VARCHAR, nameLast VARCHAR, birthCountry VARCHAR)')
    c.execute("INSERT INTO People VALUES ('a', 'Al', 'A', 'USA'), ('b', 'Bo', 'B', 'Japan')")
    c.execute('CREATE TABLE Teams (yearID BIGINT, lgID VARCHAR, teamID VARCHAR, franchID VARCHAR, name VARCHAR, '
              'G BIGINT, W BIGINT, L BIGINT, R BIGINT, RA BIGINT, WSWin VARCHAR, LgWin VARCHAR, divID VARCHAR, '
              'attendance BIGINT)')
    c.execute("INSERT INTO Teams VALUES "
              "(2000, 'AL', 'X1', 'FX', 'X One', 10, 6, 4, 50, 40, 'Y', 'Y', 'E', 100),"
              "(2000, 'NL', 'Y1', 'FY', 'Y One', 10, 3, 7, 30, 45, 'N', 'N', 'W', 100),"
              "(2001, 'AL', 'X2', 'FX', 'X Two', 10, 5, 5, 40, 40, 'N', 'N', 'E', 100)")
    c.execute('CREATE TABLE TeamsFranchises (franchID VARCHAR, franchName VARCHAR)')
    c.execute("INSERT INTO TeamsFranchises VALUES ('FX', 'Franchise X'), ('FY', 'Franchise Y')")
    c.execute('CREATE TABLE Batting (playerID VARCHAR, yearID BIGINT, stint BIGINT, teamID VARCHAR, lgID VARCHAR, '
              'G BIGINT, AB BIGINT, R BIGINT, H BIGINT, "2B" BIGINT, "3B" BIGINT, HR BIGINT, RBI BIGINT, SB BIGINT, '
              'BB BIGINT, SO BIGINT, HBP BIGINT, SH BIGINT, SF BIGINT)')
    # 選手 a は 2000 年に X1→Y1 へ移籍（stint 2行）。打率は 1/10 と 9/30。合計なら 10/40 = .250（平均は .200）
    c.execute("INSERT INTO Batting VALUES "
              "('a', 2000, 1, 'X1', 'AL', 5, 10, 1, 1, 0, 0, 0, 1, 0, 0, 3, 0, 0, 0),"
              "('a', 2000, 2, 'Y1', 'NL', 5, 30, 2, 9, 2, 1, 3, 8, 1, 2, 1, 0, 0, 0),"
              "('b', 2000, 1, 'X1', 'AL', 9, 40, 5, 10, 1, 0, 1, 4, 0, 4, 9, 1, 1, 1),"
              "('b', 2001, 1, 'X2', 'AL', 9, 40, 5, 20, 0, 0, 0, 2, 0, 0, 2, 0, 0, 0)")
    c.execute('CREATE TABLE Pitching (playerID VARCHAR, yearID BIGINT, stint BIGINT, teamID VARCHAR, lgID VARCHAR, '
              'W BIGINT, L BIGINT, G BIGINT, GS BIGINT, SV BIGINT, IPOuts BIGINT, H BIGINT, ER BIGINT, HR BIGINT, '
              'BB BIGINT, SO BIGINT)')
    # ERA: stint1 27*1/81=0.333, stint2 27*9/27=9.0 → 合計なら 27*10/108 = 2.5（平均は 4.67）
    c.execute("INSERT INTO Pitching VALUES "
              "('a', 2000, 1, 'X1', 'AL', 1, 0, 1, 1, 0, 81, 3, 1, 0, 0, 9),"
              "('a', 2000, 2, 'Y1', 'NL', 0, 1, 1, 1, 0, 27, 6, 9, 0, 3, 3)")
    yield c
    c.close()


@pytest.fixture(scope="module")
def comp():
    return Compiler(load_views())


def q(con, sql):
    return con.execute(sql).fetchall()


def test_rate_recomputed_from_totals_across_stints(con, comp):
    sql = comp.compile({"measures": ["batting_avg"], "dimensions": ["player_id"],
                        "filters": [{"field": "year", "op": "=", "value": 2000}],
                        "order_by": [{"field": "player_id", "direction": "asc"}]})
    rows = q(con, sql)
    assert rows[0] == ("a", pytest.approx(0.25))         # (1+9)/(10+30)。stint の打率の平均 0.2 ではない
    assert rows[1] == ("b", pytest.approx(0.25))


def test_era_recomputed_not_averaged(con, comp):
    sql = comp.compile({"measures": ["era"], "dimensions": ["player_id"]})
    (pid, era), = q(con, sql)
    assert era == pytest.approx(2.5)
    assert era != pytest.approx((27.0 * 1 / 81 + 27.0 * 9 / 27) / 2)


def test_rate_at_coarser_grain(con, comp):
    # リーグ全体（次元なし）でも合計から再計算
    sql = comp.compile({"measures": ["batting_avg", "home_runs"], "filters": [{"field": "year", "op": "=", "value": 2000}]})
    assert q(con, sql) == [(pytest.approx(20 / 80), 4)]


def test_stints_not_double_counted_by_player_year(con, comp):
    sql = comp.compile({"measures": ["home_runs", "team_count"], "dimensions": ["player_id", "year"],
                        "filters": [{"field": "player_id", "op": "=", "value": "a"}]})
    assert q(con, sql) == [("a", 2000, 3, 2)]


def test_measure_filter_becomes_having(con, comp):
    sql = comp.compile({"measures": ["home_runs"], "dimensions": ["player_id"],
                        "filters": [{"field": "plate_appearances", "op": ">=", "value": 45}]})
    assert "HAVING" in sql and "WHERE" not in sql
    assert sorted(q(con, sql)) == [("b", 1)]


def test_join_included_only_when_used(con, comp):
    sql = comp.compile({"measures": ["home_runs"], "dimensions": ["player_id"]})
    assert "JOIN" not in sql
    sql = comp.compile({"measures": ["home_runs"], "dimensions": ["franchise_id"],
                        "order_by": [{"field": "franchise_id", "direction": "asc"}]})
    assert "LEFT JOIN lahman.main.Teams AS teams" in sql and "People" not in sql
    assert q(con, sql) == [("FX", 1), ("FY", 3)]


def test_join_on_team_and_year(con, comp):
    # teamID だけで結合すると行が増える。teamID+yearID で結合していれば合計は変わらない
    a = q(con, comp.compile({"measures": ["at_bats"]}))
    b = q(con, comp.compile({"measures": ["at_bats"], "dimensions": ["team_name"]}))
    assert a[0][0] == sum(r[1] for r in b) == 120


def test_people_join_and_name(con, comp):
    sql = comp.compile({"measures": ["hits"], "dimensions": ["player_name"],
                        "filters": [{"field": "birth_country", "op": "in", "value": ["Japan"]}]})
    assert q(con, sql) == [("Bo B", 30)]


def test_backtick_columns_translated(comp):
    sql = comp.compile({"measures": ["doubles", "slugging_pct"]})
    assert '"2B"' in sql and "`" not in sql


def test_team_view_and_franchise(con, comp):
    sql = comp.compile({"measures": ["wins", "win_pct", "world_series_titles"], "dimensions": ["franchise_name"],
                        "order_by": [{"field": "wins", "direction": "desc"}]})
    assert q(con, sql) == [("Franchise X", 11, pytest.approx(11 / 20), 1), ("Franchise Y", 3, pytest.approx(0.3), 0)]


def test_between_and_limit(con, comp):
    sql = comp.compile({"measures": ["runs_per_game"], "dimensions": ["year"],
                        "filters": [{"field": "year", "op": "between", "value": [2000, 2000]}], "limit": 1})
    assert q(con, sql) == [(2000, pytest.approx(80 / 20))]


def test_unknown_measure_is_compile_error(comp):
    with pytest.raises(CompileError, match="未定義の measure"):
        comp.compile({"measures": ["iso"], "dimensions": ["player_id"]})


def test_unknown_dimension_is_compile_error(comp):
    with pytest.raises(CompileError, match="dimension"):
        comp.compile({"measures": ["home_runs"], "dimensions": ["park"]})


def test_measures_across_views_rejected(comp):
    with pytest.raises(CompileError, match="単一の metric view"):
        comp.compile({"measures": ["home_runs", "era"]})


def test_dimension_from_other_view_rejected(comp):
    with pytest.raises(CompileError):
        comp.compile({"measures": ["wins"], "dimensions": ["player_id"]})


def test_unanswerable(comp):
    with pytest.raises(Unanswerable):
        comp.compile({"unanswerable": True, "reason": "ISO が未定義"})


def test_bad_operator_and_unknown_key(comp):
    with pytest.raises(CompileError):
        comp.compile({"measures": ["hits"], "filters": [{"field": "year", "op": "LIKE", "value": "2%"}]})
    with pytest.raises(CompileError, match="未知のキー"):
        comp.compile({"measures": ["hits"], "sql": "SELECT 1"})


def test_string_literal_is_escaped(con, comp):
    sql = comp.compile({"measures": ["hits"], "filters": [{"field": "player_id", "op": "=", "value": "a' OR '1'='1"}]})
    assert q(con, sql) == [(None,)]


def test_order_by_measure_not_selected(con, comp):
    sql = comp.compile({"measures": ["hits"], "dimensions": ["player_id"],
                        "order_by": [{"field": "home_runs", "direction": "desc"}]})
    assert [r[0] for r in q(con, sql)] == ["a", "b"]
