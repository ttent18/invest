"""設定値。設計書の Global Constraints と対応する。

数値を変えるときは、設計書 16章（ルールのバージョン管理）の手順に従い、
理由と出典を rules/ に記録すること。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    total_capital: int          # 総資金（円）
    max_position_pct: float     # 1銘柄に投じる上限（資金に対する比率）
    max_positions: int          # 同時に持てる銘柄数
    risk_per_trade_pct: float   # 1取引で許容する損失（資金に対する比率）
    stop_loss_pct: float        # 損切り幅（買値からの下落率）
    take_profit_pct: float      # 利確幅（買値からの上昇率）
    jp_fee_rate: float          # 日本株の往復手数料率
    us_fee_rate: float          # 米国株の往復手数料率


@dataclass(frozen=True)
class ScreenCriteria:
    max_market_cap: float       # 時価総額の上限（円）
    min_revenue_growth: float   # 売上高増減率の下限
    min_operating_margin: float # 営業利益率の下限
    min_roe: float              # ROE の下限
    min_equity_ratio: float     # 自己資本比率の下限


SETTINGS = Settings(
    total_capital=550_000,
    max_position_pct=0.25,
    max_positions=4,
    risk_per_trade_pct=0.02,
    stop_loss_pct=0.08,
    take_profit_pct=0.22,
    jp_fee_rate=0.0,
    us_fee_rate=0.01,
)

SCREEN = ScreenCriteria(
    max_market_cap=30_000_000_000,
    min_revenue_growth=0.20,
    min_operating_margin=0.10,
    min_roe=0.15,
    min_equity_ratio=0.40,
)
