import { describe, expect, it } from "vitest";
import { validateFill } from "../functions/_shared/validate.js";

const good = (over = {}) => ({
  client_key: "k1", proposal_id: 3, symbol: "156A.T", side: "buy",
  quantity: 100, price: 899, fee: 0, ...over,
});

describe("記録の検証", () => {
  it("正しい買いは通る", () => {
    expect(validateFill(good())).toEqual([]);
  });

  it("日本株の株数が100の倍数でなければ弾く", () => {
    const errors = validateFill(good({ quantity: 137 }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("100株");
  });

  it("米国株は100の倍数でなくてよい", () => {
    expect(validateFill(good({ symbol: "AAPL", quantity: 137 }))).toEqual([]);
  });

  it("買いに提案が紐づいていなければ弾く", () => {
    const errors = validateFill(good({ proposal_id: null }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("枠");
  });

  it("売りは提案が無くてよい", () => {
    expect(validateFill(good({ side: "sell", proposal_id: null }))).toEqual([]);
  });

  it("株数が0や負の数なら弾く", () => {
    expect(validateFill(good({ quantity: 0 })).length).toBe(1);
    expect(validateFill(good({ quantity: -100 })).length).toBe(1);
  });

  it("値段が0以下なら弾く", () => {
    expect(validateFill(good({ price: 0 })).length).toBe(1);
  });

  it("二重送信よけの鍵が無ければ弾く", () => {
    expect(validateFill(good({ client_key: "" })).length).toBe(1);
  });

  it("誤りが複数あれば全部返す", () => {
    const errors = validateFill(good({ quantity: 137, price: -1 }));
    expect(errors.length).toBe(2);
  });

  it("side が buy でも sell でもなければ弾く", () => {
    const errors = validateFill(good({ side: "hold" }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("side");
  });

  it("銘柄コードが無ければ弾く", () => {
    expect(validateFill(good({ symbol: "" })).length).toBe(1);
  });

  it("取引日時が省略されていれば通る", () => {
    expect(validateFill(good({ traded_at: undefined }))).toEqual([]);
  });

  it("取引日時が読めない値なら弾く", () => {
    const errors = validateFill(good({ traded_at: "先週のいつか" }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("日時");
  });

  it("価格が数値でなければ弾く", () => {
    expect(validateFill(good({ price: "899" })).length).toBe(1);
  });
});
