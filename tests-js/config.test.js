import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { BUCKETS, MAX_POSITIONS, SETTINGS, breakevenWinRate } from "../functions/_shared/config.js";

// functions/_shared/config.js は src/investment/config.py の「写し」である。
// Python と JavaScript の間で設定を共有する仕組みが無いため、同じ数字が
// 2箇所にある。片方だけ直しても、どちらのプログラムもエラーにならない。
//
// **だからこのファイルは、config.py を実際に読んで突き合わせる。**
// 以前はここに `expect(SETTINGS.max_position_pct).toBe(0.25)` と書いていたが、
// それは「JavaScript の写しを、JavaScript にもう一度書いた数字」と比べて
// いるだけで、写しの検査になっていなかった。config.py を 0.25 → 0.20 に
// 直して config.js を忘れても、pytest も vitest も全部緑のまま通った。

const CONFIG_PY = readFileSync(new URL("../src/investment/config.py", import.meta.url), "utf8");

// config.py から数字を1つ取り出す。
// **取り出せなければテストを落とす。** config.py の書き方が変わったときに
// 「値が取れなかったので比べなかった」と静かに素通りするのが一番危ない
// （写しがずれていても緑になる）。
function pyNumber(field) {
  const match = CONFIG_PY.match(new RegExp(`${field}\\s*=\\s*([0-9_]+(?:\\.[0-9]+)?)`));
  if (!match) {
    throw new Error(
      `src/investment/config.py から ${field} の値を取り出せなかった。` +
        `config.py の書き方が変わったなら、この取り出し方も直すこと` +
        `（取り出せないまま素通りさせると、写しのずれに気づけなくなる）。`
    );
  }
  // Python は 550_000 のようにアンダースコアで桁を区切れる。
  return Number(match[1].replace(/_/g, ""));
}

// config.py の BUCKETS = ( Bucket(...), Bucket(...) ) を読み取る。
function pyBuckets() {
  const pattern =
    /Bucket\(\s*"([^"]+)",\s*slots=(\d+),\s*take_profit_pct=([0-9.]+),\s*stop_loss_pct=([0-9.]+),\s*max_holding_days=(None|\d+)\s*\)/g;
  const buckets = [...CONFIG_PY.matchAll(pattern)].map((m) => ({
    name: m[1],
    slots: Number(m[2]),
    take_profit_pct: Number(m[3]),
    stop_loss_pct: Number(m[4]),
    max_holding_days: m[5] === "None" ? null : Number(m[5]),
  }));
  if (buckets.length === 0) {
    throw new Error(
      "src/investment/config.py から Bucket(...) を1つも取り出せなかった。" +
        "config.py の書き方が変わったなら、この取り出し方も直すこと。"
    );
  }
  return buckets;
}

describe("config.js の値が src/investment/config.py と同じであること", () => {
  it("枠（じっくり・回転）の名前・枠数・利確幅・損切り幅・期限が config.py と一致する", () => {
    const fromPython = pyBuckets();
    // 枠の数そのものが違えば、どちらかにしか無い枠がある。
    expect(BUCKETS).toHaveLength(fromPython.length);
    expect(BUCKETS).toEqual(fromPython);
  });

  it("1銘柄への投入上限（max_position_pct）が config.py と一致する", () => {
    expect(SETTINGS.max_position_pct).toBe(pyNumber("max_position_pct"));
  });

  it("1取引で許容する損失（risk_per_trade_pct）が config.py と一致する", () => {
    expect(SETTINGS.risk_per_trade_pct).toBe(pyNumber("risk_per_trade_pct"));
  });

  it("最初に入金した額（initial_capital）が config.py と一致する", () => {
    // 画面の「初期資金からいくら増えたか」がこの数字を基準にしている。
    // ずれると、増減の表示がそのまま嘘になる。
    expect(SETTINGS.initial_capital).toBe(pyNumber("initial_capital"));
  });

  it("取り出しそのものが働いていること（config.py の書き方が変わったら気づけること）", () => {
    // 上の3本は「取り出せなければ例外」で守っているが、その仕掛け自体が
    // 効いているかをここで確かめる。存在しない項目を読もうとしたら
    // 落ちること。ここが落ちないなら、値が取れないまま素通りしている。
    expect(() => pyNumber("this_field_does_not_exist")).toThrow(/取り出せなかった/);
  });
});

describe("枠の定義から導かれる値", () => {
  it("同時に持てる銘柄数は枠数の合計（config.py の MAX_POSITIONS と同じ導き方）", () => {
    expect(MAX_POSITIONS).toBe(pyBuckets().reduce((sum, b) => sum + b.slots, 0));
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
