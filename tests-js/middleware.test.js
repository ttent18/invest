import { describe, expect, it } from "vitest";

import { onRequest } from "../functions/_middleware.js";

// 合言葉を base64 にして、ブラウザが送るのと同じ形の見出しを作る
function authHeader(user, password) {
  const bytes = new TextEncoder().encode(`${user}:${password}`);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return `Basic ${btoa(binary)}`;
}

function call({ authorization, password = "ひみつの合言葉-abc123" } = {}) {
  const headers = new Headers();
  if (authorization !== undefined) headers.set("Authorization", authorization);

  let passedThrough = false;
  const next = async () => {
    passedThrough = true;
    return new Response("中身", { status: 200 });
  };

  return onRequest({
    request: new Request("https://example.pages.dev/api/state", { headers }),
    env: password === null ? {} : { APP_PASSWORD: password },
    next,
  }).then((response) => ({ response, passedThrough: () => passedThrough }));
}

describe("合言葉の確認", () => {
  it("合言葉が合っていれば、中身を返す", async () => {
    const { response, passedThrough } = await call({
      authorization: authHeader("だれでも", "ひみつの合言葉-abc123"),
    });

    expect(response.status).toBe(200);
    expect(passedThrough()).toBe(true);
    expect(await response.text()).toBe("中身");
  });

  it("利用者名は何でも通る（覚えるものを増やさないため）", async () => {
    const { response } = await call({
      authorization: authHeader("", "ひみつの合言葉-abc123"),
    });
    expect(response.status).toBe(200);

    const other = await call({
      authorization: authHeader("まったく別の名前", "ひみつの合言葉-abc123"),
    });
    expect(other.response.status).toBe(200);
  });

  it("合言葉が違えば 401 で、中身まで届かない", async () => {
    const { response, passedThrough } = await call({
      authorization: authHeader("だれでも", "ちがう合言葉"),
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
    expect(response.headers.get("www-authenticate")).toContain("Basic");
  });

  it("合言葉の前半だけ合っていても通らない", async () => {
    const { response, passedThrough } = await call({
      authorization: authHeader("だれでも", "ひみつの合言葉-abc12"),
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("合言葉を1文字だけ足しても通らない", async () => {
    const { response, passedThrough } = await call({
      authorization: authHeader("だれでも", "ひみつの合言葉-abc1234"),
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("Authorization が無ければ 401 で、合言葉を聞く見出しを付ける", async () => {
    const { response, passedThrough } = await call({});

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
    expect(response.headers.get("www-authenticate")).toContain("Basic");
  });

  it("Basic 以外の方式は通らない", async () => {
    const { response, passedThrough } = await call({
      // HTTPの見出しには日本語をそのまま入れられないので、ここだけ英数字
      authorization: "Bearer some-token-value",
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("壊れた base64 でも、落ちずに 401 を返す", async () => {
    const { response, passedThrough } = await call({
      authorization: "Basic !!!!not-valid-base64!!!!",
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("区切りの : が無ければ通らない", async () => {
    const { response, passedThrough } = await call({
      authorization: `Basic ${btoa("no-colon-here")}`,
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("合言葉に : が入っていても、そのまま比べる", async () => {
    const { response } = await call({
      authorization: authHeader("だれでも", "a:b:c"),
      password: "a:b:c",
    });

    expect(response.status).toBe(200);
  });

  // 合言葉が設定されていないとき、いちばん危ないのは
  // 「空の合言葉を送られる」場合。未設定を「空文字」と同じに扱ってしまうと、
  // 誰でも空のまま通れてしまう。だからここは空を送って確かめる。
  it("【安全側】合言葉が設定されていなければ、空の合言葉でも開かない", async () => {
    const { response, passedThrough } = await call({
      authorization: authHeader("だれでも", ""),
      password: null,
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("【安全側】合言葉が設定されていなければ、何を送っても開かない", async () => {
    for (const attempt of ["なんでも", "undefined", "null", "だれでも"]) {
      const { response, passedThrough } = await call({
        authorization: authHeader("だれでも", attempt),
        password: null,
      });

      expect(response.status).toBe(401);
      expect(passedThrough()).toBe(false);
    }
  });

  it("【安全側】合言葉が空文字なら、空文字を送っても開かない", async () => {
    const { response, passedThrough } = await call({
      authorization: authHeader("だれでも", ""),
      password: "",
    });

    expect(response.status).toBe(401);
    expect(passedThrough()).toBe(false);
  });

  it("断るときの本文に、正しい合言葉が混ざっていない", async () => {
    const { response } = await call({
      authorization: authHeader("だれでも", "ちがう合言葉"),
    });

    const body = await response.text();
    expect(body).not.toContain("ひみつの合言葉-abc123");
    expect(body).not.toContain("APP_PASSWORD=");
  });
});
