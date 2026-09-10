// 合言葉を入れてもらう画面。
//
// **なぜ、ふつうの画面として自分で返すのか:** ブラウザが出す小さな窓
// （Basic認証）に頼ると、iPhone のホーム画面に追加したアプリでは
// **窓が出ず、合言葉を入れる場所が存在しない**（2026-09-10 に発生）。
// この画面なら、ホーム画面のアプリの中に普通に表示できる。
//
// **web/app.css は使わない。** この画面は合言葉を通る前に出るので、
// 別ファイルの読み込みも門番に止められる。見た目の指定はこの中に全部書く。

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// 合言葉を通ったあとに戻る場所。
//
// **他所のサイトへ飛ばされないようにする。** `next` は利用者が
// 書き換えられる値なので、`/` で始まり `//` で始まらないものだけ受け付ける
// （`//example.com` はブラウザが「別のサイト」と解釈する）。
export function safeNextPath(value) {
  if (typeof value !== "string") return "/";
  if (!value.startsWith("/")) return "/";
  if (value.startsWith("//")) return "/";
  return value;
}

export function loginPage({ next = "/", error = null, status = 200 } = {}) {
  const body = `<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="/icon-180.png">
<title>合言葉を入れてください</title>
<style>
  :root {
    --bg: #0f1115; --surface: #171a21; --border: #2b303b;
    --text: #eef0f3; --muted: #97a0ad; --accent: #4f8cff;
    --danger: #ff5252; --danger-bg: #3a1414; --danger-border: #6b1f1f;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text); font-size: 16px; line-height: 1.55;
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans",
                 "Hiragino Kaku Gothic ProN", sans-serif;
    -webkit-text-size-adjust: 100%;
  }
  main {
    max-width: 480px; margin: 0 auto;
    padding: calc(32px + env(safe-area-inset-top)) 16px calc(24px + env(safe-area-inset-bottom));
  }
  h1 { font-size: 20px; margin: 0 0 8px; }
  p.lead { color: var(--muted); margin: 0 0 20px; font-size: 15px; }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px;
  }
  label { display: block; font-size: 14px; color: var(--muted); margin-bottom: 6px; }
  input[type="password"], input[type="text"] {
    width: 100%; padding: 12px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    /* iOS は16px未満だと入力時に画面を勝手に拡大するので、16pxを下回らせない */
    font-size: 16px; min-height: 48px;
  }
  button {
    width: 100%; margin-top: 16px; min-height: 48px;
    border: none; border-radius: 8px; background: var(--accent); color: #fff;
    font-size: 16px; font-weight: 600;
  }
  .error {
    background: var(--danger-bg); border: 1px solid var(--danger-border);
    color: var(--danger); border-radius: 8px; padding: 12px; margin-bottom: 16px;
    font-size: 15px;
  }
  .note { color: var(--muted); font-size: 13px; margin-top: 20px; }
</style>
</head>
<body>
<main>
  <h1>合言葉を入れてください</h1>
  <p class="lead">この画面はあなただけのものです。合言葉を知らない人は、保有も損益も見られません。</p>
  ${error ? `<div class="error">${escapeHtml(error)}</div>` : ""}
  <form method="POST" action="/api/login" class="card">
    <input type="hidden" name="next" value="${escapeHtml(next)}">
    <!-- パスワード管理アプリに覚えてもらうための欄。中身は見ていない。 -->
    <label for="user">名前（何でもよい。空でも通ります）</label>
    <input type="text" id="user" name="user" autocomplete="username" value="invest">
    <label for="password" style="margin-top:16px">合言葉</label>
    <input type="password" id="password" name="password" autocomplete="current-password"
           autocapitalize="off" autocorrect="off" spellcheck="false" required>
    <button type="submit">開く</button>
  </form>
  <p class="note">
    一度入れると、この端末では90日ほど覚えています。<br>
    合言葉を変えたときは、もう一度この画面が出ます。
  </p>
</main>
</body>
</html>`;

  return new Response(body, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      // 合言葉を入れる前の画面を、途中の仕組みに残させない
      "cache-control": "no-store",
    },
  });
}
