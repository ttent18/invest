// 3つの画面（今日やること／保有／収支）で共通に使う道具。
//
// ここに置くのは:
//   - /api/state を取ってくる関数（loadState）
//   - サーバーに記録を送る関数（postFill / postSkip / postAnalyze）
//   - 金額・パーセント・日付の表示の整形（yen / pct / whenText）
//   - 画面上部の共通の帯・行き来（banner / renderNav）
//
// **画面側（index.html など）はここで計算しない。** /api/state が返した
// 値をそのまま渡して表示するだけにする。含み損益や枠の空きのような
// 「積み上げて計算する値」はサーバー側（functions/_shared/state.js）で
// 計算済みなので、ここで計算し直すと二重に計算する箇所ができてしまい、
// 片方だけ直し忘れる事故につながる。
//
// フレームワークは使わない。CDNからの読み込みも無し（このファイル1つで
// 完結する）。

// ---------------------------------------------------------------------------
// 状態の取得
// ---------------------------------------------------------------------------

// /api/state を取ってくる。
//
// 失敗した場合（通信できない／サーバーがエラーを返した／JSONとして
// 読めない）は、利用者にそのまま見せてよい日本語のメッセージを持った
// Error を投げる。呼び出し側は try/catch して banner() で表示すること。
//
// サーバーのエラー本文（例外のスタックトレースなど）はここでは一切
// 画面に渡さない。/api/state が返す { message: "..." } の message だけを使う。
export async function loadState() {
  let res;
  try {
    res = await fetch("/api/state");
  } catch {
    throw new Error("いまの状態を読み込めませんでした。電波を確かめてもう一度お試しください");
  }

  let body = null;
  try {
    body = await res.json();
  } catch {
    // JSONとして読めない場合は下の !res.ok / 汎用メッセージに任せる。
  }

  if (!res.ok) {
    const message = body && typeof body.message === "string" ? body.message : "いまの状態を読み込めませんでした";
    throw new Error(message);
  }

  if (!body) {
    throw new Error("いまの状態を読み込めませんでした");
  }

  return body;
}

// ---------------------------------------------------------------------------
// サーバーへの送信
// ---------------------------------------------------------------------------

// JSON を POST して、利用者に見せてよい形の結果を返す（例外は投げない）。
// 呼び出し側が network / server / ok の3種類を見分けられるようにする。
async function postJson(url, body) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return {
      ok: false,
      kind: "network",
      message: "送れませんでした。電波を確かめてもう一度押してください",
    };
  }

  let data = null;
  try {
    data = await res.json();
  } catch {
    // 本文が無い／JSONでない。下のメッセージに任せる。
  }

  if (!res.ok) {
    // サーバーが日本語で返した理由（検証エラーなど）はそのまま出す。
    const message = data && typeof data.message === "string" ? data.message : "送れませんでした。電波を確かめてもう一度押してください";
    return { ok: false, kind: "server", message };
  }

  return { ok: true, data };
}

// 二重送信よけの鍵を新しく作る。
export function newClientKey() {
  return crypto.randomUUID();
}

// POST /api/fills — 「買った」「売った」を記録する。
//
// body には client_key 以外の項目（symbol, side, quantity, price, fee,
// traded_at, proposal_id）を渡す。client_key を渡さなかった場合はここで
// 新しく作る。**送信が失敗して利用者がもう一度押すときは、直前に使った
// client_key（この関数が返す result.client_key）を次の呼び出しの body に
// 渡して、同じ鍵を再利用すること。** そうしないと、サーバーには届いて
// いたが応答だけが届かなかった場合に、二重に記録される。
//
// 戻り値:
//   { ok: true, created: boolean, fill: {...}, client_key }
//     created が false のときは「既にこの鍵で記録済みだった」ことを表す
//     （エラーではない。利用者には「記録済みです」とだけ伝えて閉じる）。
//   { ok: false, kind: "network" | "server", message, client_key }
export async function postFill(body) {
  const client_key = body.client_key || newClientKey();
  const payload = { ...body, client_key };
  const result = await postJson("/api/fills", payload);
  if (!result.ok) {
    return { ok: false, kind: result.kind, message: result.message, client_key };
  }
  return { ok: true, created: result.data.created, fill: result.data.fill, client_key };
}

// POST /api/proposals/{id}/skip — 「見送る」を記録する。
//
// 理由（reason）は functions/api/proposals/[id]/skip.js が
// proposals.skip_reason に保存する。利用者に理由を考えさせること自体にも
// 意味があるので、空でも送る。
export async function postSkip(proposalId, reason) {
  const result = await postJson(`/api/proposals/${proposalId}/skip`, { reason });
  if (!result.ok) {
    return { ok: false, kind: result.kind, message: result.message };
  }
  return { ok: true, changed: result.data.changed, outcome: result.data.outcome };
}

// POST /api/analyze — 「いま分析して」。
//
// このタスクの時点ではサーバー側にまだ存在しない（別タスクで作る）ため、
// 呼び出すと 404 などの失敗になる想定。失敗時は postJson が汎用の
// 日本語メッセージに落とすので、ここでは呼び出すだけにする。
export async function postAnalyze() {
  const result = await postJson("/api/analyze", {});
  if (!result.ok) {
    return { ok: false, kind: result.kind, message: result.message };
  }
  return { ok: true, data: result.data };
}

// ---------------------------------------------------------------------------
// 表示の整形
// ---------------------------------------------------------------------------

