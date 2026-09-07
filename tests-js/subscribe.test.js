import { describe, expect, it, vi } from "vitest";
import { validateSubscription } from "../functions/_shared/validate.js";

const good = (over = {}) => ({
  endpoint: "https://web.push.apple.com/abc",
  keys: { p256dh: "k1", auth: "a1" },
  ...over,
});

describe("通知の宛先の検証", () => {
  it("正しい宛先は通る", () => {
    expect(validateSubscription(good())).toEqual([]);
  });

  it("宛先が https でなければ弾く", () => {
    expect(validateSubscription(good({ endpoint: "http://x" })).length).toBe(1);
  });

  it("宛先が無ければ弾く", () => {
    expect(validateSubscription(good({ endpoint: "" })).length).toBe(1);
  });

  it("鍵が欠けていれば弾く", () => {
    expect(validateSubscription(good({ keys: { p256dh: "k1" } })).length).toBe(1);
    expect(validateSubscription(good({ keys: {} })).length).toBe(2);
  });

  it("鍵そのものが無くても落ちない", () => {
    const errors = validateSubscription({ endpoint: "https://x" });
    expect(errors.length).toBeGreaterThan(0);
  });

  it("却下の文言が日本語である", () => {
    const [message] = validateSubscription(good({ endpoint: "" }));
    expect(message).toMatch(/[ぁ-んァ-ヶ一-龠]/);
  });

  // --- p256dh と auth、どちらが欠けているかで文言が違うことの確認（レビュー指摘: Minor 2） ---
  it("p256dhが欠けたときとauthが欠けたときで、文言が異なる", () => {
    const [messageWhenP256dhMissing] = validateSubscription(good({ keys: { auth: "a1" } }));
    const [messageWhenAuthMissing] = validateSubscription(good({ keys: { p256dh: "k1" } }));

    expect(messageWhenP256dhMissing).not.toBe(messageWhenAuthMissing);
    // 画面に出す文言に英語の識別子（p256dh, auth）をそのまま出さない。
    expect(messageWhenP256dhMissing).not.toMatch(/p256dh|auth/i);
    expect(messageWhenAuthMissing).not.toMatch(/p256dh|auth/i);
  });
});

// --- ここから POST /api/subscribe（データベースを差し替えたテスト） ---
//
// makeSql は tests-js/fills.test.js のものと同じ形。問い合わせの文字列
// だけでなく、実際に束縛された値（タグ付きテンプレートの ${...} の中身）
// も sql.calls に記録する。レビューで指摘された通り、この窓口には
// これまでデータベースを差し替えたテストが1本も無く、VALUES の中の
// p256dh と auth を入れ替えても、ON CONFLICT ... DO UPDATE を DO NOTHING
// に変えても、既存のテストは全部グリーンのままだった
// （レビュー指摘: Important 1）。

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

function mockNeon(handler) {
  // db(env) は呼び出しのたびに neon(...) を呼ぶ。同じ sql インスタンスを
  // 使い回すことで、複数回 onRequestPost を呼んだときの calls を
  // まとめて確認できるようにする（1回目の sql を2回目が上書きして
  // 記録が消えてしまわないように）。
  const sql = makeSql(handler);
  vi.doMock("@neondatabase/serverless", () => ({
    neon: () => sql,
  }));
  return () => sql;
}

// 列名リストと VALUES の中身を、列名ごとに1つずつ対応付ける
// （fills.test.js の parseInsertColumns と同じ考え方）。
function parseInsertColumns(query) {
  const match = query.match(/INSERT INTO push_subscriptions \(([^)]+)\)/);
  if (!match) throw new Error("INSERT INTO push_subscriptions の列名リストが見つかりませんでした");
  return match[1].split(",").map((c) => c.trim());
}

const goodSubscription = () => ({
  endpoint: "https://web.push.apple.com/abc",
  keys: { p256dh: "key-p256dh-value", auth: "key-auth-value" },
});

