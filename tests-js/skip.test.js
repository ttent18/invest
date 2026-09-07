import { describe, expect, it, vi } from "vitest";

// @neondatabase/serverless の neon() を丸ごと差し替える。
// 「存在しない id は404」「既に pending でない id は200 + changed:false」
// 「pending の id はUPDATEして200 + changed:true」の3分岐を確認する。
// proposals.outcome 以外はUPDATEしていないことも問い合わせ文字列で確認する。

function makeSql(handler) {
  return async (strings, ...values) => handler(strings.join("?"), values);
}

describe("POST /api/proposals/{id}/skip", () => {
  it("存在しない id なら404を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [];
          throw new Error(`想定していない問い合わせ: ${query}`);
        }),
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({ params: { id: "999" }, env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(404);
  });

  it("id がそもそも数字でなければ、データベースに触らず404を返す", async () => {
    vi.resetModules();
    let called = false;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql(() => {
          called = true;
          throw new Error("呼ばれてはいけない");
        }),
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({ params: { id: "abc" }, env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(404);
    expect(called).toBe(false);
  });

  it("既に pending でない提案は、エラーにせず changed:false を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [{ id: 3, outcome: "skipped" }];
          throw new Error(`UPDATEされてはいけない: ${query}`);
        }),
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({ params: { id: "3" }, env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.changed).toBe(false);
    expect(body.outcome).toBe("skipped");
  });

  it("pending の提案は outcome を skipped に更新し、changed:true を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [{ id: 3, outcome: "pending" }];
          if (query.includes("UPDATE proposals")) return [{ outcome: "skipped" }];
          throw new Error(`想定していない問い合わせ: ${query}`);
        }),
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({ params: { id: "3" }, env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.changed).toBe(true);
    expect(body.outcome).toBe("skipped");
  });

  it("データベースへの問い合わせが失敗したら、内部の詳細を含めずに500を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => async () => {
        throw new Error("connection to postgres://user:secret@host/db failed");
      },
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({ params: { id: "3" }, env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(500);
    const body = await res.json();
    expect(Object.keys(body)).toEqual(["message"]);
    expect(body.message).not.toContain("postgres://");
  });
});
