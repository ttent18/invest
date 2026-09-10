import { describe, expect, it } from "vitest";

import { onRequest } from "../functions/_middleware.js";
import {
  COOKIE_NAME,
  isValidSession,
  issueSession,
  readSessionCookie,
  sessionCookieHeader,
} from "../functions/_shared/session.js";

const PASSWORD = "ひみつの合言葉-abc123";

function call({
  path = "/api/state",
  method = "GET",
  headers = {},
  body,
  password = PASSWORD,
} = {}) {
  let passedThrough = false;
  const next = async () => {
    passedThrough = true;
    return new Response("中身", { status: 200 });
  };

  const request = new Request(`https://example.pages.dev${path}`, {
    method,
    headers,
    body,
  });

  return onRequest({
    request,
    env: password === null ? {} : { APP_PASSWORD: password },
    next,
  }).then((response) => ({ response, passedThrough: () => passedThrough }));
}

// 人が画面を開くときにブラウザが送る見出し
const NAVIGATION = { "Sec-Fetch-Mode": "navigate", Accept: "text/html" };

function form(fields) {
  const body = new URLSearchParams(fields).toString();
  return {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  };
}

function cookieHeader(token) {
  return { Cookie: `${COOKIE_NAME}=${encodeURIComponent(token)}` };
}

