// 「合言葉を入れた人だ」という印（クッキー）の作り方と確かめ方。
//
// **なぜクッキーにしたか:** 最初は Basic認証（ブラウザが出す小さな窓）で
// 作ったが、**iPhone のホーム画面に追加したアプリでは、その窓が出ない。**
// 401 を返しても、窓の代わりに応答の本文がそのまま画面に描かれるだけで、
// 利用者は合言葉を入れる場所を永久に見つけられない（2026-09-10 に発生）。
// クッキーなら、この仕組み自身が出す普通の画面で合言葉を受け取れる。
//
// **中身が書き換えられないようにする:** クッキーは利用者の端末にあるので、
// 「期限だけ 100 年後に書き換える」ことが誰にでもできてしまう。そこで
// 期限に**署名**（合言葉を鍵にした HMAC）を付け、署名が合わないものは
// 受け付けない。合言葉を変えると署名の鍵も変わるので、**古いクッキーは
// 自動的に無効になる**（合言葉を変えたのに入れたまま、が起きない）。

export const COOKIE_NAME = "invest_session";

// 90日。iPhone のホーム画面のアプリで、3か月に1回入れ直す程度にしたい。
export const MAX_AGE_SECONDS = 90 * 24 * 60 * 60;

const encoder = new TextEncoder();

// 合言葉そのものを鍵にして署名する。別の秘密を増やさないため。
async function hmacKey(secret) {
  return crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
}

function base64url(buffer) {
  let binary = "";
  for (const b of new Uint8Array(buffer)) binary += String.fromCharCode(b);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function signature(secret, payload) {
  const signed = await crypto.subtle.sign("HMAC", await hmacKey(secret), encoder.encode(payload));
  return base64url(signed);
}

// 2つの文字列が同じかを、かかる時間が中身によって変わらない形で比べる。
//
// ふつうの === は最初の1文字が違えばすぐ返る。その時間差を何万回も測ると、
// 署名を1文字ずつ言い当てられる。
export function sameSecret(a, b) {
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  let diff = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    diff |= (left[i] ?? 0) ^ (right[i] ?? 0);
  }
  return diff === 0;
}

// 「この時刻まで有効」という印を作る。形は `期限.署名`。
export async function issueSession(secret, nowMs = Date.now()) {
  const expiresAt = String(Math.floor(nowMs / 1000) + MAX_AGE_SECONDS);
  return `${expiresAt}.${await signature(secret, expiresAt)}`;
}

// 受け取った印が本物で、まだ期限内かを確かめる。
export async function isValidSession(token, secret, nowMs = Date.now()) {
  if (typeof token !== "string") return false;

  const separator = token.indexOf(".");
  if (separator === -1) return false;

  const expiresAt = token.slice(0, separator);
  const given = token.slice(separator + 1);

  // 期限が数字として読めないものは、署名を確かめるまでもなく偽物。
  if (!/^\d+$/.test(expiresAt)) return false;

  // **署名を先に確かめる。** 期限切れかどうかより先に偽物を弾く。
  if (!sameSecret(given, await signature(secret, expiresAt))) return false;

  return Number(expiresAt) * 1000 > nowMs;
}

// ブラウザが送ってきた Cookie の見出しから、この仕組みの印だけを取り出す。
export function readSessionCookie(header) {
  if (typeof header !== "string") return null;

  for (const part of header.split(";")) {
    const piece = part.trim();
    const eq = piece.indexOf("=");
    if (eq === -1) continue;
    if (piece.slice(0, eq) !== COOKIE_NAME) continue;
    return decodeURIComponent(piece.slice(eq + 1));
  }
  return null;
}

// クッキーを発行するときの指定。
//
// - HttpOnly … 画面のJavaScriptから読めなくする（盗まれる経路を1つ減らす）
// - Secure   … HTTPS のときだけ送る
// - SameSite=Lax … 他のサイトから貼られたリンク経由では送らない
//   （勝手に「買った」記録を入れられる経路を塞ぐ）
export function sessionCookieHeader(token, maxAgeSeconds = MAX_AGE_SECONDS) {
  return (
    `${COOKIE_NAME}=${encodeURIComponent(token)}; Path=/; Max-Age=${maxAgeSeconds}; ` +
    "HttpOnly; Secure; SameSite=Lax"
  );
}
