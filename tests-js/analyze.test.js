import { afterEach, describe, expect, it, vi } from "vitest";

// global fetch を差し替えて、本物の GitHub には一切繋がずに
// onRequest の分岐（204成功・401/403/404/その他失敗・トークン未設定・
// fetch自体の失敗・POST以外）を確認する。

function req(method = "POST") {
  return { method };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("POST /api/analyze", () => {
  it("GitHubが204を返したら、成功のメッセージを返す（200で判定しない）", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const { onRequest } = await import("../functions/api/analyze.js");

    const res = await onRequest({ request: req(), env: { GITHUB_TOKEN: "t1" } });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.message).toBe("分析を始めました。数分かかります");
  });

  it("GITHUB_TOKEN が env から使われ、画面には一切含まれない", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const { onRequest } = await import("../functions/api/analyze.js");

    const res = await onRequest({ request: req(), env: { GITHUB_TOKEN: "super-secret-token" } });

    const [, options] = fetchMock.mock.calls[0];
    expect(options.headers.Authorization).toBe("Bearer super-secret-token");

    const bodyText = await res.clone().text();
    expect(bodyText).not.toContain("super-secret-token");
  });

  it("GITHUB_TOKEN が未設定なら、fetchせずに設定不足だと分かる日本語を返す", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { onRequest } = await import("../functions/api/analyze.js");

    const res = await onRequest({ request: req(), env: {} });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(res.status).toBe(500);
    const body = await res.json();
    expect(body.message).toContain("設定");
  });

  it.each([401, 403])("GitHubが%dを返したら、トークンの確認を促す日本語を返し、本文はそのまま返さない", async (status) => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ message: "Bad credentials", documentation_url: "https://x" }), { status }));
    vi.stubGlobal("fetch", fetchMock);
    const { onRequest } = await import("../functions/api/analyze.js");

    const res = await onRequest({ request: req(), env: { GITHUB_TOKEN: "t1" } });

    expect(res.status).not.toBe(200);
    const body = await res.json();
    expect(body.message).toContain("トークンの設定を確認してください");
    expect(body.message).not.toContain("Bad credentials");
    expect(body.message).not.toContain("documentation_url");
  });

  it("GitHubが404を返したら、設定が見つからない旨の日本語を返す", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ message: "Not Found" }), { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    const { onRequest } = await import("../functions/api/analyze.js");

    const res = await onRequest({ request: req(), env: { GITHUB_TOKEN: "t1" } });

    const body = await res.json();
    expect(body.message).toBe("分析の設定が見つかりません");
  });

  it("GitHubが500などその他の失敗を返したら、始められなかった旨の日本語を返す", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ message: "Internal Server Error" }), { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);
    const { onRequest } = await import("../functions/api/analyze.js");

    const res = await onRequest({ request: req(), env: { GITHUB_TOKEN: "t1" } });

    const body = await res.json();
    expect(body.message).toBe("分析を始められませんでした");
    expect(body.message).not.toContain("Internal Server Error");
  });

  it("fetch自体が失敗した（ネットワーク断など）ら、例外の中身を含めずに日本語で返す", async () => {
    const fetchMock = vi.fn(async () => {
      throw new Error("connect ECONNREFUSED 1.2.3.4:443 secret-detail");
    });
    vi.stubGlobal("fetch", fetchMock);
    const { onRequest } = await import("../functions/api/analyze.js");

    const res = await onRequest({ request: req(), env: { GITHUB_TOKEN: "t1" } });

    expect(res.status).toBe(500);
    const body = await res.json();
    expect(body.message).not.toContain("ECONNREFUSED");
    expect(body.message).not.toContain("secret-detail");
    expect(body.message.length).toBeGreaterThan(0);
  });

  it("POST以外のメソッドは、fetchせずに405と「この操作はできません」を返す", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { onRequest } = await import("../functions/api/analyze.js");

    const res = await onRequest({ request: req("GET"), env: { GITHUB_TOKEN: "t1" } });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(res.status).toBe(405);
    const body = await res.json();
    expect(body.message).toBe("この操作はできません");
  });
});
