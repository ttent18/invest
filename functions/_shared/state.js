import { BUCKETS, MAX_POSITIONS, SETTINGS, breakevenWinRate } from "./config.js";

// GET /api/state が返すJSONを組み立てる。
//
// ここでは一切データベースに触らない。データベースへの問い合わせは
// functions/api/state.js が行い、その結果（行の配列）をこの関数に渡す。
// 分けているのは、組み立ての正しさをデータベース無しでテストできるように
// するため。
//
// Neon（HTTP経由のドライバ）は NUMERIC 型の列を文字列で返す。JSONとして
// 画面に返すときは数値にする。num() はその変換をまとめて行う小さな
// 道具で、null/undefined はそのまま null にする（「まだ分からない」を
// 0 にすり替えない）。

function num(value) {
  return value === null || value === undefined ? null : Number(value);
}

function buildCash(cashRows) {
  const cash = {};
  for (const row of cashRows) {
    cash[row.currency] = num(row.amount);
  }
  return cash;
}

function buildPositions(positionRows) {
  // 1件ずつ独立に組み立てる。ある1件の last_price が無くても、
  // その行の含み損益が null になるだけで、他の行の処理には影響しない。
  return positionRows.map((p) => {
    const quantity = num(p.quantity);
    const avgPrice = num(p.avg_price);
    const lastPrice = num(p.last_price);
    const hasLastPrice = lastPrice !== null;
    return {
      symbol: p.symbol,
      bucket: p.bucket,
      quantity,
      avg_price: avgPrice,
      take_profit: num(p.take_profit),
      stop_loss: num(p.stop_loss),
      opened_at: p.opened_at,
      last_price: lastPrice,
      last_price_at: p.last_price_at ?? null,
      // (last_price - avg_price) × quantity の引き算だけ。Python 側に同じ式は無い。
      // last_price が無ければ「まだ分からない」ので null にする（0 にしない）。
      unrealized_pnl: hasLastPrice ? (lastPrice - avgPrice) * quantity : null,
      unrealized_pct: hasLastPrice ? (lastPrice - avgPrice) / avgPrice : null,
      max_holding_days: BUCKETS.find((b) => b.name === p.bucket)?.max_holding_days ?? null,
    };
  });
}

function slotsUsed(positionRows) {
  // 枠ごとに、いま何銘柄持っているかを数える。
  // 保有に枠の記録が無い場合や知らない枠名の場合は最初の枠に数える
  // （investment/jobs/build_context.py の _slots_used と同じ考え方。
  // 黙って0にすると、枠が空いていないのに空いていると伝えてしまう）。
  const known = new Set(BUCKETS.map((b) => b.name));
  const used = {};
  for (const p of positionRows) {
    const name = known.has(p.bucket) ? p.bucket : BUCKETS[0].name;
    used[name] = (used[name] ?? 0) + 1;
  }
  return used;
}

function buildBuckets(positionRows) {
  const used = slotsUsed(positionRows);
  // 枠ごとの空きを足すと全体の残り枠を超えることがあるので、
  // 全体の残りで頭打ちにする（build_context.py の assemble と同じ考え方）。
  const remaining = Math.max(0, MAX_POSITIONS - positionRows.length);
  return BUCKETS.map((b) => {
    const u = used[b.name] ?? 0;
    return {
      name: b.name,
      take_profit_pct: b.take_profit_pct,
      stop_loss_pct: b.stop_loss_pct,
      max_holding_days: b.max_holding_days,
      slots: b.slots,
      used: u,
      free: Math.min(remaining, Math.max(0, b.slots - u)),
    };
  });
}

const MS_PER_DAY = 24 * 60 * 60 * 1000;
const JST_OFFSET_MS = 9 * 60 * 60 * 1000;

// 日本時間（UTC+9）の「日付」だけを比べて、何日前かを出す。
// 単純に経過ミリ秒を24時間で割ると、朝10:30に出た提案を翌朝9:30に
// 見たときに「まだ24時間経っていない」という理由で 0 と出てしまい、
// 実際には日付をまたいでいるのに「今日の提案」に見えてしまう
// （古い提案に気づけない方向にずれる）。ここでは時刻を無視して、
// 日本時間のカレンダー上の日付がいくつ違うかだけを見る。
function jstDayNumber(date) {
  return Math.floor((date.getTime() + JST_OFFSET_MS) / MS_PER_DAY);
}