describe("POST /api/subscribe", () => {
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
    const { onRequestPost } = await import("../functions/api/subscribe.js");

    const res = await onRequestPost({
      request: req({ endpoint: "http://not-https", keys: { p256dh: "k1", auth: "a1" } }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    expect(res.status).toBe(400);
    expect(called).toBe(false);
  });

  it("問い合わせにINSERTの列名リストとON CONFLICT DO UPDATEが含まれること", async () => {
    vi.resetModules();
    const getSql = mockNeon(() => []);
    const { onRequestPost } = await import("../functions/api/subscribe.js");

    await onRequestPost({ request: req(goodSubscription()), env: { DATABASE_URL: "postgres://dummy" } });

    const insertCall = findCall(getSql(), "INSERT INTO push_subscriptions");
    expect(insertCall).toBeDefined();
    expect(insertCall.query).toContain("INSERT INTO push_subscriptions (endpoint, p256dh, auth)");
    expect(insertCall.query).toContain("ON CONFLICT (endpoint) DO UPDATE");
  });

  // 列名 → 束縛値の対応。endpoint / p256dh / auth に相異なる3つの値を
  // 使い、それぞれが正しい列の位置に束縛されていることを確認する。
  // VALUES 側だけ ${p256dh} と ${auth} を入れ替える不具合を検知する。
  it("INSERTの列名と束縛値が1対1で対応していること（鍵の取り違えを検知する）", async () => {
    vi.resetModules();
    const getSql = mockNeon(() => []);
    const { onRequestPost } = await import("../functions/api/subscribe.js");

    const body = goodSubscription();
    await onRequestPost({ request: req(body), env: { DATABASE_URL: "postgres://dummy" } });

    const insertCall = findCall(getSql(), "INSERT INTO push_subscriptions");
    const columns = parseInsertColumns(insertCall.query);
    const values = insertCall.values;
    expect(columns.length).toBe(values.length);

    const byColumn = Object.fromEntries(columns.map((col, i) => [col, values[i]]));

    expect(byColumn.endpoint).toBe(body.endpoint);
    expect(byColumn.p256dh).toBe(body.keys.p256dh);
    expect(byColumn.auth).toBe(body.keys.auth);
    // 3つとも別々の値であること（この確認自体が意味を持つための前提）。
    expect(new Set([byColumn.endpoint, byColumn.p256dh, byColumn.auth]).size).toBe(3);
  });

  // 同じ端末から2回登録しても行が増えない、というブリーフの説明を
  // 確かめる（endpoint に UNIQUE 制約があり、ON CONFLICT で対応する
  // 前提。ここでは、問い合わせが「常に同じ1本のINSERT文（ON CONFLICT
  // 付き）」であり、事前にSELECTして分岐したりしていないことを見る）。
  it("同じendpointで2回呼んでも、どちらも同じON CONFLICT付きのINSERT一発で処理される", async () => {
    vi.resetModules();
    const getSql = mockNeon(() => []);
    const { onRequestPost } = await import("../functions/api/subscribe.js");

    const body = goodSubscription();
    await onRequestPost({ request: req(body), env: { DATABASE_URL: "postgres://dummy" } });
    await onRequestPost({ request: req(body), env: { DATABASE_URL: "postgres://dummy" } });

    const sql = getSql();
    const insertCalls = sql.calls.filter((c) => c.query.includes("INSERT INTO push_subscriptions"));
    expect(insertCalls.length).toBe(2);
    for (const call of insertCalls) {
      expect(call.query).toContain("ON CONFLICT (endpoint) DO UPDATE");
    }
  });

  // --- 前後の空白を取り除いてから束縛されることの確認（レビュー指摘: Minor 3） ---
  it("p256dhとauthの前後の空白は取り除いてから束縛されること", async () => {
    vi.resetModules();
    const getSql = mockNeon(() => []);
    const { onRequestPost } = await import("../functions/api/subscribe.js");

    await onRequestPost({
      request: req({
        endpoint: "https://web.push.apple.com/abc",
        keys: { p256dh: "  key-p256dh-value  ", auth: "  key-auth-value  " },
      }),
      env: { DATABASE_URL: "postgres://dummy" },
    });

    const insertCall = findCall(getSql(), "INSERT INTO push_subscriptions");
    const columns = parseInsertColumns(insertCall.query);
    const byColumn = Object.fromEntries(columns.map((col, i) => [col, insertCall.values[i]]));

    expect(byColumn.p256dh).toBe("key-p256dh-value");
    expect(byColumn.auth).toBe("key-auth-value");
  });

  it("正しい宛先を登録するとregistered:trueを返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({ neon: () => makeSql(() => []) }));
    const { onRequestPost } = await import("../functions/api/subscribe.js");

    const res = await onRequestPost({ request: req(goodSubscription()), env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.registered).toBe(true);
  });

  it("データベースへの問い合わせが失敗したら、内部の詳細を含めずに500を返す", async () => {
    vi.resetModules();
    vi.doMock("@neondatabase/serverless", () => ({
      neon: () => async () => {
        throw new Error("connection to postgres://user:secret@host/db failed");
      },
    }));
    const { onRequestPost } = await import("../functions/api/subscribe.js");

    const res = await onRequestPost({ request: req(goodSubscription()), env: { DATABASE_URL: "postgres://dummy" } });

    expect(res.status).toBe(500);
    const body = await res.json();
    expect(Object.keys(body)).toEqual(["message"]);
    expect(body.message).not.toContain("postgres://");
    expect(body.message).not.toContain("secret");
  });
});
