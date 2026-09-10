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
// **なぜ Basic認証をやめたか（2026-09-10）:**
// 最初はブラウザが出す小さな窓（Basic認証）で作った。Safari のタブでは
// 窓が出て通れるが、**iPhone のホーム画面に追加したアプリでは窓が出ない。**
// 401 を返しても、窓の代わりに応答の本文がそのまま画面に描かれるだけで、
// **合言葉を入れる場所が存在しない。** リロードしても同じ 401 に当たり続け、
// 利用者は永久に入れない。しかも通知が届くのはホーム画面のアプリだけなので、
// この仕組みの唯一の安全装置（損切りの知らせ）ごと使えなくなっていた。
// いまは自前の画面で合言葉を受け取り、クッキーで覚える。
//
// **合言葉が設定されていなければ、何も開きません。**
// 「設定を忘れる」が「丸見えになる」ではなく「誰も開けない」に倒しています。
// 忘れたことには自分が開けなくなって気づけますが、
// 丸見えになったことには誰も気づけないためです。

import { loginPage, safeNextPath } from "./_shared/login-page.js";
import {
  isValidSession,
  issueSession,
  readSessionCookie,
  sameSecret,
  sessionCookieHeader,
} from "./_shared/session.js";

export async function onRequest({ request, env, next }) {
  const expected = env.APP_PASSWORD;

  // 合言葉が設定されていない ＝ 誰も開けない（安全側に倒す）
  if (typeof expected !== "string" || expected.length === 0) {
    return unavailable(request);
  }

  const url = new URL(request.url);

  // 合言葉を受け取る窓口。ここだけは合言葉を通る前に呼べる必要がある。
  if (url.pathname === "/api/login") {
    if (request.method !== "POST") {
      // 画面側が「記憶が切れた」ことに気づいてここへ送ってくる。
      // 元いた画面（?next=）に戻せるよう受け取る。
      return loginPage({ next: safeNextPath(url.searchParams.get("next")) });
    }
    return handleLogin(request, expected);
  }

  const token = readSessionCookie(request.headers.get("Cookie"));
  if (await isValidSession(token, expected)) {
    return next();
  }

  // 画面を開こうとしている（人が見る要求）なら、合言葉の画面を出す。
  if (isNavigation(request)) {
    return loginPage({ next: url.pathname + url.search });
  }

  // 画面の中から呼ばれたAPIなど。ここで合言葉の画面（HTML）を返すと、
  // 画面側が JSON を期待しているところに HTML が届いて意味不明な失敗になる。
  // JSON で「入れ直してください」と伝え、画面側が案内を出す。
  return json(401, {
    message: "合言葉の記憶が切れました。画面を開き直して、合言葉を入れ直してください",
    reason: "login_required",
  });
}

async function handleLogin(request, expected) {
  let given = "";
  let nextPath = "/";

  try {
    const contentType = request.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const body = await request.json();
      given = typeof body?.password === "string" ? body.password : "";
      nextPath = safeNextPath(body?.next);
    } else {
      const form = await request.formData();
      given = String(form.get("password") ?? "");
      nextPath = safeNextPath(form.get("next"));
    }
  } catch {
    // 本文が壊れていた。合言葉が違うのと同じ扱いにする
    // （例外の中身を画面に出さない）。
    return loginPage({ next: "/", error: "入力を読み取れませんでした。もう一度お試しください", status: 400 });
  }

  if (!sameSecret(given, expected)) {
    return loginPage({
      next: nextPath,
      error: "合言葉が違います。もう一度入れてください",
      status: 401,
    });
  }

  // 合言葉が合っていた。印（クッキー）を渡して、元いた場所へ戻す。
  //
  // 303 を使うのは、戻り先を「送信のやり直し」ではなく
  // 「普通に開く」にするため（そうしないと、再読み込みのたびに
  // 合言葉をもう一度送ることになる）。
  return new Response(null, {
    status: 303,
    headers: {
      location: nextPath,
      "set-cookie": sessionCookieHeader(await issueSession(expected)),
      "cache-control": "no-store",
    },
  });
}

// 人が画面を開こうとしているのか、画面の中のプログラムが呼んでいるのかを見る。
function isNavigation(request) {
  if (request.headers.get("Sec-Fetch-Mode") === "navigate") return true;
  // Sec-Fetch-Mode を送らないブラウザ向けの控え。
  return (request.headers.get("Accept") || "").includes("text/html");
}

function json(status, data) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

// 合言葉そのものが設定されていないとき。
//
// **正しい合言葉を送られても開けてはいけない。** 未設定を「空文字」と
// 同じに扱うと、空のまま誰でも通れてしまう。
function unavailable(request) {
  const message =
    "この画面は、まだ合言葉が設定されていないため開けません。" +
    "Cloudflare の環境変数に APP_PASSWORD を登録してください。";

  if (isNavigation(request)) {
    return loginPage({ next: "/", error: message, status: 503 });
  }
  return json(503, { message, reason: "not_configured" });
}
