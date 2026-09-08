// 枠（じっくり枠・回転枠）の定義と、資金まわりの設定の写し。
//
// 本当は src/investment/config.py の1箇所だけに置きたいが、Python と
// JavaScript の間で設定を共有する仕組みが無い。データベースにも
// 入っていない値（率は決めごとであって、日々変わるデータではない）
// なので、ここに写しを置く。
//
// **この値は必ず src/investment/config.py の BUCKETS / SETTINGS と
// 同じにすること。** 片方だけを直しても、どちらのプログラムもエラーには
// ならない（動くが数字が食い違う）。率だけでなく、初期資金
// （SETTINGS.initial_capital）のような金額もこの警告の対象。
// 値を変えるときは両方を直すこと。

export const BUCKETS = [
  {
    name: "じっくり",
    slots: 2,
    take_profit_pct: 0.22,
    stop_loss_pct: 0.08,
    max_holding_days: null,
  },
  {
    name: "回転",
    slots: 2,
    take_profit_pct: 0.1,
    stop_loss_pct: 0.05,
    max_holding_days: 10,
  },
];

// 同時に持てる銘柄数の合計。枠の合計から決まるので、別に数字を持たない
// （config.py の MAX_POSITIONS と同じ考え方）。
export const MAX_POSITIONS = BUCKETS.reduce((sum, b) => sum + b.slots, 0);

// config.py の SETTINGS のうち、画面が必要とする3つだけを写す。
// initial_capital も含め、**この値は必ず src/investment/config.py の
// SETTINGS と同じにすること**（ファイル冒頭の警告の対象）。
export const SETTINGS = {
  max_position_pct: 0.25,
  risk_per_trade_pct: 0.02,
  initial_capital: 550000, // src/investment/config.py の Settings.initial_capital と同じ（円）
};

// この枠が損益トントン（勝っても負けてもいない状態）になる勝率。
//
// investment/sizing.py の required_win_rate と同じ式である。
// required_win_rate(entry, take_profit, stop_loss, fee_rate) は
//   gain = (take_profit - entry) / entry - fee_rate
//   loss = (entry - stop_loss) / entry + fee_rate
//   戻り値 = loss / (gain + loss)
// を返すが、entry を軸にした take_profit_pct・stop_loss_pct（買値からの
// 上昇率・下落率）を使うと gain = take_profit_pct、loss = stop_loss_pct に
// なり、entry 自体は消える（日本株の往復手数料 fee_rate は 0 のため、
// fee_rate の項も消える）。そのため、率だけから直接この式で求められる。
//
// 米国株を有効にするときは、この関数も直すこと。米国株には手数料
// （src/investment/config.py の us_fee_rate）があるため、fee_rate の項が
// 消えなくなり、この式は成り立たなくなる。
export function breakevenWinRate(bucket) {
  const { take_profit_pct, stop_loss_pct } = bucket;
  return stop_loss_pct / (take_profit_pct + stop_loss_pct);
}
