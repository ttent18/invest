"""設定値。設計書の Global Constraints と対応する。

数値を変えるときは、設計書 16章（ルールのバージョン管理）の手順に従い、
理由と出典を rules/ に記録すること。

## 枠（じっくり / 回転）について

同時に持てる4銘柄を、性格の違う2つの「枠」に分けて運用する。
同じ資金・同じ株数でも、狙う値幅と持つ期間が違う。

  じっくり枠 … 決算の数字を読んで、次の決算までに評価が変わるのを待つ。
               +22%/-8%、想定6週間、期限なし。
  回転枠     … 短い値幅を何度も取りにいく。+10%/-5%、想定2週間、
               10営業日で結論が出なければ勝ち負け関係なく降りる。

**分ける目的は、どちらが自分に向いているかを実測すること。**
同じ資金・同じ枠数で並行して走らせれば、半年もすれば勝率が比較できる。
配分を変えるのはそのデータが出てからにする。
"""

from dataclasses import dataclass


# initial_capital は「最初に入金する額」であって、運用中の総資金ではない。
# 運用中の総資金は db.select_capital() が現金と保有から計算する。
# 以前はここに total_capital という名前で直書きしており、利確して資金が
# 増えても仕組みが気づけなかった（2026-09-07 に判明）。
@dataclass(frozen=True)
class Settings:
    initial_capital: int        # 最初に入金する額（円）。運用中の総資金はDBから計算する
    max_position_pct: float     # 1銘柄に投じる上限（資金に対する比率）
    risk_per_trade_pct: float   # 1取引で許容する損失（資金に対する比率）
    jp_fee_rate: float          # 日本株の往復手数料率
    us_fee_rate: float          # 米国株の往復手数料率


@dataclass(frozen=True)
class Bucket:
    """枠。狙う値幅と持つ期間が違う運用の型。

    max_holding_days は「何営業日で結論が出なければ降りるか」。
    None は期限なし（じっくり枠）。期限を付けるのは、値動きしない銘柄が
    枠に居座って戦力が減るのを防ぐため。4枠しかないので、1件の居座りで
    使える枠が25%減ったまま戻らない。
    """

    name: str
    slots: int                  # この枠で同時に持てる銘柄数
    take_profit_pct: float      # 利確幅（買値からの上昇率）
    stop_loss_pct: float        # 損切り幅（買値からの下落率）
    max_holding_days: int | None  # 期限（営業日）。None は期限なし


@dataclass(frozen=True)
class ScreenCriteria:
    max_market_cap: float       # 時価総額の上限（円）
    min_revenue_growth: float   # 売上高増減率の下限（直近四半期の前年同期比）
    min_operating_margin: float # 営業利益率の下限
    min_roe: float              # ROE の下限
    min_equity_ratio: float     # 自己資本比率の下限


SETTINGS = Settings(
    initial_capital=550_000,
    max_position_pct=0.25,
    risk_per_trade_pct=0.02,
    jp_fee_rate=0.0,
    us_fee_rate=0.01,
)

BUCKETS = (
    Bucket("じっくり", slots=2, take_profit_pct=0.22, stop_loss_pct=0.08, max_holding_days=None),
    Bucket("回転", slots=2, take_profit_pct=0.10, stop_loss_pct=0.05, max_holding_days=10),
)

# 同時に持てる銘柄数の合計。枠の合計から決まるので、別に持たない。
MAX_POSITIONS = sum(b.slots for b in BUCKETS)


def bucket_by_name(name: str) -> Bucket | None:
    """名前から枠を引く。知らない名前なら None（呼び出し側が却下する）。"""
    for b in BUCKETS:
        if b.name == name:
            return b
    return None


SCREEN = ScreenCriteria(
    max_market_cap=30_000_000_000,
    min_revenue_growth=0.20,
    min_operating_margin=0.10,
    min_roe=0.15,
    min_equity_ratio=0.40,
)
