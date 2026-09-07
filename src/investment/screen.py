"""スクリーニングの判定。外部依存を持たない純粋関数のみ。

条件の出典と採用理由は設計書 15.8 を参照。
"""

from dataclasses import dataclass

from investment.config import ScreenCriteria
from investment.market import Fundamentals


@dataclass(frozen=True)
class ScreenResult:
    passed: bool
    reasons: list[str]  # 通過しなかった理由。通過した場合は空


def evaluate(f: Fundamentals, c: ScreenCriteria) -> ScreenResult:
    """5条件で判定する。取得できなかった項目は通過させない。"""
    reasons: list[str] = []

    def check_max(value: float | None, limit: float, label: str, fmt: str) -> None:
        if value is None:
            reasons.append(f"{label}が取得できません")
        elif value > limit:
            reasons.append(f"{label} {fmt.format(value)} が上限 {fmt.format(limit)} を超えています")

    def check_min(value: float | None, limit: float, label: str, fmt: str) -> None:
        if value is None:
            reasons.append(f"{label}が取得できません")
        elif value < limit:
            reasons.append(f"{label} {fmt.format(value)} が下限 {fmt.format(limit)} を下回ります")

    check_max(f.market_cap, c.max_market_cap, "時価総額", "{:,.0f}円")
    check_min(f.revenue_growth, c.min_revenue_growth, "売上高増減率", "{:.1%}")
    check_min(f.operating_margin, c.min_operating_margin, "営業利益率", "{:.1%}")
    check_min(f.roe, c.min_roe, "ROE", "{:.1%}")
    check_min(f.equity_ratio, c.min_equity_ratio, "自己資本比率", "{:.1%}")

    return ScreenResult(passed=not reasons, reasons=reasons)
