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
// 合言葉を入れ直す画面へ移る。いま見ている画面を覚えさせて、
// 入れ直したあと同じ場所に戻れるようにする。
export function goToLogin() {
  const next = window.location.pathname + window.location.search;
  window.location.assign(`/api/login?next=${encodeURIComponent(next)}`);
}

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

  // サーバーがエラーを返した場合（500など）。サーバーが返す message は
  // 「いまの状態を読み込めませんでした」だけで、利用者が次に何をすれば
  // よいかが分からない。通信失敗のほうには「電波を確かめて…」という
  // 次の一手があるので、こちらにも足す。**画面が読めないときこそ、
  // 本当の保有と注文はSBIのアプリで確かめてほしい**（この画面が黙って
  // 古い内容を見せているのではないか、と疑えるようにするため）。
  const NEXT_STEP =
    "時間をおいて「もう一度読み込む」を押してください。" +
    "それでも直らないときは、いまの保有と予約注文をSBIのアプリで確かめてください";

  // 合言葉の記憶が切れた。**利用者が自力で入り直せる場所へ送る。**
  // ここで「読み込めませんでした」とだけ出すと、入れ直す場所が
  // どこにも無く、利用者は詰む（2026-09-10 に実際に起きた）。
  if (res.status === 401 && body && body.reason === "login_required") {
    goToLogin();
    throw new Error("合言葉の記憶が切れました。合言葉の画面に移ります");
  }

  if (!res.ok) {
    const detail = body && typeof body.message === "string" ? body.message : "いまの状態を読み込めませんでした";
    throw new Error(`${detail}。${NEXT_STEP}`);
  }

  if (!body) {
    throw new Error(`いまの状態を読み込めませんでした。${NEXT_STEP}`);
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

  // 合言葉の記憶が切れた。
  //
  // **ここでは勝手に画面を移らない。** 送信の途中なので、移ると
  // 利用者が入力した株数・値段・日時が消える。入れ直す場所だけ案内し、
  // 入力はそのまま残す（入れ直したあと、もう一度押せば送れる）。
  if (res.status === 401 && data && data.reason === "login_required") {
    return {
      ok: false,
      kind: "login_required",
      message:
        "合言葉の記憶が切れました。この画面を開き直して合言葉を入れると、" +
        "入力はそのままもう一度送れます",
    };
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

// ---------------------------------------------------------------------------
// 二重送信よけの鍵の置き場所
//
// 鍵をモーダル（入力欄）の中の変数だけに持たせると、
// 「送信に失敗 → [やめる] → もう一度 [売った]」のときに新しい鍵になる。
// サーバーには届いていたが応答だけが届かなかった場合、同じ売買が2行
// 記録されてしまう（お金の記録が二重になる）。
// そこで「どの売買か」を表す文字列（scope）をキーに sessionStorage へ
// 退避し、同じ売買に対しては同じ鍵を使い回す。
//
// **記録に成功したら forgetClientKey() で必ず捨てること。**
// 捨てないと、次に同じ銘柄を売買したときに同じ鍵が使われ、サーバーが
// 「その鍵はもう記録済み」と判断して2回目の売買が記録されない。
//
// sessionStorage はプライベートブラウズなどで例外を投げることがある。
// その場合は落とさずに、その場限りの新しい鍵を返す（＝これまでどおり
// モーダルの中だけで鍵を持つ動きに戻る）。
// ---------------------------------------------------------------------------

const CLIENT_KEY_PREFIX = "fill-client-key:";

// scope の例: "proposal:12:buy" / "position:7203.T:sell"
export function clientKeyFor(scope) {
  const storageKey = CLIENT_KEY_PREFIX + scope;
  try {
    const saved = window.sessionStorage.getItem(storageKey);
    if (saved) return saved;
    const created = newClientKey();
    window.sessionStorage.setItem(storageKey, created);
    return created;
  } catch {
    return newClientKey();
  }
}

export function forgetClientKey(scope) {
  try {
    window.sessionStorage.removeItem(CLIENT_KEY_PREFIX + scope);
  } catch {
    // 使えないだけなので、何もしないで進む。
  }
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

// POST /api/subscribe — 通知の宛先を登録する。
//
// subscription はブラウザの PushSubscription.toJSON() をそのまま渡す
// （endpoint / keys.p256dh / keys.auth）。形は
// functions/_shared/validate.js の validateSubscription が検証する形と
// 同じにすること（task-9-report.md に対応表がある）。
export async function postSubscribe(subscription) {
  const result = await postJson("/api/subscribe", subscription);
  if (!result.ok) {
    return { ok: false, kind: result.kind, message: result.message };
  }
  return { ok: true };
}

// /api/state が返す vapid_public_key（base64url の文字列）を、
// pushManager.subscribe({ applicationServerKey }) が要求する Uint8Array に
// 変換する。Web Push の決まりごとで、ブラウザの Push API はこの形でしか
// 鍵を受け取れない。
export function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i++) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
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

// "2026-09-07T23:02:00Z" → "9月8日 8:02"
//
// iPhone のタイムゾーン設定に関係なく、**常に日本時間（Asia/Tokyo）で
// 出す**。データベースの時刻はすべてUTCで入っている（last_price_at や
// created_at は NOW()）ので、日本時間に直してから見せる必要がある。
//
// サーバー側も日本時間で数えている（functions/_shared/state.js の
// jstDayNumber が「何日前の提案か」を日本時間のカレンダー上の日付で
// 数える）。ここを日本時間にしておくことで、同じカードの中の
// 「今日の提案」と「提案が出たのは ◯月◯日 ◯:◯◯」が食い違わない。
//
// （以前はUTCの構成要素をそのまま使っていたため、表示が常に日本時間の
//  9時間前になり、朝8時台に保存された株価が「前日の23時台」として
//  出ていた。）
//
// hourCycle: "h23" を明示するのは、環境によって深夜0時が "24:00" と
// 出ることがあるため（0〜23 に固定する）。
const JST_TIME_FORMAT = new Intl.DateTimeFormat("ja-JP", {
  timeZone: "Asia/Tokyo",
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function whenText(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const parts = JST_TIME_FORMAT.formatToParts(d);
  const part = (type) => parts.find((p) => p.type === type)?.value ?? "";
  const month = Number(part("month"));
  const day = Number(part("day"));
  const hour = Number(part("hour"));
  const minute = part("minute");
  if (!Number.isFinite(month) || !Number.isFinite(day) || !Number.isFinite(hour) || !minute) {
    return "—";
  }
  return `${month}月${day}日 ${hour}:${minute}`;
}

// <input type="datetime-local"> の初期値用。
// "2026-09-08T16:02" のような、タイムゾーンを持たない文字列を返す。
//
// **whenText と同じく、常に日本時間（Asia/Tokyo）で出す。**
// 以前は端末のローカル時刻（date.getHours() など）を使っていたため、
// iPhone のタイムゾーンが日本以外だと、同じ画面の中で
// 「提案が出たのは 9月8日 10:30」（日本時間）と
// 「売買した日時 2026-09-08T01:30」（端末の時刻）が並び、
// どちらを基準に入れればよいのか分からなくなっていた。
//
// **この関数を日本時間にする以上、入力された文字列を読み戻す側も
// 日本時間として読まなければならない**（下の jstLocalToIso）。
// new Date("2026-09-08T16:02") は端末のローカル時刻として解釈するので、
// 表示だけ日本時間にして読み戻しを変えないと、記録される時刻が
// 端末のずれのぶんだけ間違ったものになる。
const JST_DATETIME_LOCAL_FORMAT = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Tokyo",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function toDatetimeLocalValue(date) {
  const parts = JST_DATETIME_LOCAL_FORMAT.formatToParts(date);
  const part = (type) => parts.find((p) => p.type === type)?.value ?? "";
  const year = part("year");
  const month = part("month");
  const day = part("day");
  const hour = part("hour");
  const minute = part("minute");
  if (!year || !month || !day || !hour || !minute) return "";
  return `${year}-${month}-${day}T${hour}:${minute}`;
}

// toDatetimeLocalValue が入れた（そして利用者が直した）
// "2026-09-08T16:02" を、**日本時間として読んで** ISO 文字列にする。
//
// 日本時間は UTC+9 の固定で、夏時間による切り替えが無い。だから
// 「日本時間の 16:02」は「UTCの 07:02」であり、Date.UTC の時に 9 を
// 引くだけで正確に出せる（Date.UTC は時が負でも前日に繰り下がる）。
//
// 読めない文字列（空・形が違う）は null を返す。呼び出し側は
// null を「日時の指定なし」としてサーバーに渡す。
export function jstLocalToIso(raw) {
  if (typeof raw !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(raw);
  if (!m) return null;
  const ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]) - 9, Number(m[5]));
  if (Number.isNaN(ms)) return null;
  return new Date(ms).toISOString();
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

// 「反映できていない記録」の帯。**3つの画面すべてで出すこと。**
//
// [買った]／[売った]を記録しても、保有と現金に反映されるのは
// GitHub Actions の次回実行のとき。それまでは画面を読み込み直しても
// 保有はそのまま残り、ボタンも押せる状態のままになる。この帯が無いと、
// 利用者は「記録できたのか分からない」まま同じ売買をもう一度記録して
// しまう（お金の記録が二重になる）。
//
// root: 帯を差し込む要素（各画面の banner-root）。
// fills: /api/state の unapplied_fills（applied_at が NULL の記録）。
export function renderUnappliedBanner(root, fills) {
  if (!root) return;
  if (!Array.isArray(fills) || fills.length === 0) return;

  const wrap = document.createElement("div");
  wrap.className = "banner banner-danger";

  const title = document.createElement("div");
  title.textContent = `まだ反映できていない記録が${fills.length}件あります`;
  wrap.appendChild(title);

  const list = document.createElement("ul");
  list.className = "unapplied-list";
  let hasFailed = false;
  for (const f of fills) {
    const li = document.createElement("li");
    const sideText = f.side === "sell" ? "売り" : "買い";
    // 銘柄と売買の向きだけでは、同じ銘柄を2回記録したときに
    // どちらの記録のことなのか分からない。株数と値段も来ているので
    // 一緒に出して、見分けられるようにする。
    const quantityText =
      typeof f.quantity === "number" && Number.isFinite(f.quantity) ? `${f.quantity}株` : "株数が不明";
    const priceText =
      typeof f.price === "number" && Number.isFinite(f.price) ? `1株 ${yen(f.price)}` : "値段が不明";
    const reason = f.apply_error
      ? `反映できませんでした：${f.apply_error}`
      : "まだ反映されていません（次の自動処理を待っています）";
    if (f.apply_error) hasFailed = true;
    li.textContent = `${f.symbol}（${sideText}） ${quantityText} ・ ${priceText} … ${reason}`;
    list.appendChild(li);
  }
  wrap.appendChild(list);

  const note = document.createElement("div");
  note.className = "unapplied-note";
  // 「反映できませんでした」の記録は、保有にも現金にも入っていない。
  // それを「もう届いているから触るな」と言うと、利用者はそこから
  // 先へ進めなくなる（同じ理由で毎回失敗し続けるため）。
  note.textContent = hasFailed
    ? "反映されるまで、保有と収支の表示は変わりません。まだ反映されていない記録は、もう届いているので同じ売買をもう一度記録しないでください。" +
      "ただし「反映できませんでした」と出ている記録は、保有にも現金にも入っていません。理由を直したうえで、もう一度記録し直してください。"
    : "反映されるまで、保有と収支の表示は変わりません。記録はもう届いているので、同じ売買をもう一度記録しないでください。";
  wrap.appendChild(note);

  root.appendChild(wrap);
}

// ---------------------------------------------------------------------------
// 反映待ちの記録の引き当て
//
// [買った]／[売った]を記録しても、提案や保有の見た目が変わるのは
// 反映（GitHub Actions の次回実行）が済んだあと。それまでは同じ提案・
// 同じ保有がボタン付きで出たままなので、二重に記録しないよう
// unapplied_fills と突き合わせてボタンを止める必要がある。
//
// **ここで守っている決まりは2つ。**
//
// 1. 引き当ては proposal_id で行う。銘柄コードと売買の向きだけで
//    引くと、同じ銘柄・同じ向きの提案が2件並んだときに、片方だけ
//    記録したのに両方が「記録済み」になる。反映後、記録していない
//    ほうが生き返り、そこで [買った] を押すと保有中の銘柄の買い増しに
//    なる（この仕組みが禁じている操作）。proposal_id を持たない記録
//    （保有画面から出した売りなど）だけ、銘柄＋向きで引く。
//
// 2. **apply_error が付いた記録は「記録済み」に数えない。**
//    反映に失敗した記録は、同じ理由で毎回失敗し続け、放っておいても
//    決して反映されない。それを「記録済み」として扱うと、その銘柄の
//    ボタンが永久に押せなくなる。fills を消す手段は画面にも API にも
//    無いので、iPhone しか持っていない利用者はそこで詰む。
//    数えない代わりに、失敗した理由をカードに出す（下の failedFillFor）。
// ---------------------------------------------------------------------------

function fillKey(symbol, side) {
  return `${symbol}:${side}`;
}

// fills: /api/state の unapplied_fills。
// 戻り値は recordedFillFor / failedFillFor に渡すための索引。
export function indexUnappliedFills(fills) {
  const index = {
    // 提案に紐づいた記録（proposal_id あり）
    recordedByProposal: new Set(),
    failedByProposal: new Map(),
    // 提案に紐づかない記録（proposal_id が null。保有画面からの売りなど）
    recordedByLooseSymbolSide: new Set(),
    failedByLooseSymbolSide: new Map(),
    // 銘柄＋向きだけで引くとき用（保有画面はこちらを使う）。
    // proposal_id の有無にかかわらず全部入っている。
    recordedBySymbolSide: new Set(),
    failedBySymbolSide: new Map(),
  };
  if (!Array.isArray(fills)) return index;
  for (const f of fills) {
    if (!f || typeof f.symbol !== "string" || typeof f.side !== "string") continue;
    const hasProposalId = f.proposal_id !== null && f.proposal_id !== undefined;
    const key = fillKey(f.symbol, f.side);
    if (f.apply_error) {
      const reason = typeof f.apply_error === "string" ? f.apply_error : String(f.apply_error);
      if (hasProposalId) index.failedByProposal.set(f.proposal_id, reason);
      else if (!index.failedByLooseSymbolSide.has(key)) index.failedByLooseSymbolSide.set(key, reason);
      if (!index.failedBySymbolSide.has(key)) index.failedBySymbolSide.set(key, reason);
      // **記録済みには数えない。** 数えるとボタンが永久に押せなくなる。
      continue;
    }
    if (hasProposalId) index.recordedByProposal.add(f.proposal_id);
    else index.recordedByLooseSymbolSide.add(key);
    index.recordedBySymbolSide.add(key);
  }
  return index;
}

// 提案に対する引き当て。**まず proposal_id で引く。**
// proposal_id を持たない記録だけ、銘柄＋向きで引く。
export function recordedFillForProposal(index, proposalId, symbol, side) {
  if (!index) return false;
  if (proposalId !== null && proposalId !== undefined && index.recordedByProposal.has(proposalId)) {
    return true;
  }
  return index.recordedByLooseSymbolSide.has(fillKey(symbol, side));
}

// 保有（提案を経由しない）に対する引き当て。銘柄＋向きで引く。
export function recordedFillForSymbol(index, symbol, side) {
  if (!index) return false;
  return index.recordedBySymbolSide.has(fillKey(symbol, side));
}

// 提案に対する「反映できなかった理由」（無ければ null）。
// 引き当ての決まりは recordedFillForProposal と同じにする。
export function failedFillForProposal(index, proposalId, symbol, side) {
  if (!index) return null;
  if (proposalId !== null && proposalId !== undefined) {
    const byProposal = index.failedByProposal.get(proposalId);
    if (byProposal) return byProposal;
  }
  return index.failedByLooseSymbolSide.get(fillKey(symbol, side)) ?? null;
}

// 保有に対する「反映できなかった理由」（無ければ null）。
export function failedFillForSymbol(index, symbol, side) {
  if (!index) return null;
  return index.failedBySymbolSide.get(fillKey(symbol, side)) ?? null;
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

// 日本株（銘柄コードが ".T" で終わる）かどうか。
//
// **functions/_shared/validate.js の isJapaneseStock（15〜17行目）と
// 同じ判定にすること。** サーバー側は日本株にだけ「100株単位」を
// 課す（validate.js の 53行目）ので、画面の案内もそれに合わせて
// 出し分ける。無条件に「100株単位で入れてください」と書くと、
// 米国株を売るときに嘘の案内になる。
export function isJapaneseStock(symbol) {
  return typeof symbol === "string" && symbol.toUpperCase().endsWith(".T");
}

// 回転枠の「期限」表示に使う business_days_held / days_left は、
// いまは /api/state（positions[].business_days_held / days_left）が
// 計算して返すので、この画面側では計算しない（functions/_shared/state.js
// 参照）。以前はここに祝日を考慮できない自前の計算があったが、
// サーバー側に移した。
