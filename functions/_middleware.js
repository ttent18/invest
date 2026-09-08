// この画面とAPIを、合言葉を知っている人だけが開けるようにする。
//
// **なぜ必要か:** /api/analyze と /api/fills には、これ以外の守りがありません。
// URLを知られると、他人が GitHub Actions を何度でも起動でき、
// 勝手に「この銘柄を買った」という記録を入れられます。
// 保有・現金・損益も /api/state から全部読まれます。
//
// **なぜこの場所か:** functions/_middleware.js は、このサイトへの
// **すべての要求**（画面のファイルもAPIも）が、中身を返す前に必ず通る場所です。
// ここで止めれば、後ろに何を足しても守り漏れが起きません。
//
// **合言葉が設定されていなければ、何も開きません。**
// 「設定を忘れる」が「丸見えになる」ではなく「誰も開けない」に倒れるようにしています。
// 忘れたことには自分が開けなくなって気づけますが、
// 丸見えになったことには誰も気づけないためです。

const REALM = 'Basic realm="investment", charset="UTF-8"';

export async function onRequest({ request, env, next }) {
  const expected = env.APP_PASSWORD;

  // 合言葉が設定されていない ＝ 誰も開けない（安全側に倒す）
  if (typeof expected !== "string" || expected.length === 0) {
    return deny(
      "この画面は、まだ合言葉が設定されていないため開けません。" +
        "Cloudflare の環境変数に APP_PASSWORD を登録してください。"
    );
  }

  const given = readPassword(request.headers.get("Authorization"));
  if (given === null || !sameSecret(given, expected)) {
    return deny("合言葉が違います。");
  }

  return next();
}

// ブラウザが送ってくる `Authorization: Basic <利用者名:合言葉 を base64 にしたもの>`
// から、合言葉の部分だけを取り出す。取り出せなければ null。
//
// 利用者名は見ません。ブラウザのパスワード欄は「利用者名」と「合言葉」の
// 2つを聞いてきますが、覚えるものは少ないほうがよいので、
// 利用者名は何を入れても通します。
function readPassword(header) {
  if (typeof header !== "string") return null;

  const prefix = "Basic ";
  if (!header.startsWith(prefix)) return null;

  let decoded;
  try {
    // atob は base64 を1文字ずつのバイト列に戻す。
    // 合言葉に日本語などを使えるように、そのバイト列を UTF-8 として読み直す。
    const bytes = Uint8Array.from(atob(header.slice(prefix.length)), (c) =>
      c.charCodeAt(0)
    );
    decoded = new TextDecoder().decode(bytes);
  } catch {
    // 壊れた base64。合言葉が違うのと同じ扱いにする
    return null;
  }

  const separator = decoded.indexOf(":");
  if (separator === -1) return null;
  return decoded.slice(separator + 1);
}

// 2つの合言葉が同じかを、**かかる時間が中身によって変わらない形で**比べる。
//
// ふつうの === は、最初の1文字が違えばすぐ false を返します。
// その「返るまでの時間」を何万回も測ると、合言葉を1文字ずつ言い当てられます。
// 長さも同じ理由で先に漏らさないよう、長さが違う場合も最後まで比べます。
function sameSecret(a, b) {
  const enc = new TextEncoder();
  const left = enc.encode(a);
  const right = enc.encode(b);

  let diff = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    // 範囲外は 0 として扱う。長さが違えば上の diff で既に 0 ではない
    diff |= (left[i] ?? 0) ^ (right[i] ?? 0);
  }
  return diff === 0;
}

// 401 を返すと、ブラウザが合言葉を聞く欄を出す。
//
// **例外の中身や、正しい合言葉に関わることを本文に入れないこと。**
// ここに出したものは、合言葉を知らない人にも全部見えます。
function deny(message) {
  return new Response(JSON.stringify({ message }), {
    status: 401,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "www-authenticate": REALM,
      // 合言葉を聞かれる前の応答を、途中の仕組みに残させない
      "cache-control": "no-store",
    },
  });
}
