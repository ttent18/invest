import { db } from "../../../_shared/db.js";
import { fail, ok } from "../../../_shared/respond.js";

// POST /api/proposals/{id}/skip
//
// 「この提案は見送る」を記録するだけの窓口。proposals.outcome を
// 'skipped' に変える以外、何も書き換えない（保有や現金には触らない。
// そもそも見送りなので、売買は起きていない）。
//
// 既に pending でない提案（既に見送り済み・既に買った）に対して
// 押されたときは、404 ではなく 200 で { changed: false } を返す。
// 二重タップで利用者を不安にさせないため。存在しない id のときだけ 404。
export async function onRequestPost({ params, env }) {
  const id = Number(params.id);
  if (!Number.isInteger(id) || id < 1) {
    return fail(404, "指定された提案が見つかりませんでした");
  }

  try {
    const sql = db(env);

    const rows = await sql`SELECT id, outcome FROM proposals WHERE id = ${id}`;
    if (rows.length === 0) {
      return fail(404, "指定された提案が見つかりませんでした");
    }

    if (rows[0].outcome !== "pending") {
      // 既に決着済み。エラーにせず、そのまま現状を伝える。
      return ok({ changed: false, outcome: rows[0].outcome });
    }

    // pending のものだけを対象にして更新する。SELECT の後に他の操作で
    // 状態が変わっていた場合（同時に2回押された場合など）は0件更新に
    // なるので、そのときも changed: false として返す。
    const updated = await sql`
      UPDATE proposals SET outcome = 'skipped'
      WHERE id = ${id} AND outcome = 'pending'
      RETURNING outcome
    `;

    if (updated.length === 0) {
      // 同時に2回押された場合など。何に変わったのかを読み直して返す。
      const after = await sql`SELECT outcome FROM proposals WHERE id = ${id}`;
      return ok({ changed: false, outcome: after[0]?.outcome ?? null });
    }

    return ok({ changed: true, outcome: updated[0].outcome });
  } catch (err) {
    console.error("POST /api/proposals/{id}/skip に失敗:", err);
    return fail(500, "見送りとして記録できませんでした。もう一度お試しください");
  }
}
