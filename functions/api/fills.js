import { db } from "../_shared/db.js";
import { fail, ok } from "../_shared/respond.js";
import { validateFill } from "../_shared/validate.js";

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
      return fail(400, errors.join("\n"));
    }

    const {
      client_key,
      proposal_id = null,
      symbol,
      side,
      quantity,
      price,
      fee = 0,
      traded_at = null,
    } = body;

    // 通貨は銘柄コードの形から機械的に決まるもの（日本株は .T が付く）で、
    // 保有や損益のような「積み上げて計算する値」ではないので、ここで
    // 決めてよい。
    const currency = symbol.endsWith(".T") ? "JPY" : "USD";

    const sql = db(env);

    const inserted = await sql`
      INSERT INTO fills (proposal_id, symbol, side, quantity, price, currency, fee, traded_at, client_key)
      VALUES (${proposal_id}, ${symbol}, ${side}, ${quantity}, ${price}, ${currency}, ${fee}, ${traded_at}, ${client_key})
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
        FROM fills WHERE client_key = ${client_key}
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
