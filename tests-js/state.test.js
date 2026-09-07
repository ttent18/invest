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
  it("is_virtual を渡さなければ true（詳しくは後半の is_virtual のテスト）", () => {
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
      { bucket: "じっくり", closed: 0, wins: 0, win_rate: null, total_pnl: 0, avg_holding_days: null, expired_count: null, breakeven_win_rate: undefined },
      { bucket: "回転", closed: 0, wins: 0, win_rate: null, total_pnl: 0, avg_holding_days: null, expired_count: 0, breakeven_win_rate: undefined },
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

  it("期限で降りた件数は、期限のある回転枠だけ数える。じっくり枠は 0 ではなく null（そもそも期限が無いため）", () => {
    // rules/v3.md は「各枠15件たまったら、勝率・平均保有日数・期限切れの
    // 件数で比べる」と決めている。その3つ目がこれ。
    const perf = buildState(
      base({
        performanceRows: [
          { bucket: "回転", closed: "6", wins: "2", total_pnl: "-3000", avg_holding_days: "7.5", expired_count: "2" },
          { bucket: "じっくり", closed: "3", wins: "2", total_pnl: "9000", avg_holding_days: "31", expired_count: "1" },
        ],
      })
    ).performance;
    expect(perf.find((p) => p.bucket === "回転").expired_count).toBe(2);
    // じっくり枠には期限が無い。SQL が何を返していても null にする
    // （0 と書くと「期限切れが一度も無かった」という別の意味になる）。
    expect(perf.find((p) => p.bucket === "じっくり").expired_count).toBe(null);
  });

  it("回転枠にまだ1件も決着が無ければ、期限で降りた件数は 0（null ではない。0件だったという事実）", () => {
    const perf = buildState(base()).performance;
    expect(perf.find((p) => p.bucket === "回転").expired_count).toBe(0);
  });
});

describe("会社名（name）", () => {
  // 利用者は 137,500 円の注文を、銘柄コードだけを頼りに SBI の画面へ
  // 手で打ち込む。会社名が並んでいないと、打ち間違えても気づけない。
  function position(overrides = {}) {
    return {
      symbol: "7203.T", bucket: "じっくり", quantity: 100, avg_price: "899",
      take_profit: "1096.78", stop_loss: "827.08", opened_at: "2026-09-01T00:00:00Z",
      last_price: null, last_price_at: null,
      ...overrides,
    };
  }
  function proposal(overrides = {}) {
    return {
      id: "3", symbol: "6758.T", action: "buy", bucket: "じっくり", quantity: 100, entry_price: "899",
      take_profit: "1096.78", stop_loss: "827.08", rationale: "…", scenario: "…",
      confidence: "mid", created_at: "2026-09-07T01:30:00.000Z",
      ...overrides,
    };
  }

  it("保有と提案の両方に会社名が入る", () => {
    const state = buildState(
      base({
        namesBySymbol: new Map([["7203.T", "トヨタ自動車"], ["6758.T", "ソニーグループ"]]),
        positionRows: [position()],
        proposalRows: [proposal()],
      })
    );
    expect(state.positions[0].name).toBe("トヨタ自動車");
    expect(state.proposals[0].name).toBe("ソニーグループ");
  });

  it("会社名が引けなかった銘柄は null。他の銘柄は巻き添えにならない", () => {
    // 会社名は補助の情報。1件引けなかっただけで画面が落ちたり、
    // 他の銘柄の名前まで消えたりしてはいけない。
    const state = buildState(
      base({
        namesBySymbol: new Map([["7203.T", "トヨタ自動車"]]),
        positionRows: [position(), position({ symbol: "9999.T" })],
        proposalRows: [proposal()],
      })
    );
    expect(state.positions[0].name).toBe("トヨタ自動車");
    expect(state.positions[1].name).toBe(null);
    expect(state.positions[1].symbol).toBe("9999.T");
    // 名前が引けなくても、その保有の中身は今までどおり出る。
    expect(state.positions[1].quantity).toBe(100);
    expect(state.proposals[0].name).toBe(null);
  });

  it("会社名の取得そのものが失敗して名前が1件も渡ってこなくても、画面は組み立てられる", () => {
    const state = buildState(
      base({ namesBySymbol: undefined, positionRows: [position()], proposalRows: [proposal()] })
    );
    expect(state.positions[0].name).toBe(null);
    expect(state.proposals[0].name).toBe(null);
    expect(state.positions[0].symbol).toBe("7203.T");
    expect(state.capital).toBe(550000);
  });
});

