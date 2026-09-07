from unittest.mock import patch

from investment.config import SCREEN, SETTINGS
from investment.jobs.build_context import build


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
    ):
        ctx = build(conn=None, settings=SETTINGS, criteria=SCREEN, limit=50)

    assert ctx["is_virtual"] is True
    assert ctx["rule_version"] == "v1"
    assert ctx["constraints"]["stop_loss_pct"] == 0.08
    assert ctx["constraints"]["take_profit_pct"] == 0.22
    assert ctx["constraints"]["max_positions"] == 5
    assert len(ctx["candidates"]) == 1
    assert ctx["candidates"][0]["symbol"] == "3993.T"


def test_build_reports_no_room_when_positions_are_full():
    full = [{"symbol": f"{i}.T"} for i in range(5)]
    with (
        patch("investment.jobs.build_context.select_screened", return_value=[]),
        patch("investment.jobs.build_context.select_positions", return_value=full),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 0}),
    ):
        ctx = build(conn=None, settings=SETTINGS, criteria=SCREEN, limit=50)

    assert ctx["can_open_new"] is False
