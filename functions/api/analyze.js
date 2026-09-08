import { fail, ok } from "../_shared/respond.js";

// POST /api/analyze
//
// 「いま分析して」ボタンの窓口。GitHub Actions の analyze ワークフローを
// 起動するだけで、分析そのものはここでは一切行わない（GitHub Actions 上の
// Python が行う）。ここが計算やデータベースへの書き込みをしないのは、
// このファイルの役目が「起動の合図を送ること」だけだから。
//
// トークン（env.GITHUB_TOKEN）は Cloudflare の環境変数からだけ取る。
// 画面側からは送られてこないし、画面側にも一切返さない。

const DISPATCH_URL =
  "https://api.github.com/repos/ttent18/invest/actions/workflows/analyze.yml/dispatches";

export async function onRequest({ request, env }) {
  // POST 以外は受け付けない。onRequestPost だけを export すると
  // Cloudflare の側で自動的に405になるが、その場合はここのコードが
  // 一度も呼ばれず、動きをテストできない。ここで明示的に見て、
  // テストできる形にする。
  if (request.method !== "POST") {
    return fail(405, "この操作はできません");
  }

  if (!env.GITHUB_TOKEN) {
    // トークンが無いまま fetch すると GitHub から401が返り、
    // 「トークンの設定を確認してください」という、原因がここに
    // あることが分かりにくいメッセージになる。設定漏れそのものを
    // 名指しして返す。
    return fail(500, "GitHubへの接続の設定がされていません");
  }

  let res;
  try {
    res = await fetch(DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "investment-pwa",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    });
  } catch (err) {
    // fetch そのものが失敗した場合（ネットワーク断・タイムアウトなど）。
    // 例外の中身（接続先やスタックトレース）は画面に返さない。
    console.error("POST /api/analyze の呼び出しに失敗:", err);
    return fail(500, "分析を始められませんでした（GitHubに接続できませんでした）");
  }

  // GitHub は成功時に 204 No Content を返す。200 ではない。
  // res.ok は 200〜299 をまとめて真にするので、204 もこれで拾えるが、
  // 「204であること」を明示的にも見て、判定の意図を残す。
  if (res.status === 204 || res.ok) {
    return ok({ message: "分析を始めました。数分かかります" });
  }

  // GitHub の応答本文はそのまま返さない。トークンの権限に関する話などが
  // 含まれることがあり、画面に出す意味が無いうえ、情報が漏れる。
  if (res.status === 401 || res.status === 403) {
    return fail(502, "GitHubへの接続が拒否されました。トークンの設定を確認してください");
  }
  if (res.status === 404) {
    return fail(502, "分析の設定が見つかりません");
  }
  return fail(502, "分析を始められませんでした");
}