describe("提案に「もう持っている銘柄です」の印を付ける（already_held）", () => {
  // 提案の候補から外しているのは保有中の銘柄だけで、「返事をしていない
  // 提案がある銘柄」は外していない。返事の無い提案も自動では消えない。
  // そのため「月曜の提案を放置 → 火曜に同じ銘柄がまた提案される」が起きる。
  // 片方を記録して翌朝それが保有になったあと、残ったもう1件を押すと
  // 買い増しになる。画面がその危険を出せるように印を付ける。
  const proposal = {
    id: "3", symbol: "7203.T", action: "buy", bucket: "じっくり", quantity: 100, entry_price: "899",
    take_profit: "1096.78", stop_loss: "827.08", rationale: "…", scenario: "…",
    confidence: "mid", created_at: "2026-09-07T01:30:00.000Z",
  };
  const position = {
    symbol: "7203.T", bucket: "じっくり", quantity: 100, avg_price: "899",
    take_profit: "1096.78", stop_loss: "827.08", opened_at: "2026-09-01T00:00:00Z",
    last_price: null, last_price_at: null,
  };

  it("提案の銘柄をいま持っていれば already_held は true", () => {
    const state = buildState(base({ proposalRows: [proposal], positionRows: [position] }));
    expect(state.proposals[0].already_held).toBe(true);
  });

  it("持っていなければ already_held は false（null や undefined にしない）", () => {
    const state = buildState(base({ proposalRows: [proposal], positionRows: [] }));
    expect(state.proposals[0].already_held).toBe(false);
  });

  it("持っている銘柄と持っていない銘柄が混ざっていても、それぞれ正しく付く", () => {
    const state = buildState(
      base({
        proposalRows: [proposal, { ...proposal, id: "4", symbol: "6758.T" }],
        positionRows: [position],
      })
    );
    expect(state.proposals.map((p) => [p.symbol, p.already_held])).toEqual([
      ["7203.T", true],
      ["6758.T", false],
    ]);
  });
});

