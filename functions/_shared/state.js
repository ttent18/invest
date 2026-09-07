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

// jstDayNumber が返す「日本時間の日番号」から、その日の曜日
// (0=日, 6=土) を出す。day * MS_PER_DAY - JST_OFFSET_MS は、その
// 日本時間の午前0時ちょうどのUTCミリ秒になる。その瞬間の getUTCDay()
// は、日本時間のカレンダー上のその日の曜日と一致する。
function jstWeekday(dayNumber) {
  return new Date(dayNumber * MS_PER_DAY - JST_OFFSET_MS).getUTCDay();
}

// start から end（ともに jstDayNumber）までの営業日数を数える
// （土日を除く。祝日は考慮しない）。
//
// **src/investment/jobs/morning_check.py の _business_days_between と
// 完全に同じ数え方にすること。** 同じ式が2箇所にあると、片方だけ
// 直したときに気づけない。start の翌日から数え始め、end を含む
// （while d < end: d += 1日; 平日なら +1）。
function businessDaysBetween(startDayNumber, endDayNumber) {
  let days = 0;
  for (let day = startDayNumber + 1; day <= endDayNumber; day++) {
    const dow = jstWeekday(day);
    if (dow !== 0 && dow !== 6) days++; // 月〜金
  }
  return days;
}

function buildPositions(positionRows, now) {
  const todayDay = jstDayNumber(now);
  // 1件ずつ独立に組み立てる。ある1件の last_price が無くても、
  // その行の含み損益が null になるだけで、他の行の処理には影響しない。
  return positionRows.map((p) => {
    const quantity = num(p.quantity);
    const avgPrice = num(p.avg_price);
    const lastPrice = num(p.last_price);
    const hasLastPrice = lastPrice !== null;
    const maxHoldingDays = BUCKETS.find((b) => b.name === p.bucket)?.max_holding_days ?? null;

    // 買った日から今日までの営業日数（土日を除く。祝日は考慮しない）。
    // opened_at が読めない場合は「分からない」として null にする。
    const openedDate = p.opened_at ? new Date(p.opened_at) : null;
    const hasOpenedDate = openedDate !== null && !Number.isNaN(openedDate.getTime());
    const businessDaysHeld = hasOpenedDate
      ? businessDaysBetween(jstDayNumber(openedDate), todayDay)
      : null;

    // 回転枠だけ期限までの残り営業日数を出す。じっくり枠には期限が無いので null。
    // ちょうど max_holding_days たった日に 0 になる（morning_check.py の
    // find_expired が elapsed >= max_holding_days で期限切れと判定するのと
    // 同じ境目。> にすると1日ずれる）。期限を過ぎればさらに負の値になる。
    const daysLeft =
      maxHoldingDays !== null && businessDaysHeld !== null ? maxHoldingDays - businessDaysHeld : null;

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
      max_holding_days: maxHoldingDays,
      business_days_held: businessDaysHeld,
      days_left: daysLeft,
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
  const capitalNum = num(capital);
  return {
    generated_at: now.toISOString(),
    is_virtual: true, // いまは仮想資金での検証段階のため、常に true
    vapid_public_key: vapidPublicKey ?? null,
    capital: capitalNum,
    // 最初に入金した額（円）。src/investment/config.py の
    // SETTINGS.initial_capital の写し（functions/_shared/config.js 参照）。
    initial_capital: SETTINGS.initial_capital,
    // 初期資金からいくら増えた（減った）か。capital - initial_capital の
    // 引き算だけ。capital が分からなければ null（0 にしない）。
    profit_since_start: capitalNum === null ? null : capitalNum - SETTINGS.initial_capital,
    cash: buildCash(cashRows),
    constraints: {
      max_position_pct: SETTINGS.max_position_pct,
      risk_per_trade_pct: SETTINGS.risk_per_trade_pct,
      max_positions: MAX_POSITIONS,
    },
    buckets: buildBuckets(positionRows),
    proposals: buildProposals(proposalRows, now),
    positions: buildPositions(positionRows, now),
    performance: buildPerformance(performanceRows),
    unapplied_fills: buildFills(fillRows),
  };
}
