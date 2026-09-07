import { db } from "../../../_shared/db.js";
import { fail, ok } from "../../../_shared/respond.js";

// 見送る理由の長さの上限。長すぎる入力をそのまま保存すると、あとで
// 一覧に表示するときに崩れるため、ここで区切る。
const MAX_REASON_LENGTH = 1000;

// POST /api/proposals/{id}/skip
//
// 「この提案は見送る」を記録するだけの窓口。proposals.outcome を
// 'skipped' に変え、入力されていれば skip_reason も一緒に記録する
// （保有や現金には触らない。そもそも見送りなので、売買は起きていない）。
//
// 「なぜ見送ったか」は、この仕組みが存在する理由そのもの（あとで
// 振り返って学ぶための材料）なので、黙って捨てない。
//
// 既に pending でない提案（既に見送り済み・既に買った）に対して
// 押されたときは、404 ではなく 200 で { changed: false } を返す。
// 二重タップで利用者を不安にさせないため。存在しない id のときだけ 404。
export async function onRequestPost({ params, request, env }) {
  const id = Number(params.id);
  if (!Number.isInteger(id) || id < 1) {
    return fail(404, "指定された提案が見つかりませんでした");
  }

  // 見送る理由は任意。入力欄が空でも見送り自体はできる。
  //
  // 本文が無い・JSONとして壊れているなど、理由の読み取りに失敗しても
  // ここでは reason を null にするだけにとどめ、例外を外へ投げない。
  // 「理由の読み取りが失敗したら見送りという本体まで失敗する」事故
  // （補助的な処理の失敗が本体を巻き添えにする）を避けるため。
  let reason = null;
  try {
    const body = await request.json();
    if (body && typeof body.reason === "string") {
      const trimmed = body.reason.trim();
      reason = trimmed === "" ? null : trimmed;
    }
  } catch {
    reason = null;
  }

  if (reason !== null && reason.length > MAX_REASON_LENGTH) {
    return fail(400, `見送る理由は${MAX_REASON_LENGTH}文字以内で入力してください`);
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
      UPDATE proposals SET outcome = 'skipped', skip_reason = ${reason}
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