describe("画面が実弾かどうかを言う（is_virtual）", () => {
  it("渡された値をそのまま返す（判断は functions/api/state.js が環境変数から行う）", () => {
    expect(buildState(base({ isVirtual: false })).is_virtual).toBe(false);
    expect(buildState(base({ isVirtual: true })).is_virtual).toBe(true);
  });

  it("渡されなければ true（安全側。設定がまだ無い＝まだ切り替えていない）", () => {
    expect(buildState(base({ isVirtual: undefined })).is_virtual).toBe(true);
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

describe("初期資金からの増減（profit_since_start）", () => {
  it("initial_capital を返す（config.js の SETTINGS.initial_capital）", () => {
    expect(buildState(base()).initial_capital).toBe(550000);
  });

  it("capital が 570,000 のとき profit_since_start は 20,000（capital - initial_capital の引き算だけ）", () => {
    const state = buildState(base({ capital: 570000 }));
    expect(state.profit_since_start).toBe(20000);
  });

  it("capital が分からなければ profit_since_start も null（0ではない）", () => {
    const state = buildState(base({ capital: null }));
    expect(state.profit_since_start).toBe(null);
  });
});

describe("営業日の数え方（business_days_held）", () => {
  // ここが1日ずれると、画面が「期限切れ。降りて枠を空けてください」と
  // 出す日が、ルール（Python 側）が期限切れと判断する日とずれる。
  // 利用者は画面を見て実際に売るので、ずれはそのまま「1日早い売却」になる。
  //
  // **正解の値は src/investment/jobs/morning_check.py の
  // _business_days_between に聞いて決めた。** あの関数は
  //     d = start; while d < end: d += 1日; d が月〜金なら +1
  // と書かれている。つまり「買った日の翌日から、見た日までの間にある
  // 月〜金の日数」を数える（買った日は数えず、見た日は数える）。
  // 下の表の値は、その規則で1日ずつ数えて求め、実際に Python の
  // _business_days_between を呼んで18組すべて一致することを確かめた。
  //
  // **土曜・日曜・月曜をまたぐ組を必ず入れてある。** 以前ここには
  // 「金曜→月曜」のような、曜日が1日ずれていても答えが同じになる組しか
  // 無かったため、曜日が1日ずれる不具合があってもテストは緑のままだった。
  const CASES = [
    // [買った日, 見た日, 正解の営業日数, 説明]
    ["2026-09-04", "2026-09-05", 0, "金→土: 土は営業日ではない"],
    ["2026-09-04", "2026-09-06", 0, "金→日: 土日とも営業日ではない"],
    ["2026-09-04", "2026-09-07", 1, "金→月: 土日を飛ばして月の1日"],
    ["2026-09-05", "2026-09-05", 0, "土→土: 同じ日なので0"],
    ["2026-09-05", "2026-09-06", 0, "土→日"],
    ["2026-09-05", "2026-09-07", 1, "土→月: 月の1日だけ"],
    ["2026-09-06", "2026-09-07", 1, "日→月: 月の1日だけ"],
    ["2026-09-07", "2026-09-08", 1, "月→火"],
    ["2026-09-07", "2026-09-11", 4, "月→金: 火水木金の4日"],
    ["2026-09-07", "2026-09-12", 4, "月→土: 土は増えない"],
    ["2026-09-07", "2026-09-13", 4, "月→日: 日も増えない"],
    ["2026-09-07", "2026-09-14", 5, "月→翌月: 土日を挟んで5日"],
    ["2026-09-07", "2026-09-21", 10, "月→2週間後の月: ちょうど10営業日"],
    ["2026-09-11", "2026-09-14", 1, "金→月"],
    ["2026-09-12", "2026-09-14", 1, "土→月"],
    ["2026-09-13", "2026-09-18", 5, "日→金: 月火水木金の5日"],
    ["2026-06-01", "2026-06-13", 9, "月→12日後の土: 9日（10ではない）"],
    ["2026-06-06", "2026-06-19", 10, "土→13日後の金: ちょうど10営業日"],
  ];

  // 日付（YYYY-MM-DD）を「その日の日本時間 09:00」にする。
  // 日本時間 09:00 は UTC で同じ日付の 00:00。
  const jstMorning = (day) => `${day}T00:00:00.000Z`;
  // 日付（YYYY-MM-DD）を「その日の日本時間 10:30」にする（画面を見た時刻）。
  const jstLater = (day) => new Date(`${day}T01:30:00.000Z`);

  function position(overrides = {}) {
    return {
      symbol: "156A.T", bucket: "回転", quantity: 100, avg_price: "899",
      take_profit: "988.9", stop_loss: "854.05", opened_at: "2026-09-07T00:00:00Z",
      last_price: null, last_price_at: null,
      ...overrides,
    };
  }

  for (const [opened, viewed, expected, why] of CASES) {
    it(`${opened} に買って ${viewed} に見たら ${expected} 営業日（${why}）`, () => {
      const state = buildState(
        base({
          now: jstLater(viewed),
          positionRows: [position({ opened_at: jstMorning(opened) })],
        })
      );
      expect(state.positions[0].business_days_held).toBe(expected);
    });
  }

  it("時刻が違っても、日本時間の日付が同じなら答えは変わらない（夜に買っても翌朝に買っても同じ日）", () => {
    // JST 2026-09-07 23:00 = UTC 2026-09-07 14:00。日付は日本時間の 09-07。
    const state = buildState(
      base({
        now: jstLater("2026-09-08"),
        positionRows: [position({ opened_at: "2026-09-07T14:00:00.000Z" })],
      })
    );
    expect(state.positions[0].business_days_held).toBe(1);
  });
});

describe("回転枠の期限（days_left）", () => {
  function position(overrides = {}) {
    return {
      symbol: "156A.T", bucket: "回転", quantity: 100, avg_price: "899",
      take_profit: "988.9", stop_loss: "854.05", opened_at: "2026-09-07T00:00:00Z", // JST 2026-09-07 09:00（月曜）
      last_price: null, last_price_at: null,
      ...overrides,
    };
  }

  it("回転枠でちょうど10営業日たったら days_left が 0（1 ではない）", () => {
    // 2026-09-07（月）から10営業日後 = 2026-09-21（月）。
    const state = buildState(
      base({
        now: new Date("2026-09-21T01:30:00.000Z"), // JST 2026-09-21 10:30（月曜）
        positionRows: [position()],
      })
    );
    expect(state.positions[0].business_days_held).toBe(10);
    expect(state.positions[0].days_left).toBe(0);
  });

  it("2026-06-01（月）に買って 2026-06-13（土）に見たら、まだ期限内（days_left は 1）", () => {
    // Python の _business_days_between は 9 を返す。回転枠の期限は10営業日
    // なので、この日はまだ降りる日ではない。曜日が1日ずれていると 10 と
    // 数えてしまい、画面が1日早く「期限切れ」と言い出す。
    const state = buildState(
      base({
        now: new Date("2026-06-13T01:30:00.000Z"), // JST 2026-06-13 10:30（土曜）
        positionRows: [position({ opened_at: "2026-06-01T00:00:00.000Z" })],
      })
    );
    expect(state.positions[0].business_days_held).toBe(9);
    expect(state.positions[0].days_left).toBe(1);
  });

  it("2026-06-06（土）に買って 2026-06-19（金）に見たら、ちょうど期限（days_left は 0）", () => {
    // Python は 10 を返す。朝の通知が「1件が期限切れ」と鳴らす日なので、
    // 画面も同じ日に期限切れと出さないと、通知と画面が食い違う。
    const state = buildState(
      base({
        now: new Date("2026-06-19T01:30:00.000Z"), // JST 2026-06-19 10:30（金曜）
        positionRows: [position({ opened_at: "2026-06-06T00:00:00.000Z" })],
      })
    );
    expect(state.positions[0].business_days_held).toBe(10);
    expect(state.positions[0].days_left).toBe(0);
  });

  it("じっくり枠は days_left が null（期限が無いため）", () => {
    const state = buildState(
      base({
        positionRows: [position({ bucket: "じっくり" })],
      })
    );
    expect(state.positions[0].days_left).toBe(null);
    expect(state.positions[0].business_days_held).toBe(1);
  });
});

describe("未反映の記録（fills）", () => {
  it("行をそのまま返す。IDはJSONの数値にする", () => {
    const fills = buildState(
      base({
        fillRows: [
          { id: "5", proposal_id: "3", symbol: "1111.T", side: "buy", quantity: 100, price: "900", recorded_at: "…", apply_error: "…" },
        ],
      })
    ).unapplied_fills;
    expect(fills[0].id).toBe(5);
    expect(fills[0].price).toBe(900);
    expect(fills[0].quantity).toBe(100);
  });

  it("どの提案に対する記録かを返す（proposal_id）", () => {
    // これが無いと、画面は「銘柄コードと売買の向き」だけで記録済みかを
    // 判定するしかない。同じ銘柄・同じ向きの提案が2件並んだとき、片方を
    // 記録しただけで両方が記録済みに見え、翌朝の反映後に生き残った側を
    // 押すと保有中の銘柄の買い増しになる。
    const fills = buildState(
      base({
        fillRows: [
          { id: "5", proposal_id: "3", symbol: "1111.T", side: "buy", quantity: 100, price: "900", recorded_at: "…", apply_error: null },
          { id: "6", proposal_id: "4", symbol: "1111.T", side: "buy", quantity: 100, price: "905", recorded_at: "…", apply_error: null },
        ],
      })
    ).unapplied_fills;
    expect(fills.map((f) => f.proposal_id)).toEqual([3, 4]);
  });

  it("提案を経由しない売りの記録（proposal_id が null）でも落ちない", () => {
    const fills = buildState(
      base({
        fillRows: [
          { id: "7", proposal_id: null, symbol: "1111.T", side: "sell", quantity: 100, price: "950", recorded_at: "…", apply_error: null },
        ],
      })
    ).unapplied_fills;
    expect(fills[0].proposal_id).toBe(null);
    expect(fills[0].side).toBe("sell");
  });

  it("proposal_id の列そのものが無い行でも null になる（undefined を返さない）", () => {
    const fills = buildState(
      base({
        fillRows: [
          { id: "8", symbol: "1111.T", side: "sell", quantity: 100, price: "950", recorded_at: "…", apply_error: null },
        ],
      })
    ).unapplied_fills;
    expect(fills[0].proposal_id).toBe(null);
  });
});
