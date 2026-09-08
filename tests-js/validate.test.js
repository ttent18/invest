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

  // --- proposal_id の型検証（レビュー指摘: Important 4） ---
  // "abc" / "3"（数字の文字列）/ 1.5 / {} / "" は、見た目は色々でも
  // どれも BIGINT の列にそのまま入れると変換エラーになる値。
  // 「未指定」（null/undefined）とは別の理由として弾く。

  it("proposal_idが数字として読めない文字列なら弾く", () => {
    const errors = validateFill(good({ proposal_id: "abc" }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("提案");
  });

  it("proposal_idが数字の見た目でも文字列なら弾く", () => {
    const errors = validateFill(good({ proposal_id: "3" }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("提案");
  });

  it("proposal_idが小数なら弾く", () => {
    const errors = validateFill(good({ proposal_id: 1.5 }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("提案");
  });

  it("proposal_idがオブジェクトなら弾く", () => {
    const errors = validateFill(good({ proposal_id: {} }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("提案");
  });

  it("proposal_idが空文字なら弾く", () => {
    const errors = validateFill(good({ proposal_id: "" }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("提案");
  });

  it("proposal_idが0や負の整数なら弾く", () => {
    expect(validateFill(good({ proposal_id: 0 })).length).toBe(1);
    expect(validateFill(good({ proposal_id: -1 })).length).toBe(1);
  });

  it("売りでもproposal_idが不正な型なら弾く（未指定は許すが、指定された値がおかしいのは別）", () => {
    const errors = validateFill(good({ side: "sell", proposal_id: "abc" }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("提案");
  });

  // --- fee の検証（レビュー指摘: Critical 2） ---
  // マイナスの手数料は、Python側の計算で現金を黙って増やしてしまう。

  it("feeが省略されていれば通る", () => {
    expect(validateFill(good({ fee: undefined }))).toEqual([]);
  });

  it("feeがマイナスなら弾く", () => {
    const errors = validateFill(good({ fee: -500 }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("手数料");
  });

  it("feeが数値でなければ弾く", () => {
    expect(validateFill(good({ fee: "500" })).length).toBe(1);
  });

  // --- symbol の大文字・小文字（レビュー指摘: Minor） ---

  it("銘柄コードの.Tが小文字でも日本株として100株単位を適用する", () => {
    const errors = validateFill(good({ symbol: "7203.t", quantity: 137 }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("100株");
  });

  // --- traded_at の型（レビュー指摘: Minor / isReadableDate） ---
  // new Date(12345) や new Date(true) は「有効な日時」になってしまうため、
  // 数値・真偽値は日時としては受け付けない（文字列だけを許す）。

  it("取引日時が数値なら弾く", () => {
    const errors = validateFill(good({ traded_at: 12345 }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("日時");
  });

  it("取引日時が真偽値なら弾く", () => {
    const errors = validateFill(good({ traded_at: true }));
    expect(errors.length).toBe(1);
    expect(errors[0]).toContain("日時");
  });

  it("取引日時が空文字なら省略として通る（空欄のまま送信できる画面のため）", () => {
    expect(validateFill(good({ traded_at: "" }))).toEqual([]);
  });
});
