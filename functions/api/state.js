import { BUCKETS } from "../_shared/config.js";
import { db } from "../_shared/db.js";
import { fail, ok } from "../_shared/respond.js";
import { buildState } from "../_shared/state.js";

// 期限（何営業日で降りるか）が決まっている枠の日数。いまは回転枠の10営業日
// だけ。枠ごとの成績で「期限で降りた件数」を数えるSQLに渡す。
// src/investment/config.py の BUCKETS から写した functions/_shared/config.js
// を唯一の出どころにして、SQLの中に 10 と書かない（config.py を直したときに
// ここだけ古い数字が残るのを防ぐ）。
// 期限のある枠が2つ以上になったら、枠ごとに日数が違うのでこの1つの数字では
// 数えられない。そのときはSQLを枠ごとに分けること。
const HOLDING_LIMITS = BUCKETS.map((b) => b.max_holding_days).filter((d) => d !== null);
const EXPIRY_DAYS = HOLDING_LIMITS.length === 1 ? HOLDING_LIMITS[0] : null;

// 提案と保有に出てくる銘柄の会社名を引く。
//
// **会社名は補助の情報。ここが失敗しても /api/state 全体は失敗させない。**
// 会社名が出ないのは不便だが、保有・現金・提案が見えなくなるほうがはるかに
// 困る（この仕組みでは「補助の失敗が本体を巻き添えにする」壊れ方が
// 繰り返し出ている）。失敗したら空の Map を返し、画面には name: null が出る。
//
// 銘柄ごとに一番新しい取得日の行を取る（DISTINCT ON）。
// src/investment/db.py の select_screened は
// 「as_of = (SELECT MAX(as_of) FROM fundamentals)」という全体の最新日で
// 絞っているが、あれは「今週スクリーニングした銘柄の中から選ぶ」ための
// 条件。ここは目的が違い、いま持っている銘柄の名前が欲しい。持っている
// 銘柄が最新の取得日に含まれていない（条件から外れた等）ことはありうるので、
// 全体の最新日ではなく銘柄ごとの最新日を見る。
// db.py に「銘柄ごとの最新の会社名を引く」関数は無いため、揃える相手は無い。
async function fetchNames(sql, symbols) {
  if (symbols.length === 0) return new Map();
  try {
    const rows = await sql`
      SELECT DISTINCT ON (symbol) symbol, name
      FROM fundamentals
      WHERE symbol = ANY(${symbols})
      ORDER BY symbol, as_of DESC
    `;
    const names = new Map();
    for (const row of rows) {
      if (row.name) names.set(row.symbol, row.name);
    }
    return names;
  } catch (err) {
    // エラー本文は画面に返さず、サーバー側のログにだけ残す。
    console.error("会社名の取得に失敗（会社名なしで続行）:", err);
    return new Map();
  }
}

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
//
// **例外は会社名（fundamentals.name）だけ。** 会社名が無くても保有・現金・
// 提案は正しく読める（不便になるだけ）ので、そこだけは失敗しても
// 名前なしで続ける。fetchNames のコメントを参照。
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
      // expired_count は「期限で降りた件数」だが、**新しい列ではなく
      // 保有日数からの導出**である。売った理由はどこにも記録されていない
      // ため、期限のある枠で保有日数が期限以上だった取引を数えている。
      // 利確で降りた日がたまたま10営業日目だった取引も数に入るので、
      // 件数は実際より多め（過大側）に出る。詳しくは
      // functions/_shared/state.js の buildPerformance のコメントを参照。
      sql`
        SELECT bucket,
               COUNT(*)                                     AS closed,
               COUNT(*) FILTER (WHERE realized_pnl > 0)     AS wins,
               COALESCE(SUM(realized_pnl), 0)                AS total_pnl,
               AVG(holding_days)                             AS avg_holding_days,
               COUNT(*) FILTER (WHERE holding_days >= ${EXPIRY_DAYS}) AS expired_count
        FROM trades
        WHERE side = 'sell' AND bucket IS NOT NULL
        GROUP BY bucket
      `,
    ]);

    const capital = Number(capitalRows[0].c);

    // 会社名は本体の問い合わせが終わってから引く（銘柄コードの一覧が
    // 必要なため）。失敗しても fetchNames が空の Map を返すので、
    // ここで /api/state 全体が落ちることはない。
    const symbols = [
      ...new Set([...positionRows, ...proposalRows].map((r) => r.symbol).filter(Boolean)),
    ];
    const namesBySymbol = await fetchNames(sql, symbols);

    const state = buildState({
      now: new Date(),
      vapidPublicKey: env.VAPID_PUBLIC_KEY,
      // 実際のお金を動かし始めたら、Cloudflare の環境変数 IS_VIRTUAL に
      // "false" を入れる。それ以外（未設定を含む）は練習中として扱う。
      isVirtual: env.IS_VIRTUAL !== "false",
      capital,
      namesBySymbol,
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
