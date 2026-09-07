import { describe, expect, it, vi } from "vitest";

// @neondatabase/serverless の neon() を丸ごと差し替える。
// 「存在しない id は404」「既に pending でない id は200 + changed:false」
// 「pending の id はUPDATEして200 + changed:true」の3分岐を確認する。
//
// makeSql は問い合わせの文字列だけでなく、実際に束縛された値も
// sql.calls に記録する。UPDATE から WHERE id の条件を落としても
// （＝pending の提案を全部「買った」ことにしてしまう不具合が起きても）
// 以前のテストは気づかなかった。「pending の提案は…」のテストで、
// UPDATE の問い合わせ文字列に "WHERE id = ?" が含まれること、
// SET句が outcome 以外の列に触れていないこと、id が実際に束縛される
// 値であることを、問い合わせ文字列と束縛値の両方で確認する。

function makeSql(handler) {
  const calls = [];
  const sql = async (strings, ...values) => {
    const query = strings.join("?");
    calls.push({ query, values });
    return handler(query, values);
  };
  sql.calls = calls;
  return sql;
}

function findCall(sql, substring) {
  return sql.calls.find((c) => c.query.includes(substring));
}

// 本文を返す request のダミー。呼び出し側で request を渡さないテストも
// 残す（本文を一切送らない場合の挙動を確認するため）。
function req(body) {
  return { json: async () => body };
}

function brokenReq() {
  return { json: async () => { throw new Error("bad json"); } };
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

  it("pending の提案は outcome を skipped に更新し、changed:true を返す（UPDATEがidを条件にしていること・束縛値も確認）", async () => {
    vi.resetModules();
    let sql;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => {
        sql = makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [{ id: 3, outcome: "pending" }];
          if (query.includes("UPDATE proposals")) return [{ outcome: "skipped" }];
          throw new Error(`想定していない問い合わせ: ${query}`);
        });
        return sql;
      },
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({ params: { id: "3" }, env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.changed).toBe(true);
    expect(body.outcome).toBe("skipped");

    // ここが今回の修正の核心: WHERE に id の条件が実在し、その id が
    // 実際に束縛されていること。これが無いと、pending の提案が
    // 全部「買った」ことになってしまう事故を検知できない。
    // request を渡していないので、理由（skip_reason）は null で束縛される。
    const updateCall = findCall(sql, "UPDATE proposals");
    expect(updateCall.query).toContain("WHERE id = ? AND outcome = 'pending'");
    expect(updateCall.values).toEqual([null, 3]);
    // SET句が outcome と skip_reason 以外の列に触れていないことも文字列で確認する。
    expect(updateCall.query).toContain("SET outcome = 'skipped', skip_reason = ?");
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

  // --- ここから: 「見送る理由」が黙って捨てられていないことの確認 ---
  // (持ち越し1) web/index.html は { reason } を本文で送ってくる。
  // これまでは skip.js が本文を一切読んでおらず、理由が黙って捨てられていた。

  it("理由を入れて見送ると、skip_reasonとして一緒に保存される", async () => {
    vi.resetModules();
    let sql;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => {
        sql = makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [{ id: 3, outcome: "pending" }];
          if (query.includes("UPDATE proposals")) return [{ outcome: "skipped" }];
          throw new Error(`想定していない問い合わせ: ${query}`);
        });
        return sql;
      },
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({
      params: { id: "3" },
      request: req({ reason: "値動きが怖くて手が出せなかった" }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(200);
    const updateCall = findCall(sql, "UPDATE proposals");
    expect(updateCall.values).toEqual(["値動きが怖くて手が出せなかった", 3]);
  });

  it("理由が無くても（本文に reason を含めなくても）見送れる", async () => {
    vi.resetModules();
    let sql;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => {
        sql = makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [{ id: 3, outcome: "pending" }];
          if (query.includes("UPDATE proposals")) return [{ outcome: "skipped" }];
          throw new Error(`想定していない問い合わせ: ${query}`);
        });
        return sql;
      },
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({
      params: { id: "3" },
      request: req({}),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.changed).toBe(true);
    const updateCall = findCall(sql, "UPDATE proposals");
    expect(updateCall.values).toEqual([null, 3]);
  });

  it("本文が壊れたJSONでも、理由の読み取りの失敗が見送り本体を巻き添えにせず、見送りは成功する", async () => {
    vi.resetModules();
    let sql;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => {
        sql = makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [{ id: 3, outcome: "pending" }];
          if (query.includes("UPDATE proposals")) return [{ outcome: "skipped" }];
          throw new Error(`想定していない問い合わせ: ${query}`);
        });
        return sql;
      },
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({
      params: { id: "3" },
      request: brokenReq(),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.changed).toBe(true);
    const updateCall = findCall(sql, "UPDATE proposals");
    expect(updateCall.values).toEqual([null, 3]);
  });

  it("理由が長すぎる（1000文字を超える）ときは、データベースに触らず400と日本語で返す", async () => {
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

    const res = await onRequestPost({
      params: { id: "3" },
      request: req({ reason: "あ".repeat(1001) }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.message).toMatch(/[ぁ-んァ-ヶ一-龠]/);
    expect(called).toBe(false);
  });

  it("理由がちょうど1000文字なら通る（境界値）", async () => {
    vi.resetModules();
    let sql;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => {
        sql = makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [{ id: 3, outcome: "pending" }];
          if (query.includes("UPDATE proposals")) return [{ outcome: "skipped" }];
          throw new Error(`想定していない問い合わせ: ${query}`);
        });
        return sql;
      },
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const reason = "あ".repeat(1000);
    const res = await onRequestPost({
      params: { id: "3" },
      request: req({ reason }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(200);
    const updateCall = findCall(sql, "UPDATE proposals");
    expect(updateCall.values).toEqual([reason, 3]);
  });

  it("空白だけの理由は、無かったことと同じ扱いになる（null で保存）", async () => {
    vi.resetModules();
    let sql;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => {
        sql = makeSql((query) => {
          if (query.includes("SELECT id, outcome")) return [{ id: 3, outcome: "pending" }];
          if (query.includes("UPDATE proposals")) return [{ outcome: "skipped" }];
          throw new Error(`想定していない問い合わせ: ${query}`);
        });
        return sql;
      },
    }));
    const { onRequestPost } = await import("../functions/api/proposals/[id]/skip.js");

    const res = await onRequestPost({
      params: { id: "3" },
      request: req({ reason: "   " }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(200);
    const updateCall = findCall(sql, "UPDATE proposals");
    expect(updateCall.values).toEqual([null, 3]);
  });
});
