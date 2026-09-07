import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { BUCKETS } from "../functions/_shared/config.js";
import { db } from "../functions/_shared/db.js";

// src/investment/db.py の中身を実際に読んで、そこに書かれているSQLと
// 突き合わせる。JavaScript 側に書いた期待値どうしを比べても、Python 側を
// 直したときに気づけないため（写しの検査になっていない）。
const DB_PY = readFileSync(new URL("../src/investment/db.py", import.meta.url), "utf8");

// db.py の関数1つ分の本文を取り出す。
// 見つからなければ（関数名が変わった等）テストを落とす。黙って素通りさせない。
function pySource(functionName) {
  const match = DB_PY.match(new RegExp(`\\ndef ${functionName}\\(([\\s\\S]*?)\\n(?=\\n*def |\\n*$)`));
  if (!match) {
    throw new Error(
      `src/investment/db.py に ${functionName} が見つからない。` +
        `関数名が変わったなら、このテストの突き合わせ先も直すこと。`
    );
  }
  return normalize(match[0]);
}

// SQL を比べやすくする。改行と連続する空白を1つの空白にまとめる。
function normalize(sql) {
  return sql.replace(/\s+/g, " ").trim();
}

// @neondatabase/serverless の neon() を丸ごと差し替える。本物のデータベースには
// 一切繋がず、「onRequestGet が正しい問い合わせを組み立て、正しい形で
// 返しているか」「途中の問い合わせが失敗したら500を返すか」だけを見る。

// queries を渡すと、実際に投げられた問い合わせを全部記録する。
// sql はテンプレートの断片を "?" でつないだ文字列、params は "?" に入る値。
// SQLの中身そのものを確かめるテスト（守りが静かに消えていないかのテスト）で使う。
function makeSql(rowsByPattern, queries) {
  return async (strings, ...params) => {
    const query = strings.join("?");
    if (queries) queries.push({ sql: query, params });
    for (const [pattern, rows] of rowsByPattern) {
      if (pattern.test(query)) return rows;
    }
    throw new Error(`テスト用のダミーが用意されていない問い合わせ: ${query}`);
  };
}

// onRequestGet が投げる問い合わせすべてに対応するダミー応答。
// capital は select_capital 相当を1本のSQLにまとめたもの（Minor 6）なので、
// パターンは1つだけになる。fundamentals（会社名）は保有も提案も0件のときは
// 投げられないが、銘柄がある場合のテストのために用意しておく。
const DEFAULT_ROWS = [
  [/quantity \* avg_price/, [{ c: "550000" }]],
  [/SELECT currency, amount FROM cash/, [{ currency: "JPY", amount: "550000" }, { currency: "USD", amount: "0" }]],
  [/FROM positions ORDER BY symbol/, []],
  [/FROM proposals WHERE outcome = 'pending'/, []],
  [/FROM fills WHERE applied_at IS NULL/, []],
  [/FROM trades/, []],
  [/FROM fundamentals/, []],
];

// 1銘柄だけ保有している状態のダミー。会社名まわりのテストで使う。
const POSITION_ROW = {
  symbol: "7203.T", bucket: "じっくり", quantity: 100, avg_price: "899",
  take_profit: "1096.78", stop_loss: "827.08", opened_at: "2026-09-01T00:00:00Z",
  last_price: null, last_price_at: null,
};

function rowsWithOnePosition(overrides = []) {
  return [
    ...overrides,
    [/FROM positions ORDER BY symbol/, [POSITION_ROW]],
    ...DEFAULT_ROWS,
  ];
}

// テスト用の env。DATABASE_URL だけ入れておく。
const ENV = { DATABASE_URL: "postgres://dummy" };

// onRequestGet を、差し替えたデータベースで1回呼ぶ小さな道具。
async function callState({ rows = DEFAULT_ROWS, env = ENV, queries } = {}) {
  vi.resetModules();
  vi.doMock("@neondatabase/serverless", () => ({ neon: () => makeSql(rows, queries) }));
  const { onRequestGet } = await import("../functions/api/state.js");
  return onRequestGet({ env });
}

