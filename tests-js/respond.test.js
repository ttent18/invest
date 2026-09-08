import { describe, expect, it } from "vitest";
import { fail, ok } from "../functions/_shared/respond.js";

describe("応答の組み立て", () => {
  it("成功はJSONと200を返す", async () => {
    const res = ok({ a: 1 });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("application/json");
    expect(await res.json()).toEqual({ a: 1 });
  });

  it("失敗は理由を日本語で返す", async () => {
    const res = fail(400, "株数は100株単位で入れてください");
    expect(res.status).toBe(400);
    expect((await res.json()).message).toBe("株数は100株単位で入れてください");
  });

  it("失敗の応答に内部の詳細を混ぜない", async () => {
    // 画面に出す文言だけを返す。データベースのエラー本文などを渡さない
    const res = fail(500, "保存できませんでした");
    const body = await res.json();
    expect(Object.keys(body)).toEqual(["message"]);
  });
});
