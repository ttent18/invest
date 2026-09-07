from investment.config import SCREEN, SETTINGS


def test_settings_match_spec():
    assert SETTINGS.total_capital == 550_000
    assert SETTINGS.max_position_pct == 0.25
    assert SETTINGS.max_positions == 4
    assert SETTINGS.risk_per_trade_pct == 0.02
    assert SETTINGS.stop_loss_pct == 0.08
    assert SETTINGS.take_profit_pct == 0.22
    assert SETTINGS.jp_fee_rate == 0.0
    assert SETTINGS.us_fee_rate == 0.01


def test_screen_criteria_match_spec():
    assert SCREEN.max_market_cap == 30_000_000_000
    assert SCREEN.min_revenue_growth == 0.20
    assert SCREEN.min_operating_margin == 0.10
    assert SCREEN.min_roe == 0.15
    assert SCREEN.min_equity_ratio == 0.40
