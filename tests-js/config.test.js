import { describe, expect, it } from "vitest";
import { BUCKETS, MAX_POSITIONS, SETTINGS, breakevenWinRate } from "../functions/_shared/config.js";

// このファイルは src/investment/config.py（BUCKETS・SETTINGS）と
// src/investment/sizing.py（required_win_rate）の写し・等価な式を検証する。
// Python 側の値と数式は uv run pytest 側でテスト済み。ここでは
// 「JavaScript 側の写しが、その値・その式のとおりに動くか」だけを見る。

describe("枠の定義（Python の config.py の写し）", () => {
  it("じっくり枠・回転枠がそれぞれ2枠ずつで、合計4銘柄まで持てる", () => {
    expect(BUCKETS.map((b) => b.name)).toEqual(["じっくり", "回転"]);
    expect(BUCKETS.every((b) => b.slots === 2)).toBe(true);
    expect(MAX_POSITIONS).toBe(4);
  });

  it("1銘柄への投入上限と1取引の損失許容率が config.py の SETTINGS と同じ", () => {
    expect(SETTINGS.max_position_pct).toBe(0.25);
    expect(SETTINGS.risk_per_trade_pct).toBe(0.02);
  });
});

describe("損益トントンの勝率（sizing.py の required_win_rate と同じ式）", () => {
  it("じっくり枠: +22%/-8% なら 8/(22+8) ≈ 0.2667", () => {
    const jikkuri = BUCKETS.find((b) => b.name === "じっくり");
    expect(breakevenWinRate(jikkuri)).toBeCloseTo(0.08 / 0.3, 10);
  });

  it("回転枠: +10%/-5% なら 5/(10+5) ≈ 0.3333", () => {
    const kaiten = BUCKETS.find((b) => b.name === "回転");
    expect(breakevenWinRate(kaiten)).toBeCloseTo(0.05 / 0.15, 10);
  });
});