describe("GET /api/state（データベースへの接続は差し替える）", () => {
  it("各問い合わせの結果を、契約どおりのJSONにまとめて返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => makeSql(DEFAULT_ROWS),
    }));
    const { onRequestGet } = await import("../functions/api/state.js");

    const res = await onRequestGet({ env: { DATABASE_URL: "postgres://dummy", VAPID_PUBLIC_KEY: "BCCgMo8e" } });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.capital).toBe(550000);
    expect(body.cash).toEqual({ JPY: 550000, USD: 0 });
    expect(body.vapid_public_key).toBe("BCCgMo8e");
    expect(body.buckets).toHaveLength(2);
    expect(body.performance).toHaveLength(2);
    expect(body.positions).toEqual([]);
    expect(body.proposals).toEqual([]);
    expect(body.unapplied_fills).toEqual([]);
  });

  it("いずれか1つの問い合わせが失敗したら、内部の詳細を含めずに500を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => async () => {
        throw new Error("connection to postgres://user:secret@host/db failed");
      },
    }));
    const { onRequestGet } = await import("../functions/api/state.js");

    const res = await onRequestGet({ env: { DATABASE_URL: "postgres://dummy" } });
    expect(res.status).toBe(500);
    const body = await res.json();
    expect(Object.keys(body)).toEqual(["message"]);
    expect(body.message).not.toContain("postgres://");
    expect(body.message).not.toContain("secret");
  });

  it("DATABASE_URL が無ければ、その旨も内部の詳細を出さずに500を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({ neon: () => makeSql([]) }));
    const { onRequestGet } = await import("../functions/api/state.js");

    const res = await onRequestGet({ env: {} });
    expect(res.status).toBe(500);
    const body = await res.json();
    expect(Object.keys(body)).toEqual(["message"]);
  });

  it("/api/state が投げるSQLは、すべて SELECT で始まる（書き込みが紛れ込んでいないことの守り）", async () => {
    const queries = [];
    await callState({ rows: rowsWithOnePosition(), queries });

    expect(queries.length).toBeGreaterThan(0);
    for (const q of queries) {
      expect(q.sql.trim().toUpperCase().startsWith("SELECT")).toBe(true);
    }
  });
});

