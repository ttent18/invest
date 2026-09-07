import { db } from "../_shared/db.js";
import { fail, ok } from "../_shared/respond.js";
import { buildState } from "../_shared/state.js";

// GET /api/state
//
// SQLは src/investment/db.py の対応する関数と同じ表・同じ条件にする:
//   select_positions            → positions
//   select_cash                 → cash
//   select_capital              → cash(JPY) の合計 + positions(JPY) の取得原価の合計
//                                  （Python側が同じ接続の中の2本の問い合わせを
//                                  足しているのに合わせ、こちらも1本のSQLの中で
//                                  足す。JavaScript側で2回のHTTP問い合わせの
//                                  結果を後から足すと、その間に別の変更が挟まり
//                                  ずれる窓が広くなるため）
//   select_unapplied_fills      → fills（未反映のみ）
//   select_bucket_performance   → trades（売り・枠ありのみ）を枠ごとに集計
// proposals は Python 側に対応する読み取り関数が無いため、契約が指定する
// 条件（outcome = 'pending'）だけをここに書く。
//
// **この問い合わせのどれか1つでも失敗したら、/api/state 全体を失敗にする。**
// 個々の項目だけを空にして200を返すと、「本当に保有が0件」なのか
// 「保有の問い合わせが失敗しただけ」なのかが画面から区別できず、
// 「持っているのに0件と表示されて買い増してしまう」といった事故につながる。
// 空の配列は「0件でした」という事実であって、「分かりませんでした」の
// 代わりに使ってはいけない。失敗はまとめて fail(500, ...) にする。
export async function onRequestGet({ env }) {
  try {
    const sql = db(env);

    const [
      cashRows,
      capitalRows,
      positionRows,
      proposalRows,
      fillRows,
      performanceRows,
    ] = await Promise.all([
      sql`SELECT currency, amount FROM cash`,
      // src/investment/db.py の select_capital と同じ、現金(JPY)の合計と
      // 保有(JPY)の取得原価の合計を、1本のSQLの中で足す。
      sql`
        SELECT
          (SELECT COALESCE(SUM(amount), 0) FROM cash WHERE currency = 'JPY') +
          (SELECT COALESCE(SUM(quantity * avg_price), 0) FROM positions WHERE currency = 'JPY')
          AS c
      `,
      sql`SELECT * FROM positions ORDER BY symbol`,
      sql`SELECT * FROM proposals WHERE outcome = 'pending' ORDER BY id`,
      sql`SELECT * FROM fills WHERE applied_at IS NULL ORDER BY id`,
      sql`
        SELECT bucket,
               COUNT(*)                                 AS closed,
               COUNT(*) FILTER (WHERE realized_pnl > 0) AS wins,
               COALESCE(SUM(realized_pnl), 0)            AS total_pnl,
               AVG(holding_days)                         AS avg_holding_days
        FROM trades
        WHERE side = 'sell' AND bucket IS NOT NULL
        GROUP BY bucket
      `,
    ]);

    const capital = Number(capitalRows[0].c);

    const state = buildState({
      now: new Date(),
      vapidPublicKey: env.VAPID_PUBLIC_KEY,
      capital,
      cashRows,
      positionRows,
      proposalRows,
      performanceRows,
      fillRows,
    });

    return ok(state);
  } catch (err) {
    // データベースのエラー本文（接続先や表の構造が分かってしまう）は
    // 画面に返さない。サーバー側のログにだけ残す。
    console.error("GET /api/state に失敗:", err);
    return fail(500, "いまの状態を読み込めませんでした");
  }
}
