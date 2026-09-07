import { describe, expect, it } from "vitest";
import { buildState } from "../functions/_shared/state.js";

// buildState は DB から取ってきた行（配列・オブジェクト）を受け取って
// /api/state が返す形に組み立てるだけの、純粋な関数。
// データベースには一切繋がないので、ここで細かく確認できる。

const NOW = new Date("2026-09-08T01:30:00.000Z");

function base(overrides = {}) {
  return {
    now: NOW,
    vapidPublicKey: "BCCgMo8e",
    capital: 550000,
    cashRows: [
      { currency: "JPY", amount: "550000" },
      { currency: "USD", amount: "0" },
    ],
    positionRows: [],
    proposalRows: [],
    performanceRows: [],
    fillRows: [],
    ...overrides,
  };
}

describe("buildState の基本の形", () => {
  it("常に is_virtual: true を返す（いまは仮想資金の検証段階のため）", () => {
    expect(buildState(base()).is_virtual).toBe(true);
  });

  it("generated_at は渡した時刻をISO文字列にしたもの", () => {
    expect(buildState(base()).generated_at).toBe("2026-09-08T01:30:00.000Z");
  });

  it("vapid_public_key はそのまま返す。無ければ null（1件がAPI全体を落とさない）", () => {
    expect(buildState(base()).vapid_public_key).toBe("BCCgMo8e");
    expect(buildState(base({ vapidPublicKey: undefined })).vapid_public_key).toBe(null);
  });

  it("cash は行を { 通貨: 金額 } の形にする", () => {
    expect(buildState(base()).cash).toEqual({ JPY: 550000, USD: 0 });
  });

  it("constraints は枠の定義から出す max_positions を含む", () => {
    expect(buildState(base()).constraints).toEqual({
      max_position_pct: 0.25,
      risk_per_trade_pct: 0.02,
      max_positions: 4,
    });
  });
});

describe("含み損益（last_price が無い保有があっても他の保有は出る）", () => {
  it("last_price が無ければ unrealized_pnl / unrealized_pct は null（0ではない）", () => {
    const state = buildState(
      base({
        positionRows: [
          {
            symbol: "9999.T", bucket: "じっくり", quantity: 100, avg_price: "900",
            take_profit: "1096.78", stop_loss: "827.08", opened_at: "2026-09-01T00:00:00Z",
            last_price: null, last_price_at: null,
          },
        ],
      })
    );
    expect(state.positions[0].unrealized_pnl).toBe(null);
    expect(state.positions[0].unrealized_pct).toBe(null);
  });

  it("1件のlast_price欠けが、他の保有の含み損益を巻き添えにしない", () => {
    const state = buildState(
      base({
        positionRows: [
          {
            symbol: "9999.T", bucket: "じっくり", quantity: 100, avg_price: "900",
            take_profit: "1096.78", stop_loss: "827.08", opened_at: "2026-09-01T00:00:00Z",
            last_price: null, last_price_at: null,
          },
          {
            symbol: "156A.T", bucket: "じっくり", quantity: 100, avg_price: "899",
            take_profit: "1096.78", stop_loss: "827.08", opened_at: "2026-09-01T00:00:00Z",
            last_price: "910", last_price_at: "2026-09-08T00:00:00Z",
          },
        ],
      })
    );
    expect(state.positions[0].unrealized_pnl).toBe(null);
    expect(state.positions[1].unrealized_pnl).toBe(1100);
    expect(state.positions[1].unrealized_pct).toBeCloseTo(11 / 899, 10);
  });
});

describe("枠ごとの使用状況（used / free）", () => {
  it("保有が無ければ全枠が空き", () => {
    const buckets = buildState(base()).buckets;
    expect(buckets).toEqual([
      { name: "じっくり", take_profit_pct: 0.22, stop_loss_pct: 0.08, max_holding_days: null, slots: 2, used: 0, free: 2 },
      { name: "回転", take_profit_pct: 0.1, stop_loss_pct: 0.05, max_holding_days: 10, slots: 2, used: 0, free: 2 },
    ]);
  });

  it("じっくり枠を1つ使っていれば、じっくりの空きが1減る", () => {
    const buckets = buildState(
      base({
        positionRows: [
          {
            symbol: "156A.T", bucket: "じっくり", quantity: 100, avg_price: "899",
            take_profit: "1096.78", stop_loss: "827.08", opened_at: "2026-09-01T00:00:00Z",
            last_price: null, last_price_at: null,
          },
        ],
      })
    ).buckets;
    expect(buckets.find((b) => b.name === "じっくり").used).toBe(1);
    expect(buckets.find((b) => b.name === "じっくり").free).toBe(1);
    expect(buckets.find((b) => b.name === "回転").free).toBe(2);
  });

  it("全体の残り枠で頭打ちになる（じっくりが3件で埋まっていれば、回転の空きは自前の2ではなく全体の残り1に抑えられる）", () => {
    // MAX_POSITIONS は4（じっくり2 + 回転2）。じっくりに3件を積むと
    // （本来の枠数2を超えるが、データ上は起こりうる）全体の残りは
    // 4 - 3 = 1 になる。回転は自前では2件空いているはずだが、
    // 全体の残りを超えて「空いている」と出してはいけない。
    const position = {
      bucket: "じっくり", quantity: 100, avg_price: "899",
      take_profit: "1096.78", stop_loss: "827.08", opened_at: "2026-09-01T00:00:00Z",
      last_price: null, last_price_at: null,
    };
    const buckets = buildState(
      base({
        positionRows: [
          { ...position, symbol: "1111.T" },
          { ...position, symbol: "2222.T" },
          { ...position, symbol: "3333.T" },
        ],
      })
    ).buckets;
    expect(buckets.find((b) => b.name === "回転").free).toBe(1);
  });
});

