from pathlib import Path
from unittest.mock import patch

from investment.config import SCREEN, SETTINGS
from investment.jobs.build_context import (
    RULE_VERSION,
    assemble,
    attach_last_price,
    drop_unaffordable,
    main,
    read_inputs,
)
from investment.market import MarketDataError


def test_assemble_includes_candidates_and_constraints():
    candidates = [
        {
            "symbol": "3993.T",
            "name": "PKSHA",
            "market_cap": 9_800_000_000,
            "revenue_growth": 0.81,
            "operating_margin": 0.14,
            "roe": 0.20,
            "equity_ratio": 0.63,
            "last_price": 3120.0,
        }
    ]
    ctx = assemble(candidates, [], {"JPY": 550_000}, SETTINGS, SCREEN, dropped=[])

    assert ctx["is_virtual"] is True
    assert ctx["rule_version"] == "v2"
    assert ctx["constraints"]["stop_loss_pct"] == 0.08
    assert ctx["constraints"]["take_profit_pct"] == 0.22
    assert ctx["constraints"]["max_positions"] == 4
    assert len(ctx["candidates"]) == 1
    assert ctx["candidates"][0]["symbol"] == "3993.T"
    assert ctx["candidates"][0]["last_price"] == 3120.0


def test_assemble_reports_no_room_when_positions_are_full():
    full = [{"symbol": f"{i}.T"} for i in range(5)]
    ctx = assemble([], full, {"JPY": 0}, SETTINGS, SCREEN, dropped=[])
    assert ctx["can_open_new"] is False


