import { describe, expect, it, vi } from "vitest";

// @neondatabase/serverless の neon() を丸ごと差し替える。本物のデータベースには
// 一切繋がず、「onRequestPost が正しい問い合わせを組み立て、正しい形で
// 返しているか」「検証で弾いたときに何もINSERTしないか」「同じ client_key
// で2回来たときにエラーにならないか」だけを見る。
//
// makeSql は、呼ばれた問い合わせの文字列だけでなく、実際に束縛された値
// （タグ付きテンプレートの ${...} に入った中身）も sql.calls に記録する。
// レビューで指摘された通り、以前はここを一度も見ておらず、INSERTの
// 列を1つ削っても・列順を入れ替えても・fee の既定値を変えてもテストは
// 全部グリーンのままだった。列名の並びと、実際に束縛された値の両方を
// 確認することで、それらの崩れを検知できるようにする。

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

// INSERT の列名リスト（並び順まで含む）。列を1つ削ったり、順番を
// 入れ替えたりすると、この文字列と一致しなくなる。
const INSERT_COLUMNS = "INSERT INTO fills (proposal_id, symbol, side, quantity, price, currency, fee, traded_at, client_key)";

function mockNeon(handler) {
  let sql;
  vi.doMock("@neondatabase/serverless", () => ({
    neon: () => {
      sql = makeSql(handler);
      return sql;
    },
  }));
  return () => sql;
}

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
    // message（1本の文字列）だけでなく、理由ごとに分かれた配列も返す。
    expect(body.errors).toEqual([expect.stringContaining("100株")]);
    expect(called).toBe(false);
  });

  it("新規の client_key ならINSERTして created:true を返す", async () => {
    vi.resetModules();
    const getSql = mockNeon((query) => {
      if (query.includes("SELECT id FROM proposals")) return [{ id: 3 }];
      if (query.includes("INSERT INTO fills")) {
        return [
          { id: 42, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "2026-09-08T01:00:00Z" },
        ];
      }
      throw new Error(`想定していない問い合わせ: ${query}`);
    });
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
          if (query.includes("SELECT id FROM proposals")) return [{ id: 3 }];
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

  // --- ここから、SQLの列と束縛値そのものを確認するテスト（レビュー指摘: Critical 1） ---

  it("INSERTの列名リストが想定どおりであること（列の削除・並び替えを検知する）", async () => {
    vi.resetModules();
    const getSql = mockNeon((query) => {
      if (query.includes("SELECT id FROM proposals")) return [{ id: 3 }];
      if (query.includes("INSERT INTO fills")) return [{ id: 1, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "now" }];
      throw new Error(`想定していない問い合わせ: ${query}`);
    });
    const { onRequestPost } = await import("../functions/api/fills.js");

    await onRequestPost({ request: req(goodBody()), env: { DATABASE_URL: "postgres://dummy" } });

    const insertCall = findCall(getSql(), "INSERT INTO fills");
    expect(insertCall.query).toContain(INSERT_COLUMNS);
  });

  it("日本株（.T終わり）なら currency に JPY が束縛されること", async () => {
    vi.resetModules();
    const getSql = mockNeon((query) => {
      if (query.includes("SELECT id FROM proposals")) return [{ id: 3 }];
      if (query.includes("INSERT INTO fills")) return [{ id: 1, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "now" }];
      throw new Error(`想定していない問い合わせ: ${query}`);
    });
    const { onRequestPost } = await import("../functions/api/fills.js");

    await onRequestPost({ request: req(goodBody()), env: { DATABASE_URL: "postgres://dummy" } });

    const insertCall = findCall(getSql(), "INSERT INTO fills");
    // 列順は proposal_id, symbol, side, quantity, price, currency, fee, traded_at, client_key
    expect(insertCall.values[5]).toBe("JPY");
  });

  it("日本株でない銘柄なら currency に USD が束縛されること", async () => {
    vi.resetModules();
    const getSql = mockNeon((query) => {
      if (query.includes("INSERT INTO fills")) return [{ id: 1, symbol: "AAPL", side: "buy", quantity: 100, price: "10", fee: "0", traded_at: null, recorded_at: "now" }];
      throw new Error(`想定していない問い合わせ: ${query}`);
    });
    const { onRequestPost } = await import("../functions/api/fills.js");

    // 米国株は proposal_id を省略できる（売りではないが、ここでは
    // currency 判定だけを見たいので売りにして proposal_id 確認を避ける）。
    await onRequestPost({
      request: req({ ...goodBody(), symbol: "AAPL", side: "sell", proposal_id: null }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    const insertCall = findCall(getSql(), "INSERT INTO fills");
    expect(insertCall.values[5]).toBe("USD");
  });

  it("feeを省略したときは0が束縛されること", async () => {
    vi.resetModules();
    const getSql = mockNeon((query) => {
      if (query.includes("SELECT id FROM proposals")) return [{ id: 3 }];
      if (query.includes("INSERT INTO fills")) return [{ id: 1, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "now" }];
      throw new Error(`想定していない問い合わせ: ${query}`);
    });
    const { onRequestPost } = await import("../functions/api/fills.js");

    const body = goodBody();
    expect(body.fee).toBeUndefined(); // goodBody() は fee を含まない

    await onRequestPost({ request: req(body), env: { DATABASE_URL: "postgres://dummy" } });

    const insertCall = findCall(getSql(), "INSERT INTO fills");
    expect(insertCall.values[6]).toBe(0);
  });

  it("client_keyが本文で渡した値そのままで束縛されること", async () => {
    vi.resetModules();
    const getSql = mockNeon((query) => {
      if (query.includes("SELECT id FROM proposals")) return [{ id: 3 }];
      if (query.includes("INSERT INTO fills")) return [{ id: 1, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "now" }];
      throw new Error(`想定していない問い合わせ: ${query}`);
    });
    const { onRequestPost } = await import("../functions/api/fills.js");

    await onRequestPost({ request: req(goodBody()), env: { DATABASE_URL: "postgres://dummy" } });

    const insertCall = findCall(getSql(), "INSERT INTO fills");
    expect(insertCall.values[8]).toBe("k1");
  });

  it("client_keyの前後の空白は取り除いてから束縛されること（validateの判定とずれないように）", async () => {
    vi.resetModules();
    const getSql = mockNeon((query) => {
      if (query.includes("SELECT id FROM proposals")) return [{ id: 3 }];
      if (query.includes("INSERT INTO fills")) return [{ id: 1, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "now" }];
      throw new Error(`想定していない問い合わせ: ${query}`);
    });
    const { onRequestPost } = await import("../functions/api/fills.js");

    await onRequestPost({ request: req({ ...goodBody(), client_key: "  k1  " }), env: { DATABASE_URL: "postgres://dummy" } });

    const insertCall = findCall(getSql(), "INSERT INTO fills");
    expect(insertCall.values[8]).toBe("k1");
  });

  // --- traded_at が空文字のときに永久に失敗しないことの確認（レビュー指摘: Important 3） ---

  it("traded_atが空文字のときは、nullとして記録され500にならない", async () => {
    vi.resetModules();
    const getSql = mockNeon((query) => {
      if (query.includes("SELECT id FROM proposals")) return [{ id: 3 }];
      if (query.includes("INSERT INTO fills")) return [{ id: 1, symbol: "156A.T", side: "buy", quantity: 100, price: "899", fee: "0", traded_at: null, recorded_at: "now" }];
      throw new Error(`想定していない問い合わせ: ${query}`);
    });
    const { onRequestPost } = await import("../functions/api/fills.js");

    const res = await onRequestPost({ request: req({ ...goodBody(), traded_at: "" }), env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(200);
    const insertCall = findCall(getSql(), "INSERT INTO fills");
    expect(insertCall.values[7]).toBeNull();
  });

  // --- proposal_id の存在確認（レビュー指摘: Important 4・5） ---

  it("存在しないproposal_idのときは400と「見つかりません」を返し、INSERTしない", async () => {
    vi.resetModules();
    let insertCalled = false;
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () =>
        makeSql((query) => {
          if (query.includes("SELECT id FROM proposals")) return [];
          if (query.includes("INSERT INTO fills")) {
            insertCalled = true;
            throw new Error("呼ばれてはいけない");
          }
          throw new Error(`想定していない問い合わせ: ${query}`);
        }),
    }));
    const { onRequestPost } = await import("../functions/api/fills.js");

    const res = await onRequestPost({ request: req(goodBody()), env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.message).toContain("見つかりません");
    expect(insertCalled).toBe(false);
  });

  it("proposal_idの型が不正なときは、検証で弾かれデータベースに一切触らず400を返す", async () => {
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
      request: req({ ...goodBody(), proposal_id: "abc" }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.message).toContain("提案");
    expect(called).toBe(false);
  });

  // --- マイナスの手数料を拒否することの確認（レビュー指摘: Critical 2） ---

  it("feeがマイナスのときは、検証で弾かれデータベースに一切触らず400を返す", async () => {
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
      request: req({ ...goodBody(), fee: -500 }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.message).toContain("手数料");
    expect(called).toBe(false);
  });
});