describe("合言葉の門番", () => {
  it("印が無いまま画面を開こうとしたら、合言葉の画面を出す（中身は返さない）", async () => {
    const { response, passedThrough } = await call({ path: "/", headers: NAVIGATION });

    expect(response.status).toBe(200);
    expect(passedThrough()).toBe(false);
    const html = await response.text();
    expect(html).toContain("合言葉を入れてください");
    expect(html).toContain('type="password"');
  });

  // ここがこの作り直しの理由そのもの。iPhone のホーム画面のアプリでは
  // ブラウザの小さな窓が出ないため、401 を返すだけでは合言葉を入れる
  // 場所が存在しなくなる。**画面を開く要求には、必ず入力できる画面を返す。**
  it("画面を開く要求に、入力できない 401 を返さない", async () => {
    const { response } = await call({ path: "/holdings", headers: NAVIGATION });

    expect(response.status).not.toBe(401);
    expect(response.headers.get("content-type")).toContain("text/html");
  });

  it("印が無いまま API を呼んだら、JSON で「入れ直して」と返す", async () => {
    const { response, passedThrough } = await call({
      path: "/api/state",
      headers: { Accept: "application/json", "Sec-Fetch-Mode": "cors" },
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
    const body = await response.json();
    expect(body.reason).toBe("login_required");
    expect(body.message).toContain("合言葉");
  });

  it("合言葉が合っていれば、印を渡して元の場所へ戻す", async () => {
    const { response, passedThrough } = await call({
      path: "/api/login",
      ...form({ password: PASSWORD, next: "/holdings" }),
    });

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/holdings");
    expect(passedThrough()).toBe(false);

    const setCookie = response.headers.get("set-cookie");
    expect(setCookie).toContain(`${COOKIE_NAME}=`);
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("Secure");
    expect(setCookie).toContain("SameSite=Lax");
  });

  it("受け取った印で、次からは中身が返る", async () => {
    const login = await call({
      path: "/api/login",
      ...form({ password: PASSWORD, next: "/" }),
    });
    const token = readSessionCookie(login.response.headers.get("set-cookie"));

    const { response, passedThrough } = await call({
      path: "/api/state",
      headers: cookieHeader(token),
    });

    expect(response.status).toBe(200);
    expect(passedThrough()).toBe(true);
    expect(await response.text()).toBe("中身");
  });

  it("合言葉が違えば、印を渡さず、もう一度入力できる画面を出す", async () => {
    const { response, passedThrough } = await call({
      path: "/api/login",
      ...form({ password: "ちがう合言葉", next: "/" }),
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
    expect(response.headers.get("set-cookie")).toBeNull();
    const html = await response.text();
    expect(html).toContain("合言葉が違います");
    expect(html).toContain('type="password"');
  });

  it("合言葉の前半だけ合っていても通らない", async () => {
    const { response } = await call({
      path: "/api/login",
      ...form({ password: "ひみつの合言葉-abc12", next: "/" }),
    });

    expect(response.status).toBe(401);
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("JSON で送っても合言葉を受け取れる", async () => {
    const { response } = await call({
      path: "/api/login",
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: PASSWORD, next: "/" }),
    });

    expect(response.status).toBe(303);
    expect(response.headers.get("set-cookie")).toContain(`${COOKIE_NAME}=`);
  });

  it("壊れた本文でも落ちず、入力できる画面を出す", async () => {
    const { response, passedThrough } = await call({
      path: "/api/login",
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{これはJSONではない",
    });

    expect(response.status).toBe(400);
    expect(passedThrough()).toBe(false);
    expect(await response.text()).toContain('type="password"');
  });

  // 戻り先は利用者が書き換えられる。他所のサイトへ飛ばす踏み台にされないこと。
  it("戻り先に他所のサイトを指定されても、自分の中に留まる", async () => {
    for (const evil of ["//evil.example.com", "https://evil.example.com", "javascript:alert(1)"]) {
      const { response } = await call({
        path: "/api/login",
        ...form({ password: PASSWORD, next: evil }),
      });

      expect(response.status).toBe(303);
      expect(response.headers.get("location")).toBe("/");
    }
  });

  it("印を書き換えたものは受け付けない", async () => {
    const token = await issueSession(PASSWORD);
    const [expiresAt, sig] = token.split(".");
    // 期限だけを100年後に伸ばす（署名はそのまま）
    const forged = `${Number(expiresAt) + 100 * 365 * 24 * 60 * 60}.${sig}`;

    const { response, passedThrough } = await call({
      path: "/api/state",
      headers: cookieHeader(forged),
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("別の合言葉で作られた印は受け付けない（合言葉を変えたら入り直しになる）", async () => {
    const token = await issueSession("むかしの合言葉");

    const { response, passedThrough } = await call({
      path: "/api/state",
      headers: cookieHeader(token),
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("【安全側】合言葉が設定されていなければ、正しい印を持っていても開かない", async () => {
    const token = await issueSession(PASSWORD);

    const { response, passedThrough } = await call({
      path: "/api/state",
      headers: cookieHeader(token),
      password: null,
    });

    expect(response.status).toBe(503);
    expect(passedThrough()).toBe(false);
  });

  it("【安全側】合言葉が設定されていなければ、合言葉を送っても印をもらえない", async () => {
    for (const attempt of ["", "なんでも", "undefined"]) {
      const { response, passedThrough } = await call({
        path: "/api/login",
        ...form({ password: attempt, next: "/" }),
        password: null,
      });

      expect(response.status).toBe(503);
      expect(response.headers.get("set-cookie")).toBeNull();
      expect(passedThrough()).toBe(false);
    }
  });

  it("合言葉の画面に、正しい合言葉が混ざっていない", async () => {
    const { response } = await call({
      path: "/api/login",
      ...form({ password: "ちがう合言葉", next: "/" }),
    });

    const html = await response.text();
    expect(html).not.toContain(PASSWORD);
    expect(html).not.toContain("APP_PASSWORD=");
  });

  it("合言葉の画面が、途中の仕組みに残らない", async () => {
    const { response } = await call({ path: "/", headers: NAVIGATION });
    expect(response.headers.get("cache-control")).toContain("no-store");
  });
});

describe("印（クッキー）そのもの", () => {
  it("期限が切れていれば通らない", async () => {
    const token = await issueSession(PASSWORD);
    const farFuture = Date.now() + 91 * 24 * 60 * 60 * 1000;

    expect(await isValidSession(token, PASSWORD, farFuture)).toBe(false);
    expect(await isValidSession(token, PASSWORD, Date.now())).toBe(true);
  });

  it("形が壊れていても落ちない", async () => {
    for (const broken of [null, undefined, "", "区切りなし", ".", "abc.def", "12x3.sig"]) {
      expect(await isValidSession(broken, PASSWORD)).toBe(false);
    }
  });

  it("Cookie の見出しから、この仕組みの印だけを取り出す", () => {
    expect(readSessionCookie(`other=1; ${COOKIE_NAME}=abc.def; another=2`)).toBe("abc.def");
    expect(readSessionCookie("other=1")).toBeNull();
    expect(readSessionCookie(null)).toBeNull();
  });

  it("指定に HttpOnly / Secure / SameSite が入っている", () => {
    const header = sessionCookieHeader("abc.def");
    expect(header).toContain("HttpOnly");
    expect(header).toContain("Secure");
    expect(header).toContain("SameSite=Lax");
    expect(header).toContain("Path=/");
  });
});