def test_read_inputs_reads_everything_the_database_holds():
    """株価を取りに行く前に、データベースから読むものを読み切ること。"""
    with (
        patch("investment.jobs.build_context.select_screened", return_value=[{"symbol": "1111.T"}]),
        patch("investment.jobs.build_context.select_positions", return_value=[{"symbol": "9.T"}]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
    ):
        candidates, positions, cash = read_inputs(conn=None, criteria=SCREEN, limit=50)

    assert candidates == [{"symbol": "1111.T"}]
    assert positions == [{"symbol": "9.T"}]
    assert cash == {"JPY": 550_000}


def test_attach_last_price_never_touches_the_database():
    """株価の取得中はデータベースに触れないこと。

    データベースの接続は数分放置されると切られる。株価の取得には
    候補の件数だけ時間がかかるため、この間に接続を触る設計にしていると、
    候補を増やしたときに切断されてクラッシュする（run_screen で実際に起きた）。
    """

    def boom(*args, **kwargs):
        raise AssertionError("株価の取得中にデータベースへアクセスしてはいけない")

    def fake_fetch(symbol):
        if symbol == "2222.T":
            raise MarketDataError("2222.T の株価取得に失敗しました")
        return 1500.0

    candidates = [
        {"symbol": "1111.T", "name": "取れる会社"},
        {"symbol": "2222.T", "name": "株価が取れない会社"},
    ]

    with (
        patch("investment.jobs.build_context.fetch_last_price", side_effect=fake_fetch),
        patch("investment.jobs.build_context.connect", side_effect=boom),
        patch("investment.jobs.build_context.record_gaps", side_effect=boom),
    ):
        priced, gaps = attach_last_price(candidates)

    # 株価が取れなかった 2222.T は候補から除外される
    assert [c["symbol"] for c in priced] == ["1111.T"]
    assert priced[0]["last_price"] == 1500.0

    # 除外した事実は呼び出し側に返され、あとでまとめて記録される
    assert len(gaps) == 1
    assert gaps[0][0] == "price:2222.T"
    assert "2222.T" in gaps[0][1]


def test_attach_last_price_prints_exclusion_message_to_stdout(capsys):
    with patch(
        "investment.jobs.build_context.fetch_last_price",
        side_effect=MarketDataError("失敗"),
    ):
        priced, gaps = attach_last_price([{"symbol": "2222.T", "name": "株価が取れない会社"}])

    assert priced == []
    assert len(gaps) == 1
    assert "1 件を候補から除外しました" in capsys.readouterr().out


def test_main_closes_the_connection_before_fetching_prices(tmp_path):
    """接続を開くのは、株価の取得の前後に分かれること。

    「読む → (接続を閉じて)株価を取る → 開き直して書く」の順序そのものを検証する。
    """
    calls: list[str] = []

    class FakeConn:
        def __enter__(self):
            calls.append("connect")
            return self

        def __exit__(self, *exc):
            calls.append("close")
            return False

    def fake_fetch(symbol):
        calls.append(f"fetch:{symbol}")
        raise MarketDataError("株価が取れません")

    with (
        patch("investment.jobs.build_context.connect", side_effect=lambda: FakeConn()),
        patch("investment.jobs.build_context.select_screened", return_value=[{"symbol": "1111.T"}]),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.fetch_last_price", side_effect=fake_fetch),
        patch("investment.jobs.build_context.record_gaps") as record,
        patch("investment.jobs.build_context.OUTPUT", tmp_path / "context.json"),
    ):
        assert main() == 0

    assert calls == ["connect", "close", "fetch:1111.T", "connect", "close"]
    record.assert_called_once()
    assert record.call_args.args[1][0][0] == "price:1111.T"


def test_main_does_not_reopen_the_connection_when_nothing_failed(tmp_path):
    """株価が全件取れた日は、書き込みのために接続を開き直さないこと。"""
    calls: list[str] = []

    class FakeConn:
        def __enter__(self):
            calls.append("connect")
            return self

        def __exit__(self, *exc):
            return False

    with (
        patch("investment.jobs.build_context.connect", side_effect=lambda: FakeConn()),
        patch("investment.jobs.build_context.select_screened", return_value=[{"symbol": "1111.T"}]),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.fetch_last_price", return_value=1500.0),
        patch("investment.jobs.build_context.record_gaps") as record,
        patch("investment.jobs.build_context.OUTPUT", tmp_path / "context.json"),
    ):
        assert main() == 0

    assert calls == ["connect"]
    record.assert_not_called()


# --- 100株単位で買えない銘柄を候補から外す ----------------------------------


def test_drop_unaffordable_removes_stocks_whose_one_lot_exceeds_the_limit():
    """1単元（日本株なら100株）で1銘柄の上限金額を超える銘柄は候補から外す。

    株価2,704円だと100株で270,400円になり、上限137,500円を超えるので買えない。
    AIに渡しても発注できない提案しか作れず、調べる手間が無駄になる。
    2026-09-07 の初回運用で実際に起きた（AIが41件中5件しか調べられなかった）。
    """
    candidates = [
        {"symbol": "1111.T", "last_price": 1200.0},   # 100株 = 120,000円 → 買える
        {"symbol": "2222.T", "last_price": 2704.0},   # 100株 = 270,400円 → 買えない
        {"symbol": "3333.T", "last_price": 1375.0},   # 100株 = 137,500円 ちょうど → 買える
    ]
    kept, dropped = drop_unaffordable(candidates, SETTINGS)

    assert [c["symbol"] for c in kept] == ["1111.T", "3333.T"]
    assert [d["symbol"] for d in dropped] == ["2222.T"]


def test_drop_unaffordable_keeps_us_stocks_that_fit_in_one_share():
    """米国株は1株から買えるので、株価が上限内なら残る。"""
    kept, dropped = drop_unaffordable([{"symbol": "AAPL", "last_price": 200.0}], SETTINGS)
    assert [c["symbol"] for c in kept] == ["AAPL"]
    assert dropped == []


def test_assemble_records_what_it_dropped_and_why():
    """外した銘柄を黙って消さず、件数と理由をコンテキストに残すこと。

    AIが「なぜこの銘柄が候補にないのか」を追えるようにするため。
    """
    dropped = [
        {"symbol": "2222.T", "last_price": 2704.0, "lot_size": 100, "lot_cost": 270400.0}
    ]
    ctx = assemble([], [], {"JPY": 550_000}, SETTINGS, SCREEN, dropped=dropped)

    assert ctx["excluded_candidates"]["count"] == 1
    assert "2222.T" in str(ctx["excluded_candidates"]["symbols"])
    assert "単元" in ctx["excluded_candidates"]["reason"]


def test_kept_candidates_carry_the_lot_size_and_the_buyable_quantity():
    """各候補が何株単位で、最大何株まで買えるかをAIに渡すこと。

    AIが「上限金額 ÷ 株価」を自分で計算すると単元未満の株数になり、発注できない。
    正しい答えをこちらで計算して渡す。
    株価1,200円・上限137,500円なら114株買えるが、100株単位なので100株。
    """
    kept, _ = drop_unaffordable([{"symbol": "1111.T", "last_price": 1200.0}], SETTINGS)
    assert kept[0]["lot_size"] == 100
    assert kept[0]["max_quantity"] == 100


def test_the_rule_version_has_a_matching_rules_document():
    """RULE_VERSION を上げたら rules/<版>.md が存在すること。

    版を上げただけで理由を書き忘れると、あとから「なぜ変えたか」が
    たどれなくなる（設計書16章が要求している記録そのものが消える）。
    """
    doc = Path(__file__).resolve().parents[1] / "rules" / f"{RULE_VERSION}.md"
    assert doc.exists(), f"{doc} がありません。ルールを変えたら理由を書くこと"
