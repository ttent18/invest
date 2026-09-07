from unittest.mock import patch

from investment.config import SCREEN, SETTINGS
from investment.jobs.build_context import assemble, attach_last_price, main, read_inputs
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
    ctx = assemble(candidates, [], {"JPY": 550_000}, SETTINGS, SCREEN)

    assert ctx["is_virtual"] is True
    assert ctx["rule_version"] == "v1"
    assert ctx["constraints"]["stop_loss_pct"] == 0.08
    assert ctx["constraints"]["take_profit_pct"] == 0.22
    assert ctx["constraints"]["max_positions"] == 5
    assert len(ctx["candidates"]) == 1
    assert ctx["candidates"][0]["symbol"] == "3993.T"
    assert ctx["candidates"][0]["last_price"] == 3120.0


def test_assemble_reports_no_room_when_positions_are_full():
    full = [{"symbol": f"{i}.T"} for i in range(5)]
    ctx = assemble([], full, {"JPY": 0}, SETTINGS, SCREEN)
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
