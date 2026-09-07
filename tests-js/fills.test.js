import { describe, expect, it, vi } from "vitest";

// @neondatabase/serverless の neon() を丸ごと差し替える。本物のデータベースには
// 一切繋がず、「onRequestPost が正しい問い合わせを組み立て、正しい形で
// 返しているか」「検証で弾いたときに何もINSERTしないか」「同じ client_key
// で2回来たときにエラーにならないか」だけを見る。

function makeSql(handler) {
  return async (strings, ...values) => handler(strings.join("?"), values);
}

function req(body) {
  return { json: async () => body };
}

const goodBody = () => ({
  client_key: "k1",
  proposal_id: 3,
  symbol: "156A.T",
  side: "buy",
  quantity: 100,
  price: 899,
});

describe("POST /api/fills", () => {
  it("検証で弾かれた内容は、データベースに一切触らずに400を返す", async () => {
    vi.resetModules();
    let called = false;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql(() => {
          called = true;
          throw new Error("呼ばれてはいけない");
        }),
    }));
    const { onRequestPost } = await import("../functions/api/fills.js");

    const res = await onRequestPost({
      request: req({ ...goodBody(), quantity: 137 }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.message).toContain("100株");
    expect(called).toBe(false);
  });

  it("新規の client_key ならINSERTして created:true を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql((query) => {
          if (query.includes("INSERT INTO fills")) {
            return [
              { id: 42, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "2026-09-08T01:00:00Z" },
            ];
          }
          throw new Error(`想定していない問い合わせ: ${query}`);
        }),
    }));
    const { onRequestPost } = await import("../functions/api/fills.js");

    const res = await onRequestPost({ request: req(goodBody()), env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.created).toBe(true);
    expect(body.fill.id).toBe(42);
    expect(body.fill.quantity).toBe(100);
  });

  it("同じ client_key で2回目に来たときは、エラーにせず既存の行を created:false で返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql((query) => {
          if (query.includes("INSERT INTO fills")) {
            return []; // ON CONFLICT DO NOTHING で何も挿入されなかった
          }
          if (query.includes("SELECT id, symbol")) {
            return [
              { id: 7, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "2026-09-08T00:00:00Z" },
            ];
          }
          throw new Error(`想定していない問い合わせ: ${query}`);
        }),
    }));
    const { onRequestPost } = await import("../functions/api/fills.js");

    const res = await onRequestPost({ request: req(goodBody()), env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.created).toBe(false);
    expect(body.fill.id).toBe(7);
  });

  it("データベースへの問い合わせが失敗したら、内部の詳細を含めずに500を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => async () => {
        throw new Error("connection to postgres://user:secret@host/db failed");
      },
    }));
    const { onRequestPost } = await import("../functions/api/fills.js");

    const res = await onRequestPost({ request: req(goodBody()), env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(500);
    const body = await res.json();
    expect(Object.keys(body)).toEqual(["message"]);
    expect(body.message).not.toContain("postgres://");
    expect(body.message).not.toContain("secret");
  });

  it("送信の形式がJSONとして読めなければ400を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({ neon: () => makeSql(() => []) }));
    const { onRequestPost } = await import("../functions/api/fills.js");

    const res = await onRequestPost({
      request: { json: async () => { throw new Error("bad json"); } },
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(400);
  });
});
