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

    # IEEE-754の浮動小数除算は、数学的にちょうど整数になる商でも
    # わずかに下回る値（例: 15.0 のはずが 14.999999999999998）を返すことがある。
    # math.floor をそのまま掛けると、この誤差のせいで1株少なく買ってしまう。
    # 小数9桁で丸めてから切り捨てることで、正当な整数の商はそのまま整数として扱い、
    # 本当に端数がある商（例: 14.9）の切り捨て結果には影響を与えない。
    loss_per_share = entry - stop_loss
    by_risk = math.floor(round(capital * risk_pct / loss_per_share, 9))
    by_cap = math.floor(round(capital * max_position_pct / entry, 9))
    return max(0, min(by_risk, by_cap))
