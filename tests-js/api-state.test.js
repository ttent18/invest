import { describe, expect, it, vi } from "vitest";

// @neondatabase/serverless の neon() を丸ごと差し替える。本物のデータベースには
// 一切繋がず、「onRequestGet が正しい問い合わせを組み立て、正しい形で
// 返しているか」「途中の問い合わせが失敗したら500を返すか」だけを見る。

function makeSql(rowsByPattern) {
  return async (strings) => {
    const query = strings.join("?");
    for (const [pattern, rows] of rowsByPattern) {
      if (pattern.test(query)) return rows;
    }
    throw new Error(`テスト用のダミーが用意されていない問い合わせ: ${query}`);
  };
}

describe("GET /api/state（データベースへの接続は差し替える）", () => {
  it("各問い合わせの結果を、契約どおりのJSONにまとめて返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql([
          [/FROM cash WHERE currency = 'JPY'/, [{ c: "550000" }]],
          [/quantity \* avg_price/, [{ c: "0" }]],
          [/SELECT currency, amount FROM cash/, [{ currency: "JPY", amount: "550000" }, { currency: "USD", amount: "0" }]],
          [/FROM positions ORDER BY symbol/, []],
          [/FROM proposals WHERE outcome = 'pending'/, []],
          [/FROM fills WHERE applied_at IS NULL/, []],
          [/FROM trades/, []],
        ]),
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
});
