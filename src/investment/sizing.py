"""必要勝率とポジションサイズの計算。外部依存を持たない純粋関数のみ。"""

import math


def required_win_rate(
    entry: float, take_profit: float, stop_loss: float, fee_rate: float
) -> float:
    """このトレードが損益トントンになる勝率を返す。

    fee_rate は往復の手数料率（0.01 = 1%）。日本株は0.0、米国株は0.01。
    利益が手数料で消える場合は 1.0（達成不能）を返す。
    """
    if not (stop_loss < entry < take_profit):
        raise ValueError("stop_loss < entry < take_profit である必要があります")

    gain = (take_profit - entry) / entry - fee_rate
    loss = (entry - stop_loss) / entry + fee_rate

    if gain <= 0:
        return 1.0
    return loss / (gain + loss)


def position_size(
    capital: float,
    risk_pct: float,
    max_position_pct: float,
    entry: float,
    stop_loss: float,
) -> int:
    """買える株数を返す。

    2つの上限のうち小さいほうを採る。
      1. 1取引の損失許容額 ÷ 1株あたりの想定損失
      2. 1銘柄への投入上限 ÷ 株価
    """
    if entry <= stop_loss:
        raise ValueError("stop_loss は entry より小さい必要があります")

    loss_per_share = entry - stop_loss
    by_risk = math.floor(capital * risk_pct / loss_per_share)
    by_cap = math.floor(capital * max_position_pct / entry)
    return max(0, min(by_risk, by_cap))
