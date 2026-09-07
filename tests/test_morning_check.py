from investment.jobs.morning_check import detect_hits

POSITIONS = [
    {"symbol": "3993.T", "take_profit": 2989.0, "stop_loss": 2254.0},
    {"symbol": "AAPL", "take_profit": 220.0, "stop_loss": 168.0},
]


def test_detects_stop_loss_hit():
    ranges = {"3993.T": (2500.0, 2200.0), "AAPL": (200.0, 190.0)}
    hits = detect_hits(POSITIONS, ranges)
    assert len(hits) == 1
    assert hits[0]["symbol"] == "3993.T"
    assert hits[0]["kind"] == "stop_loss"
    assert hits[0]["estimated_price"] == 2254.0


def test_detects_take_profit_hit():
    ranges = {"3993.T": (2500.0, 2400.0), "AAPL": (225.0, 210.0)}
    hits = detect_hits(POSITIONS, ranges)
    assert len(hits) == 1
    assert hits[0]["symbol"] == "AAPL"
    assert hits[0]["kind"] == "take_profit"


def test_boundary_exactly_at_stop_loss_counts_as_hit():
    ranges = {"3993.T": (2500.0, 2254.0), "AAPL": (200.0, 190.0)}
    hits = detect_hits(POSITIONS, ranges)
    assert [h["symbol"] for h in hits] == ["3993.T"]


def test_boundary_exactly_at_take_profit_counts_as_hit():
    ranges = {"3993.T": (2500.0, 2400.0), "AAPL": (220.0, 210.0)}
    hits = detect_hits(POSITIONS, ranges)
    assert [h["symbol"] for h in hits] == ["AAPL"]


def test_no_hits_returns_empty():
    ranges = {"3993.T": (2500.0, 2400.0), "AAPL": (200.0, 190.0)}
    assert detect_hits(POSITIONS, ranges) == []


def test_missing_range_is_skipped_not_treated_as_no_hit():
    hits = detect_hits(POSITIONS, {"AAPL": (225.0, 210.0)})
    assert [h["symbol"] for h in hits] == ["AAPL"]