// ここから下は「SQLの条件が静かに消えていないか」の守り。
// 文字列を1文字書き換えるだけで意味が変わるのに、返ってくるJSONの形は
// 同じままなので、形のテストでは気づけない。Python 側（src/investment/db.py）
// の対応する関数を実際に読み、同じ表・同じ条件であることを確かめる。
describe("SQLが src/investment/db.py と同じ表・同じ条件であること", () => {
  async function queryMatching(pattern) {
    const queries = [];
    await callState({ rows: rowsWithOnePosition(), queries });
    const found = queries.find((q) => pattern.test(q.sql));
    expect(found, `${pattern} に当たる問い合わせが投げられていない`).toBeDefined();
    return { sql: normalize(found.sql), params: found.params };
  }

  it("保有: db.py の select_positions と同じ（SELECT * FROM positions ORDER BY symbol）", async () => {
    // 並び順まで同じにする。順番が違うと、画面に出る保有の並びが
    // Python 側の一覧と食い違い、突き合わせが難しくなる。
    const expected = "SELECT * FROM positions ORDER BY symbol";
    expect(pySource("select_positions")).toContain(expected);
    expect((await queryMatching(/FROM positions ORDER BY/)).sql).toBe(expected);
  });

  it("現金: db.py の select_cash と同じ（通貨で絞らず、全通貨の残高を返す）", async () => {
    const expected = "SELECT currency, amount FROM cash";
    expect(pySource("select_cash")).toContain(expected);
    const { sql } = await queryMatching(/SELECT currency, amount FROM cash/);
    expect(sql).toBe(expected);
    // WHERE を足すと通貨が1つ消える（画面から USD が消えても気づけない）。
    expect(sql).not.toMatch(/WHERE/i);
  });

  it("総資金: db.py の select_capital と同じ（現金JPYの合計 ＋ 保有JPYの取得原価の合計）", async () => {
    // Python は同じ接続の中で2本に分けて投げ、足している。こちらは
    // 1本のSQLの中で足す（理由は functions/api/state.js のコメント参照）が、
    // **数えている中身は同じでなければならない。**
    const py = pySource("select_capital");
    expect(py).toContain("SUM(amount)");
    expect(py).toContain("FROM cash WHERE currency = 'JPY'");
    expect(py).toContain("SUM(quantity * avg_price)");
    expect(py).toContain("FROM positions WHERE currency = 'JPY'");

    const { sql } = await queryMatching(/quantity \* avg_price/);
    expect(sql).toContain("SUM(amount)");
    expect(sql).toContain("FROM cash WHERE currency = 'JPY'");
    expect(sql).toContain("SUM(quantity * avg_price)");
    expect(sql).toContain("FROM positions WHERE currency = 'JPY'");
    // 現在の株価は使わない（含み益で次に買う金額が膨らむのを防ぐため）。
    expect(sql).not.toMatch(/last_price/);
  });

  it("枠ごとの成績: db.py の select_bucket_performance と同じ条件（side = 'sell' AND bucket IS NOT NULL）", async () => {
    // 落とすと、買った記録まで決着した取引として数えてしまい、
    // 勝率・累計損益が狂う。
    expect(pySource("select_bucket_performance")).toContain("side = 'sell' AND bucket IS NOT NULL");
    const { sql } = await queryMatching(/FROM trades/);
    expect(sql).toMatch(/side\s*=\s*'sell'/);
    expect(sql).toContain("bucket IS NOT NULL");
    expect(sql).toContain("realized_pnl > 0");
    expect(sql).toContain("AVG(holding_days)");
  });

  it("期限で降りた件数は、保有日数から導く。日数は config.js の枠の定義から渡す（SQLに 10 と書かない）", async () => {
    const { sql, params } = await queryMatching(/FROM trades/);
    expect(sql).toMatch(/COUNT\(\*\) FILTER \(WHERE holding_days >= \?\)/);
    // SQLの中に日数を直接書くと、config.py の期限を変えたときにここだけ
    // 古い数字が残る。値は必ず枠の定義（回転枠の max_holding_days）から来ること。
    const limit = BUCKETS.find((b) => b.max_holding_days !== null).max_holding_days;
    expect(params).toContain(limit);
  });

  it("会社名は fundamentals から、銘柄ごとの一番新しい取得日の行を引く", async () => {
    const { sql, params } = await queryMatching(/FROM fundamentals/);
    expect(sql).toContain("SELECT DISTINCT ON (symbol) symbol, name");
    expect(sql).toContain("ORDER BY symbol, as_of DESC");
    // 銘柄コードは文字列としてSQLに埋め込まず、パラメータで渡す。
    expect(sql).toContain("WHERE symbol = ANY(?)");
    expect(params[0]).toEqual(["7203.T"]);
  });
});

