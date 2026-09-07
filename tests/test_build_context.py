from unittest.mock import patch

from investment.config import SCREEN, SETTINGS
from investment.jobs.build_context import build
from investment.market import MarketDataError


def test_build_includes_candidates_and_constraints():
    candidates = [
        {
            "symbol": "3993.T",
            "name": "PKSHA",
            "market_cap": 9_800_000_000,
            "revenue_growth": 0.81,
            "operating_margin": 0.14,
            "roe": 0.20,
            "equity_ratio": 0.63,
        }
    ]
    with (
        patch("investment.jobs.build_context.select_screened", return_value=candidates),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.fetch_last_price", return_value=3120.0),
    ):
        ctx = build(conn=None, settings=SETTINGS, criteria=SCREEN, limit=50)

    assert ctx["is_virtual"] is True
    assert ctx["rule_version"] == "v1"
    assert ctx["constraints"]["stop_loss_pct"] == 0.08
    assert ctx["constraints"]["take_profit_pct"] == 0.22
    assert ctx["constraints"]["max_positions"] == 5
    assert len(ctx["candidates"]) == 1
    assert ctx["candidates"][0]["symbol"] == "3993.T"
    assert ctx["candidates"][0]["last_price"] == 3120.0


def test_build_reports_no_room_when_positions_are_full():
    full = [{"symbol": f"{i}.T"} for i in range(5)]
    with (
        patch("investment.jobs.build_context.select_screened", return_value=[]),
        patch("investment.jobs.build_context.select_positions", return_value=full),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 0}),
    ):
        ctx = build(conn=None, settings=SETTINGS, criteria=SCREEN, limit=50)

    assert ctx["can_open_new"] is False


def test_build_excludes_candidates_without_price_and_records_gap():
    candidates = [
        {"symbol": "1111.T", "name": "取れる会社"},
        {"symbol": "2222.T", "name": "株価が取れない会社"},
    ]

    def fake_fetch(symbol):
        if symbol == "2222.T":
            raise MarketDataError("2222.T の株価取得に失敗しました")
        return 1500.0

    with (
        patch("investment.jobs.build_context.select_screened", return_value=candidates),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.fetch_last_price", side_effect=fake_fetch),
        patch("investment.jobs.build_context.record_gap") as mock_record_gap,
    ):
        ctx = build(conn=None, settings=SETTINGS, criteria=SCREEN, limit=50)

    # 株価が取れなかった 2222.T は候補から除外される
    assert [c["symbol"] for c in ctx["candidates"]] == ["1111.T"]
    assert ctx["candidates"][0]["last_price"] == 1500.0

    # 除外した事実が record_gap に記録される
    mock_record_gap.assert_called_once()
    args, kwargs = mock_record_gap.call_args
    assert args[0] is None  # conn
    assert kwargs["scope"] == "price:2222.T"
    assert "2222.T" in kwargs["detail"]


def test_build_prints_exclusion_message_to_stdout(capsys):
    candidates = [{"symbol": "2222.T", "name": "株価が取れない会社"}]
    with (
        patch("investment.jobs.build_context.select_screened", return_value=candidates),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={}),
        patch(
            "investment.jobs.build_context.fetch_last_price",
            side_effect=MarketDataError("失敗"),
        ),
        patch("investment.jobs.build_context.record_gap"),
    ):
        build(conn=None, settings=SETTINGS, criteria=SCREEN, limit=50)

    captured = capsys.readouterr()
    assert "1 件を候補から除外しました" in captured.out
