import { db } from "../_shared/db.js";
import { fail, ok } from "../_shared/respond.js";
import { validateSubscription } from "../_shared/validate.js";

// POST /api/subscribe
//
// 通知を送る宛先（ブラウザの Push API が発行する PushSubscription を
// そのまま toJSON() した形）を push_subscriptions に登録する窓口。
// endpoint に UNIQUE 制約があるので、同じ端末から2回登録しても行は
// 増えない。鍵（p256dh / auth）が変わっていた場合はそちらだけ
// 上書きする（ON CONFLICT ... DO UPDATE）。
export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch {
    return fail(400, "送信された内容を読み取れませんでした");
  }

  const errors = validateSubscription(body);
  if (errors.length > 0) {
    return new Response(JSON.stringify({ message: errors.join("\n"), errors }), {
      status: 400,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  }

  const { endpoint, keys } = body;
  // 検証（validateSubscription）は p256dh.trim() / auth.trim() で
  // 「空でないか」を判定している。ここで trim せずに保存すると、
  // 前後に空白が付いたままの鍵が push_subscriptions に入り、
  // 通知の送信（暗号化）が黙って失敗し続ける（Minor 3）。
  const p256dh = keys.p256dh.trim();
  const auth = keys.auth.trim();

  try {
    const sql = db(env);

    await sql`
      INSERT INTO push_subscriptions (endpoint, p256dh, auth)
      VALUES (${endpoint}, ${p256dh}, ${auth})
      ON CONFLICT (endpoint) DO UPDATE
      SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth
    `;

    return ok({ registered: true });
  } catch (err) {
    // データベースのエラー本文（接続先や表の構造が分かってしまう）は
    // 画面に返さない。サーバー側のログにだけ残す。
    console.error("POST /api/subscribe に失敗:", err);
    return fail(500, "通知の登録に失敗しました。もう一度お試しください");
  }
}