// 89900 → "89,900円"。null/undefined/NaN は「まだ分からない」を表す "—"。
// 0 とは区別する（0円 は本当にゼロという意味で使うこと）。
export function yen(n) {
  if (n === null || n === undefined || typeof n !== "number" || Number.isNaN(n)) {
    return "—";
  }
  return `${Math.round(n).toLocaleString("ja-JP")}円`;
}

// 0.0122 → "+1.2%"。負なら "-1.2%"。null/undefined/NaN は "—"。
export function pct(n) {
  if (n === null || n === undefined || typeof n !== "number" || Number.isNaN(n)) {
    return "—";
  }
  const value = n * 100;
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

// "2026-09-08T07:02:00Z" → "9月8日 7:02"
//
// 意図的に、閲覧しているブラウザのタイムゾーンには変換しない
// （UTCの構成要素をそのまま「月/日 時:分」として使う）。iPhone側の
// タイムゾーン設定によって表示がずれないようにするため。
export function whenText(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const month = d.getUTCMonth() + 1;
  const date = d.getUTCDate();
  const hours = d.getUTCHours();
  const minutes = String(d.getUTCMinutes()).padStart(2, "0");
  return `${month}月${date}日 ${hours}:${minutes}`;
}

// <input type="datetime-local"> の初期値用（ブラウザのローカル時刻表記）。
// "2026-09-08T16:02" のような、タイムゾーンを持たない文字列を返す。
export function toDatetimeLocalValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

// HTMLへ埋め込む文字列のエスケープ（rationale などDB由来の文字列を
// innerHTML に入れるときの事故防止）。
export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

// ---------------------------------------------------------------------------
// 画面上部の帯（banner）
// ---------------------------------------------------------------------------

// kind: "danger"（赤＝要対応） | "info"（灰＝情報）
// 戻り値は挿入できる HTMLElement。
export function banner(kind, text) {
  const el = document.createElement("div");
  el.className = `banner banner-${kind === "danger" ? "danger" : "info"}`;
  el.textContent = text;
  return el;
}

// ---------------------------------------------------------------------------
// 画面上部の共通の行き来
// ---------------------------------------------------------------------------

const NAV_ITEMS = [
  { key: "today", href: "/index.html", label: "今日やること" },
  { key: "holdings", href: "/holdings.html", label: "保有" },
  { key: "performance", href: "/performance.html", label: "収支" },
];

// active: "today" | "holdings" | "performance"
// 戻り値は挿入できる HTMLElement（<nav>）。
export function renderNav(active) {
  const nav = document.createElement("nav");
  nav.className = "app-nav";
  for (const item of NAV_ITEMS) {
    const a = document.createElement("a");
    a.href = item.href;
    a.textContent = item.label;
    a.className = "app-nav-link" + (item.key === active ? " app-nav-link-active" : "");
    if (item.key === active) {
      a.setAttribute("aria-current", "page");
    }
    nav.appendChild(a);
  }
  return nav;
}

// ---------------------------------------------------------------------------
// 「保有」「収支」画面のための追加の整形（Task 8）
//
// ここから下は、/api/state がそのまま返してはくれない表示専用の値を
// 作るための、小さな純粋関数だけを置く。**積み上げ計算（含み損益の
// 合計や枠の成績のような、複数の行にまたがる集計）はしない**。
// それはサーバー側（functions/_shared/state.js）の仕事のまま。
// ここにあるのは「サーバーがすでに返した1つ・2つの値を、その場で
// 見やすい形に直すだけ」の関数に限る。
// ---------------------------------------------------------------------------

// yen() は符号を付けない（1,100円 / -1,100円）。含み益・確定損益のように
// 「プラスのときは +1,100円 と出したい」場面専用の、符号付きの整形。
export function signedYen(n) {
  if (n === null || n === undefined || typeof n !== "number" || Number.isNaN(n)) {
    return "—";
  }
  const rounded = Math.round(n);
  const sign = rounded > 0 ? "+" : "";
  return `${sign}${rounded.toLocaleString("ja-JP")}円`;
}

// 0.267 → "26.7%"。勝率・損益トントンの勝率のような「0〜100%の割合」用。
// pct() は値上がり率のように +/- の符号を付けるための整形なので、
// 符号を付けたくない割合の表示にはこちらを使う。
export function ratePct(n) {
  if (n === null || n === undefined || typeof n !== "number" || Number.isNaN(n)) {
    return "—";
  }
  return `${(n * 100).toFixed(1)}%`;
}

// 現在値から、利確・損切りのライン（1つの値）までの距離を率で返す。
// (target - from) / from という、含み損益率と同じ形のその場の割り算で、
// 複数行の積み上げ計算ではない。
// from（現在値）が無ければ距離も出しようがないので null（0にしない）。
export function distancePct(fromPrice, toPrice) {
  if (
    fromPrice === null ||
    fromPrice === undefined ||
    !Number.isFinite(fromPrice) ||
    fromPrice === 0 ||
    toPrice === null ||
    toPrice === undefined ||
    !Number.isFinite(toPrice)
  ) {
    return null;
  }
  return (toPrice - fromPrice) / fromPrice;
}

// 回転枠の「期限」表示に使う business_days_held / days_left は、
// いまは /api/state（positions[].business_days_held / days_left）が
// 計算して返すので、この画面側では計算しない（functions/_shared/state.js
// 参照）。以前はここに祝日を考慮できない自前の計算があったが、
// サーバー側に移した。