describe("会社名（補助の失敗が本体を巻き添えにしないこと）", () => {
  it("会社名が引ければ、保有に name が入る", async () => {
    const res = await callState({
      rows: rowsWithOnePosition([[/FROM fundamentals/, [{ symbol: "7203.T", name: "トヨタ自動車" }]]]),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.positions[0].name).toBe("トヨタ自動車");
  });

  it("会社名の問い合わせだけが失敗しても、200 で保有・現金・提案は返る（name は null）", async () => {
    // ここが一番大事な守り。会社名が出ないのは不便なだけだが、
    // 保有や現金が見えなくなると「持っていないと思って買い増す」事故になる。
    const rows = rowsWithOnePosition();
    const failingSql = async (strings, ...params) => {
      const query = strings.join("?");
      if (/FROM fundamentals/.test(query)) {
        throw new Error("connection to postgres://user:secret@host/db failed");
      }
      return makeSql(rows)(strings, ...params);
    };
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({ neon: () => failingSql }));
    const { onRequestGet } = await import("../functions/api/state.js");

    const res = await onRequestGet({ env: ENV });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.positions).toHaveLength(1);
    expect(body.positions[0].symbol).toBe("7203.T");
    expect(body.positions[0].name).toBe(null);
    expect(body.positions[0].quantity).toBe(100);
    expect(body.capital).toBe(550000);
    // 失敗の中身が応答に漏れていないこと。
    expect(JSON.stringify(body)).not.toContain("secret");
  });

  it("会社名が1件だけ引けなくても、他の銘柄の会社名は消えない", async () => {
    const second = { ...POSITION_ROW, symbol: "9999.T" };
    const res = await callState({
      rows: [
        [/FROM positions ORDER BY symbol/, [POSITION_ROW, second]],
        [/FROM fundamentals/, [{ symbol: "7203.T", name: "トヨタ自動車" }]],
        ...DEFAULT_ROWS,
      ],
    });
    const body = await res.json();
    expect(body.positions.map((p) => [p.symbol, p.name])).toEqual([
      ["7203.T", "トヨタ自動車"],
      ["9999.T", null],
    ]);
  });

  it("保有も提案も0件なら、会社名の問い合わせは投げない（無駄に往復しない）", async () => {
    const queries = [];
    await callState({ queries });
    expect(queries.some((q) => /FROM fundamentals/.test(q.sql))).toBe(false);
  });
});

describe("仮想資金か実弾かは環境変数から決める（IS_VIRTUAL）", () => {
  it("IS_VIRTUAL が未設定なら is_virtual は true（安全側）", async () => {
    const res = await callState({ env: { DATABASE_URL: "postgres://dummy" } });
    expect((await res.json()).is_virtual).toBe(true);
  });

  it('IS_VIRTUAL が "false" のときだけ is_virtual は false', async () => {
    const res = await callState({ env: { DATABASE_URL: "postgres://dummy", IS_VIRTUAL: "false" } });
    expect((await res.json()).is_virtual).toBe(false);
  });

  it('IS_VIRTUAL が "true" なら is_virtual は true', async () => {
    const res = await callState({ env: { DATABASE_URL: "postgres://dummy", IS_VIRTUAL: "true" } });
    expect((await res.json()).is_virtual).toBe(true);
  });

  it("IS_VIRTUAL に思いがけない値が入っていても true（実弾だと言い出さない）", async () => {
    const res = await callState({ env: { DATABASE_URL: "postgres://dummy", IS_VIRTUAL: "no" } });
    expect((await res.json()).is_virtual).toBe(true);
  });
});

describe("db(env)（Neonへの接続そのもの）", () => {
  it("DATABASE_URL が無ければ、接続を試みる前に例外を投げる", () => {
    // functions/api/state.js が500を返すこと自体は上のテストで確認済みだが、
    // あちらはテスト用の差し替え（makeSql）がどのみち例外を投げるため、
    // functions/_shared/db.js のガード（if (!env.DATABASE_URL) throw ...）を
    // 消しても通ってしまう。ここでは db() を直接呼び、ガードそのものを確かめる。
    // メッセージそのものを確かめる。単に toThrow() だけだと、この
    // ガードを消してもテストが通ってしまう（neon() 自身も接続文字列が
    // 無ければ別の理由で例外を投げるため）。ガードが実際に働いている
    // ことを確かめるには、ガード自身のメッセージを見る必要がある。
    expect(() => db({})).toThrow("DATABASE_URL が設定されていません");
  });
});
