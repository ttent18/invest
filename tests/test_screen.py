from investment.config import SCREEN
from investment.market import Fundamentals
from investment.screen import evaluate


def make(**overrides) -> Fundamentals:
    """5条件をすべて満たす銘柄を作り、必要な項目だけ上書きする。"""
    base = {
        "symbol": "9999.T",
        "name": "テスト株式会社",
        "market_cap": 10_000_000_000,   # 100億円
        "revenue_growth": 0.30,
        "operating_margin": 0.20,
        "roe": 0.25,
        "equity_ratio": 0.60,
    }
    base.update(overrides)
    return Fundamentals(**base)


def test_passes_when_all_criteria_met():
    result = evaluate(make(), SCREEN)
    assert result.passed is True
    assert result.reasons == []


def test_fails_on_market_cap():
    result = evaluate(make(market_cap=50_000_000_000), SCREEN)
    assert result.passed is False
    assert any("時価総額" in r for r in result.reasons)


def test_fails_on_revenue_growth():
    result = evaluate(make(revenue_growth=0.05), SCREEN)
    assert result.passed is False
    assert any("売上高増減率" in r for r in result.reasons)


def test_collects_all_failing_reasons():
    result = evaluate(make(revenue_growth=0.05, roe=0.01), SCREEN)
    assert result.passed is False
    assert len(result.reasons) == 2


def test_boundary_values_pass():
    # 閾値ちょうどは通す
    result = evaluate(
        make(
            market_cap=SCREEN.max_market_cap,
            revenue_growth=SCREEN.min_revenue_growth,
            operating_margin=SCREEN.min_operating_margin,
            roe=SCREEN.min_roe,
            equity_ratio=SCREEN.min_equity_ratio,
        ),
        SCREEN,
    )
    assert result.passed is True


def test_missing_value_fails_and_is_reported():
    # データが取れなかった項目は通さない。取れたことにしない
    result = evaluate(make(roe=None), SCREEN)
    assert result.passed is False
    assert any("ROE" in r and "取得できません" in r for r in result.reasons)


def test_nan_value_fails_and_is_reported():
    # NaN は「取得できた」ことにしてはならない。None と同じ扱いにする
    # （nan < limit も nan > limit も False になるため、素通りする穴がある）
    result = evaluate(make(roe=float("nan")), SCREEN)
    assert result.passed is False
    assert any("ROE" in r and "取得できません" in r for r in result.reasons)