describe("枠ごとの成績（SQLの行をそのまま使い、勝率だけPythonと同じ形で導く）", () => {
  it("まだ1件も決着していない枠も0件として返す", () => {
    const perf = buildState(base()).performance;
    expect(perf.map((p) => ({ ...p, breakeven_win_rate: undefined }))).toEqual([
      { bucket: "じっくり", closed: 0, wins: 0, win_rate: null, total_pnl: 0, avg_holding_days: null, breakeven_win_rate: undefined },
      { bucket: "回転", closed: 0, wins: 0, win_rate: null, total_pnl: 0, avg_holding_days: null, breakeven_win_rate: undefined },
    ]);
    expect(perf[0].breakeven_win_rate).toBeCloseTo(0.08 / 0.3, 10);
    expect(perf[1].breakeven_win_rate).toBeCloseTo(0.05 / 0.15, 10);
  });

  it("SQLの集計行があれば、その数をそのまま使う（勝率だけ割り算する）", () => {
    const perf = buildState(
      base({
        performanceRows: [
          { bucket: "じっくり", closed: "4", wins: "3", total_pnl: "12000", avg_holding_days: "20.5" },
        ],
      })
    ).performance;
    const jikkuri = perf.find((p) => p.bucket === "じっくり");
    expect(jikkuri.closed).toBe(4);
    expect(jikkuri.wins).toBe(3);
    expect(jikkuri.win_rate).toBe(0.75);
    expect(jikkuri.total_pnl).toBe(12000);
    expect(jikkuri.avg_holding_days).toBe(20.5);
  });
});

describe("提案（pending の一覧）", () => {
  it("買いの提案は action: 'buy' で、cost は quantity × entry_price", () => {
    const proposals = buildState(
      base({
        proposalRows: [
          {
            id: "3", symbol: "156A.T", action: "buy", bucket: "じっくり", quantity: 100, entry_price: "899",
            take_profit: "1096.78", stop_loss: "827.08", rationale: "…", scenario: "…",
            confidence: "mid", created_at: "2026-09-07T01:30:00.000Z",
          },
        ],
      })
    ).proposals;
    expect(proposals[0].id).toBe(3);
    expect(proposals[0].action).toBe("buy");
    expect(proposals[0].cost).toBe(89900);
  });

  it("売りの提案は action: 'sell' で、cost は null（entry_price は保有時点の買値の転記であって売値ではないため、株数と掛けても投資額にならない）", () => {
    const proposals = buildState(
      base({
        proposalRows: [
          {
            id: "4", symbol: "156A.T", action: "sell", bucket: "じっくり", quantity: 100, entry_price: "899",
            take_profit: "1096.78", stop_loss: "827.08", rationale: "…", scenario: "…",
            confidence: "mid", created_at: "2026-09-07T01:30:00.000Z",
          },
        ],
      })
    ).proposals;
    expect(proposals[0].action).toBe("sell");
    expect(proposals[0].cost).toBe(null);
  });

  it("days_old は日本時間の日付の差。朝10:30に出た提案を翌朝9:30に見ると、まだ24時間経っていなくても1になる", () => {
    const proposals = buildState(
      base({
        now: new Date("2026-09-09T00:30:00.000Z"), // JST 2026-09-09 09:30
        proposalRows: [
          {
            id: "5", symbol: "156A.T", action: "buy", bucket: "じっくり", quantity: 100, entry_price: "899",
            take_profit: "1096.78", stop_loss: "827.08", rationale: "…", scenario: "…",
            confidence: "mid", created_at: "2026-09-08T01:30:00.000Z", // JST 2026-09-08 10:30
          },
        ],
      })
    ).proposals;
    expect(proposals[0].days_old).toBe(1);
  });
});

describe("未反映の記録（fills）", () => {
  it("行をそのまま返す。IDはJSONの数値にする", () => {
    const fills = buildState(
      base({
        fillRows: [
          { id: "5", symbol: "1111.T", side: "buy", quantity: 100, price: "900", recorded_at: "…", apply_error: "…" },
        ],
      })
    ).unapplied_fills;
    expect(fills[0].id).toBe(5);
    expect(fills[0].price).toBe(900);
  });
});
