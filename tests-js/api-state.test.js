import { describe, expect, it, vi } from "vitest";
import { db } from "../functions/_shared/db.js";

// @neondatabase/serverless の neon() を丸ごと差し替える。本物のデータベースには
// 一切繋がず、「onRequestGet が正しい問い合わせを組み立て、正しい形で
// 返しているか」「途中の問い合わせが失敗したら500を返すか」だけを見る。

// queries を渡すと、実際に投げられたSQL文字列（テンプレートの断片を "?" で
// つないだもの）を全部記録する。SQLの中身そのものを確かめるテスト
// （守りが静かに消えていないかのテスト）で使う。
function makeSql(rowsByPattern, queries) {
  return async (strings) => {
    const query = strings.join("?");
    if (queries) queries.push(query);
    for (const [pattern, rows] of rowsByPattern) {
      if (pattern.test(query)) return rows;
    }
    throw new Error(`テスト用のダミーが用意されていない問い合わせ: ${query}`);
  };
}

// onRequestGet が投げる6本の問い合わせすべてに対応するダミー応答。
// capital は select_capital 相当を1本のSQLにまとめたもの（Minor 6）なので、
// パターンは1つだけになる。
const DEFAULT_ROWS = [
  [/quantity \* avg_price/, [{ c: "550000" }]],
  [/SELECT currency, amount FROM cash/, [{ currency: "JPY", amount: "550000" }, { currency: "USD", amount: "0" }]],
  [/FROM positions ORDER BY symbol/, []],
  [/FROM proposals WHERE outcome = 'pending'/, []],
  [/FROM fills WHERE applied_at IS NULL/, []],
  [/FROM trades/, []],
];

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

  it("枠ごとの成績のSQLは side = 'sell' と bucket IS NOT NULL の両方を条件にする（落とすと、買った記録まで決着した取引として数えてしまい、勝率・累計損益が狂う）", async () => {
    vi.resetModules();
    const queries = [];
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => makeSql(DEFAULT_ROWS, queries),
    }));
    const { onRequestGet } = await import("../functions/api/state.js");
    await onRequestGet({ env: { DATABASE_URL: "postgres://dummy" } });

    const performanceQuery = queries.find((q) => /FROM trades/.test(q));
    expect(performanceQuery).toBeDefined();
    expect(performanceQuery).toMatch(/side\s*=\s*'sell'/);
    expect(performanceQuery).toMatch(/bucket IS NOT NULL/);
  });

  it("/api/state が投げるSQLは、すべて SELECT で始まる（書き込みが紛れ込んでいないことの守り）", async () => {
    vi.resetModules();
    const queries = [];
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => makeSql(DEFAULT_ROWS, queries),
    }));
    const { onRequestGet } = await import("../functions/api/state.js");
    await onRequestGet({ env: { DATABASE_URL: "postgres://dummy" } });

    expect(queries.length).toBeGreaterThan(0);
    for (const q of queries) {
      expect(q.trim().toUpperCase().startsWith("SELECT")).toBe(true);
    }
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
