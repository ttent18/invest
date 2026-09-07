import { db } from "../_shared/db.js";
import { fail, ok } from "../_shared/respond.js";
import { isJapaneseStock, validateFill } from "../_shared/validate.js";

// POST /api/fills
//
// 「買った」「売った」を1行、fills 表に記録するだけの窓口。
// 保有株数・平均取得単価・現金の増減はここでは一切計算しない
// （それは Python 側の仕事）。ここで計算してしまうと、同じ計算が
// 2箇所に存在することになり、片方だけ直し忘れる事故が起きる。
//
// 二重送信対策: client_key に UNIQUE 制約があるので、同じ鍵で2回来ても
// 2行目は作らない（ON CONFLICT ... DO NOTHING）。作られなかった場合は
// 既にある行を読んで返す。エラーにはしない
// （ボタンを2回押しただけで利用者を不安にさせないため）。
export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch {
    return fail(400, "送信された内容を読み取れませんでした");
  }

  try {
    const errors = validateFill(body);
    if (errors.length > 0) {
      // message は1本の文字列（今までどおり、改行区切りでそのまま
      // 表示しても読める）、errors は理由ごとに分かれた配列。画面側の
      // 実装状況に合わせてどちらでも使えるようにする。
      return new Response(JSON.stringify({ message: errors.join("\n"), errors }), {
        status: 400,
        headers: { "content-type": "application/json; charset=utf-8" },
      });
    }

    const {
      client_key,
      proposal_id = null,
      symbol,
      side,
      quantity,
      price,
      fee = 0,
      traded_at: tradedAtInput,
    } = body;

    // 空文字（画面の日時欄を空のまま送った場合）を「省略」と同じ扱いに
    // そろえる。ここで正規化しないと "" がそのまま INSERT に渡り、
    // データベース側で日時として読めずに失敗する（validate.js は ""
    // を「省略」として通しているので、ここで受け止める必要がある）。
    const traded_at = tradedAtInput || null;

    // 二重送信よけの鍵は前後の空白を取り除いてから使う。取り除かずに
    // 使うと、"k1" と "k1 " が別の鍵として扱われ、二重送信よけが
    // すり抜けてしまう。
    const normalizedClientKey = client_key.trim();

    // 通貨は銘柄コードの形から機械的に決まるもの（日本株は .T が付く）で、
    // 保有や損益のような「積み上げて計算する値」ではないので、ここで
    // 決めてよい。日本株かどうかの判定は validate.js の
    // isJapaneseStock と必ず同じ規則を使う（大文字・小文字の扱いが
    // ずれると、100株単位の検査は通ったのに通貨だけ違う、という
    // ことが起きるため）。
    const currency = isJapaneseStock(symbol) ? "JPY" : "USD";

    const sql = db(env);

    // どの提案に対する売買かを指定された場合は、その提案が実在するかを
    // 先に確認する。存在しない id のまま INSERT すると外部キー制約で
    // 失敗して原因の分からない500になり、利用者は「もう一度」と
    // 言われても直しようがない。ここで確認して、直せる誤りとして
    // 400で返す。
    if (proposal_id !== null) {
      const proposal = await sql`SELECT id FROM proposals WHERE id = ${proposal_id}`;
      if (proposal.length === 0) {
        return fail(400, "その提案は見つかりません");
      }
    }

    const inserted = await sql`
      INSERT INTO fills (proposal_id, symbol, side, quantity, price, currency, fee, traded_at, client_key)
      VALUES (${proposal_id}, ${symbol}, ${side}, ${quantity}, ${price}, ${currency}, ${fee}, ${traded_at}, ${normalizedClientKey})
      ON CONFLICT (client_key) DO NOTHING
      RETURNING id, symbol, side, quantity, price, fee, traded_at, recorded_at
    `;

    let row = inserted[0];
    let created = true;

    if (!row) {
      // client_key が重複していたため、この送信では何も作られなかった。
      // 既にある行を読んで、そちらを返す。
      const existing = await sql`
        SELECT id, symbol, side, quantity, price, fee, traded_at, recorded_at
        FROM fills WHERE client_key = ${normalizedClientKey}
      `;
      if (existing.length === 0) {
        // 通常は起こらない想定（挿入もされず、既存の行も見つからない）。
        // 起きた場合は原因が分からないため、エラーとして返す。
        return fail(500, "記録できませんでした。もう一度お試しください");
      }
      row = existing[0];
      created = false;
    }

    return ok({
      created,
      fill: {
        id: Number(row.id),
        symbol: row.symbol,
        side: row.side,
        quantity: Number(row.quantity),
        price: Number(row.price),
        fee: Number(row.fee),
        traded_at: row.traded_at,
        recorded_at: row.recorded_at,
      },
    });
  } catch (err) {
    // データベースのエラー本文（接続先や表の構造が分かってしまう）は
    // 画面に返さない。サーバー側のログにだけ残す。
    console.error("POST /api/fills に失敗:", err);
    return fail(500, "記録できませんでした。もう一度お試しください");
  }
}