function buildProposals(proposalRows, now) {
  const nowDay = jstDayNumber(now);
  return proposalRows.map((p) => {
    const quantity = num(p.quantity);
    const entryPrice = num(p.entry_price);
    const isSell = p.action === "sell";
    return {
      id: num(p.id),
      symbol: p.symbol,
      // 'buy'（買い）か 'sell'（売り）。画面はこれを見て「買った」ではなく
      // 「売った」のカードを出す必要がある。落とすと、売るべき提案が
      // 買いの提案として表示され、利用者が逆の注文を出しかねない。
      action: p.action,
      bucket: p.bucket,
      quantity,
      entry_price: entryPrice,
      take_profit: num(p.take_profit),
      stop_loss: num(p.stop_loss),
      // 表示用の投資額（株数 × 買値）。売りの提案では entry_price は
      // 「売値」ではなく保有時点の買値の転記（migrations/001_initial.sql
      // 参照）なので、株数と掛けても投資額にならない。誤解を招く数字を
      // 出すくらいなら「分からない」を表す null にする。
      cost: isSell ? null : quantity * entryPrice,
      rationale: p.rationale,
      scenario: p.scenario,
      confidence: p.confidence,
      created_at: p.created_at,
      days_old: nowDay - jstDayNumber(new Date(p.created_at)),
    };
  });
}

function buildPerformance(performanceRows) {
  // SQL の集計結果（closed / wins / total_pnl / avg_holding_days）は
  // そのまま使い、勝率をJavaScriptで計算し直さない。損益トントンの勝率
  // だけは SQL で出せないので、枠の率から1箇所（breakevenWinRate）で
  // 計算する。まだ1件も決着していない枠も0件として返す
  // （investment/db.py の select_bucket_performance と同じ考え方）。
  const byName = {};
  for (const row of performanceRows) {
    byName[row.bucket] = row;
  }
  return BUCKETS.map((b) => {
    const row = byName[b.name];
    const closed = row ? num(row.closed) : 0;
    const wins = row ? num(row.wins) : 0;
    return {
      bucket: b.name,
      closed,
      wins,
      win_rate: closed ? wins / closed : null,
      total_pnl: row ? num(row.total_pnl) : 0,
      avg_holding_days: row && row.avg_holding_days !== null ? num(row.avg_holding_days) : null,
      breakeven_win_rate: breakevenWinRate(b),
    };
  });
}

function buildFills(fillRows) {
  return fillRows.map((f) => ({
    id: num(f.id),
    symbol: f.symbol,
    side: f.side,
    quantity: num(f.quantity),
    price: num(f.price),
    recorded_at: f.recorded_at,
    apply_error: f.apply_error ?? null,
  }));
}

// now: Date。generated_at と proposals[].days_old に使う。
// vapidPublicKey: Cloudflare の環境変数 VAPID_PUBLIC_KEY（無ければ null）。
// capital: db.select_capital 相当（現金 JPY + 保有の取得原価）を呼び出し側で計算した値。
// cashRows / positionRows / proposalRows / performanceRows / fillRows は
// それぞれの表を読んだSQLの結果（行の配列）をそのまま渡す。
export function buildState({
  now,
  vapidPublicKey,
  capital,
  cashRows,
  positionRows,
  proposalRows,
  performanceRows,
  fillRows,
}) {
  return {
    generated_at: now.toISOString(),
    is_virtual: true, // いまは仮想資金での検証段階のため、常に true
    vapid_public_key: vapidPublicKey ?? null,
    capital: num(capital),
    cash: buildCash(cashRows),
    constraints: {
      max_position_pct: SETTINGS.max_position_pct,
      risk_per_trade_pct: SETTINGS.risk_per_trade_pct,
      max_positions: MAX_POSITIONS,
    },
    buckets: buildBuckets(positionRows),
    proposals: buildProposals(proposalRows, now),
    positions: buildPositions(positionRows),
    performance: buildPerformance(performanceRows),
    unapplied_fills: buildFills(fillRows),
  };
}
