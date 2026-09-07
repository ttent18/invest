import { describe, expect, it } from "vitest";
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
});
