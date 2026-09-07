import json
from pathlib import Path
from unittest.mock import patch

from investment.config import SCREEN, SETTINGS
from investment.jobs.build_context import (
    RULE_VERSION,
    assemble,
    attach_last_price,
    drop_already_held,
    drop_unaffordable,
    main,
    read_inputs,
)
from investment.market import MarketDataError


def test_assemble_includes_candidates_and_constraints():
    candidates = [
        {
            "symbol": "3993.T",
            "name": "PKSHA",
            "market_cap": 9_800_000_000,
            "revenue_growth": 0.81,
            "operating_margin": 0.14,
            "roe": 0.20,
            "equity_ratio": 0.63,
            "last_price": 3120.0,
        }
    ]
    ctx = assemble(candidates, [], {"JPY": 550_000}, SETTINGS, SCREEN, dropped=[], capital=550_000.0)

    assert ctx["is_virtual"] is True
    assert ctx["rule_version"] == "v3"
    assert ctx["constraints"]["max_positions"] == 4
    # 利確・損切りの幅は枠ごとに違うので、constraints ではなく buckets にある
    assert [b["name"] for b in ctx["buckets"]] == ["じっくり", "回転"]
    assert len(ctx["candidates"]) == 1
    assert ctx["candidates"][0]["symbol"] == "3993.T"
    assert ctx["candidates"][0]["last_price"] == 3120.0


def test_assemble_reports_no_room_when_positions_are_full():
    full = [{"symbol": f"{i}.T"} for i in range(4)]  # 合計4枠が境界
    ctx = assemble([], full, {"JPY": 0}, SETTINGS, SCREEN, dropped=[], capital=550_000.0)
    assert ctx["can_open_new"] is False


def test_read_inputs_reads_everything_the_database_holds():
    """株価を取りに行く前に、データベースから読むものを読み切ること。"""
    with (
        patch("investment.jobs.build_context.select_screened", return_value=[{"symbol": "1111.T"}]),
        patch("investment.jobs.build_context.select_positions", return_value=[{"symbol": "9.T"}]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.select_capital", return_value=550_000.0),
    ):
        candidates, positions, cash, capital = read_inputs(conn=None, criteria=SCREEN, limit=50)

    assert candidates == [{"symbol": "1111.T"}]
    assert positions == [{"symbol": "9.T"}]
    assert cash == {"JPY": 550_000}
    assert capital == 550_000.0


def test_attach_last_price_never_touches_the_database():
    """株価の取得中はデータベースに触れないこと。

    データベースの接続は数分放置されると切られる。株価の取得には
    候補の件数だけ時間がかかるため、この間に接続を触る設計にしていると、
    候補を増やしたときに切断されてクラッシュする（run_screen で実際に起きた）。
    """

    def boom(*args, **kwargs):
        raise AssertionError("株価の取得中にデータベースへアクセスしてはいけない")

    def fake_fetch(symbol):
        if symbol == "2222.T":
            raise MarketDataError("2222.T の株価取得に失敗しました")
        return 1500.0

    candidates = [
        {"symbol": "1111.T", "name": "取れる会社"},
        {"symbol": "2222.T", "name": "株価が取れない会社"},
    ]

    with (
        patch("investment.jobs.build_context.fetch_last_price", side_effect=fake_fetch),
        patch("investment.jobs.build_context.connect", side_effect=boom),
        patch("investment.jobs.build_context.record_gaps", side_effect=boom),
    ):
        priced, gaps = attach_last_price(candidates)

    # 株価が取れなかった 2222.T は候補から除外される
    assert [c["symbol"] for c in priced] == ["1111.T"]
    assert priced[0]["last_price"] == 1500.0

    # 除外した事実は呼び出し側に返され、あとでまとめて記録される
    assert len(gaps) == 1
    assert gaps[0][0] == "price:2222.T"
    assert "2222.T" in gaps[0][1]


def test_attach_last_price_prints_exclusion_message_to_stdout(capsys):
    with patch(
        "investment.jobs.build_context.fetch_last_price",
        side_effect=MarketDataError("失敗"),
    ):
        priced, gaps = attach_last_price([{"symbol": "2222.T", "name": "株価が取れない会社"}])

    assert priced == []
    assert len(gaps) == 1
    assert "1 件を候補から除外しました" in capsys.readouterr().out


def test_main_closes_the_connection_before_fetching_prices(tmp_path):
    """接続を開くのは、株価の取得の前後に分かれること。

    「読む → (接続を閉じて)株価を取る → 開き直して書く」の順序そのものを検証する。
    """
    calls: list[str] = []

    class FakeConn:
        def __enter__(self):
            calls.append("connect")
            return self

        def __exit__(self, *exc):
            calls.append("close")
            return False

    def fake_fetch(symbol):
        calls.append(f"fetch:{symbol}")
        raise MarketDataError("株価が取れません")

    with (
        patch("investment.jobs.build_context.connect", side_effect=lambda: FakeConn()),
        patch("investment.jobs.build_context.select_screened", return_value=[{"symbol": "1111.T"}]),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.select_capital", return_value=550_000.0),
        patch("investment.jobs.build_context.fetch_last_price", side_effect=fake_fetch),
        patch("investment.jobs.build_context.record_gaps") as record,
        patch("investment.jobs.build_context.OUTPUT", tmp_path / "context.json"),
    ):
        assert main() == 0

    assert calls == ["connect", "close", "fetch:1111.T", "connect", "close"]
    record.assert_called_once()
    assert record.call_args.args[1][0][0] == "price:1111.T"


def test_main_does_not_reopen_the_connection_when_nothing_failed(tmp_path):
    """株価が全件取れて除外も無い日は、書き込みのために接続を開き直さないこと。"""
    calls: list[str] = []

    class FakeConn:
        def __enter__(self):
            calls.append("connect")
            return self

        def __exit__(self, *exc):
            return False

    with (
        patch("investment.jobs.build_context.connect", side_effect=lambda: FakeConn()),
        patch("investment.jobs.build_context.select_screened", return_value=[{"symbol": "1111.T"}]),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.select_capital", return_value=550_000.0),
        # 100株で120,000円。上限137,500円に収まるので除外されない
        patch("investment.jobs.build_context.fetch_last_price", return_value=1200.0),
        patch("investment.jobs.build_context.record_gaps") as record,
        patch("investment.jobs.build_context.OUTPUT", tmp_path / "context.json"),
    ):
        assert main() == 0

    assert calls == ["connect"]
    record.assert_not_called()


# --- 既に保有している銘柄を候補から外す（指摘1-a） ---------------------------
# 保有中の銘柄がまた候補に出ると、AIはそれを「買い増し」として提案できて
# しまう。買い増すと、SBIに置いてある逆指値（この値段まで下がったら売る、
# という予約注文）が古い値のままになり、実際の損切り価格とずれる。


def test_drop_already_held_removes_candidates_that_are_currently_held():
    candidates = [
        {"symbol": "1111.T", "last_price": 1200.0},
        {"symbol": "2222.T", "last_price": 900.0},
    ]
    positions = [{"symbol": "2222.T", "quantity": 100, "bucket": "じっくり"}]

    kept, dropped = drop_already_held(candidates, positions)

    assert [c["symbol"] for c in kept] == ["1111.T"]
    assert [d["symbol"] for d in dropped] == ["2222.T"]


def test_drop_already_held_keeps_everything_when_nothing_is_held():
    candidates = [{"symbol": "1111.T", "last_price": 1200.0}]
    kept, dropped = drop_already_held(candidates, positions=[])
    assert kept == candidates
    assert dropped == []


def test_assemble_records_the_symbols_excluded_for_being_already_held():
    """外した事実を黙って消さず、件数と銘柄をコンテキストに残すこと。"""
    held_dropped = [{"symbol": "2222.T", "last_price": 900.0}]
    ctx = assemble(
        [], [], {"JPY": 550_000}, SETTINGS, SCREEN, dropped=[], capital=550_000.0,
        held_dropped=held_dropped,
    )

    assert ctx["excluded_held_candidates"]["count"] == 1
    assert "2222.T" in ctx["excluded_held_candidates"]["symbols"]
    assert "保有" in ctx["excluded_held_candidates"]["reason"]


def test_assemble_reports_no_held_exclusions_when_not_given():
    ctx = assemble([], [], {"JPY": 550_000}, SETTINGS, SCREEN, dropped=[], capital=550_000.0)
    assert ctx["excluded_held_candidates"]["count"] == 0
    assert ctx["excluded_held_candidates"]["symbols"] == []


def test_main_excludes_symbols_already_held_from_the_candidates(tmp_path):
    """本番の経路（main）で、保有中の銘柄が候補から消えること。"""
    with (
        patch("investment.jobs.build_context.connect"),
        patch(
            "investment.jobs.build_context.select_screened",
            return_value=[{"symbol": "1111.T"}, {"symbol": "2222.T"}],
        ),
        patch(
            "investment.jobs.build_context.select_positions",
            return_value=[{"symbol": "2222.T", "bucket": "じっくり"}],
        ),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.select_capital", return_value=550_000.0),
        patch("investment.jobs.build_context.fetch_last_price", return_value=1200.0),
        patch("investment.jobs.build_context.record_gaps") as record,
        patch("investment.jobs.build_context.OUTPUT", tmp_path / "context.json"),
    ):
        assert main() == 0

    ctx = json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))
    assert [c["symbol"] for c in ctx["candidates"]] == ["1111.T"]
    assert ctx["excluded_held_candidates"]["symbols"] == ["2222.T"]

    recorded = record.call_args.args[1]
    assert any(scope == "candidate_already_held:2222.T" for scope, _detail in recorded)


# --- 総資金が0円のとき、静かに全候補が除外されて終わらないこと（指摘2） -------
# cash に JPY の行が無い・消えた場合、select_capital は黙って0.0を返す。
# 気づかずに進むと、1銘柄の上限が0円になって全候補が除外されるだけで
# 正常終了してしまい、「今日は買える候補が無かった」と区別が付かなくなる。


def test_main_stops_with_error_code_when_total_capital_is_zero(tmp_path):
    with (
        patch("investment.jobs.build_context.connect"),
        patch("investment.jobs.build_context.select_screened", return_value=[]),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 0}),
        patch("investment.jobs.build_context.select_capital", return_value=0.0),
        patch("investment.jobs.build_context.record_gap") as gap,
        patch("investment.jobs.build_context.OUTPUT", tmp_path / "context.json"),
    ):
        code = main()

    assert code == 1
    gap.assert_called_once()
    assert gap.call_args.kwargs["scope"] == "capital:zero"
    assert "総資金" in gap.call_args.kwargs["detail"]
    assert not (tmp_path / "context.json").exists()


# --- 100株単位で買えない銘柄を候補から外す ----------------------------------


def test_drop_unaffordable_removes_stocks_whose_one_lot_exceeds_the_limit():
    """1単元（日本株なら100株）で1銘柄の上限金額を超える銘柄は候補から外す。

    株価2,704円だと100株で270,400円になり、上限137,500円を超えるので買えない。
    AIに渡しても発注できない提案しか作れず、調べる手間が無駄になる。
    2026-09-07 の初回運用で実際に起きた（AIが41件中5件しか調べられなかった）。
    """
    candidates = [
        {"symbol": "1111.T", "last_price": 1200.0},   # 100株 = 120,000円 → 買える
        {"symbol": "2222.T", "last_price": 2704.0},   # 100株 = 270,400円 → 買えない
        {"symbol": "3333.T", "last_price": 1375.0},   # 100株 = 137,500円 ちょうど → 買える
    ]
    kept, dropped = drop_unaffordable(candidates, SETTINGS, capital=550_000.0)

    assert [c["symbol"] for c in kept] == ["1111.T", "3333.T"]
    assert [d["symbol"] for d in dropped] == ["2222.T"]


def test_drop_unaffordable_keeps_us_stocks_that_fit_in_one_share():
    """米国株は1株から買えるので、株価が上限内なら残る。"""
    kept, dropped = drop_unaffordable([{"symbol": "AAPL", "last_price": 200.0}], SETTINGS, capital=550_000.0)
    assert [c["symbol"] for c in kept] == ["AAPL"]
    assert dropped == []


def test_assemble_records_what_it_dropped_and_why():
    """外した銘柄を黙って消さず、件数と理由をコンテキストに残すこと。

    AIが「なぜこの銘柄が候補にないのか」を追えるようにするため。
    """
    dropped = [
        {"symbol": "2222.T", "last_price": 2704.0, "lot_size": 100, "lot_cost": 270400.0}
    ]
    ctx = assemble([], [], {"JPY": 550_000}, SETTINGS, SCREEN, dropped=dropped, capital=550_000.0)

    assert ctx["excluded_candidates"]["count"] == 1
    assert "2222.T" in str(ctx["excluded_candidates"]["symbols"])
    assert "単元" in ctx["excluded_candidates"]["reason"]


def test_kept_candidates_carry_the_lot_size_and_the_buyable_quantity():
    """各候補が何株単位で、最大何株まで買えるかをAIに渡すこと。

    AIが「上限金額 ÷ 株価」を自分で計算すると単元未満の株数になり、発注できない。
    正しい答えをこちらで計算して渡す。
    株価1,200円・上限137,500円なら114株買えるが、100株単位なので100株。
    """
    kept, _ = drop_unaffordable([{"symbol": "1111.T", "last_price": 1200.0}], SETTINGS, capital=550_000.0)
    assert kept[0]["lot_size"] == 100
    assert kept[0]["max_quantity"] == 100


def test_read_inputs_returns_the_capital_calculated_from_the_database():
    """総資金をコードの直書きではなくデータベースから取ること。"""
    with (
        patch("investment.jobs.build_context.select_screened", return_value=[]),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 620_000}),
        patch("investment.jobs.build_context.select_capital", return_value=620_000.0),
    ):
        _candidates, _positions, _cash, capital = read_inputs(
            conn=None, criteria=SCREEN, limit=50
        )

    assert capital == 620_000.0


def test_assemble_reports_the_capital_it_was_given_not_the_initial_deposit():
    """コンテキストに入る総資金は、いまの額であること。"""
    ctx = assemble([], [], {"JPY": 620_000}, SETTINGS, SCREEN, dropped=[], capital=620_000.0)
    assert ctx["constraints"]["total_capital"] == 620_000.0


def test_the_buying_limit_grows_with_the_capital():
    """資金が増えたら、1銘柄に投じられる額も増えること。

    これが「利確して資金を増やし、さらに投資する」の中身である。
    550,000円のときは137,500円まで、620,000円なら155,000円まで買える。
    """
    kept, _ = drop_unaffordable(
        [{"symbol": "1111.T", "last_price": 1500.0}], SETTINGS, capital=620_000.0
    )
    assert len(kept) == 1                       # 100株で150,000円。155,000円以内
    assert kept[0]["max_quantity"] == 100

    kept, dropped = drop_unaffordable(
        [{"symbol": "1111.T", "last_price": 1500.0}], SETTINGS, capital=550_000.0
    )
    assert kept == []                           # 137,500円では買えない
    assert dropped[0]["symbol"] == "1111.T"


def test_the_rule_version_has_a_matching_rules_document():
    """RULE_VERSION を上げたら rules/<版>.md が存在すること。

    版を上げただけで理由を書き忘れると、あとから「なぜ変えたか」が
    たどれなくなる（設計書16章が要求している記録そのものが消える）。
    """
    doc = Path(__file__).resolve().parents[1] / "rules" / f"{RULE_VERSION}.md"
    assert doc.exists(), f"{doc} がありません。ルールを変えたら理由を書くこと"


def test_kept_candidates_carry_the_highest_usable_entry_price():
    """買値をいくらまで上げられるかをAIに渡すこと。

    AIは last_price の ±10% の範囲で買値を決めてよいことになっている。
    しかし max_quantity は last_price を基準に計算した値なので、
    買値を上げると同じ株数では金額の上限を超えてしまう。
    日本株は100株単位なので「1株減らす」ができず、指示どおりに答えた提案が
    まるごと却下される。株価1,250円超の候補すべてで起きる（実測の中央値は1,318円）。

    そこで「この株数を保ったまま出せる買値の上限」を計算して渡す。
    """
    kept, _ = drop_unaffordable([{"symbol": "1111.T", "last_price": 1300.0}], SETTINGS, capital=550_000.0)

    assert kept[0]["max_quantity"] == 100
    # 137,500円 ÷ 100株 = 1,375円 まで
    assert kept[0]["max_entry_price"] == 1375.0
    # この買値・この株数なら上限ちょうどに収まる
    assert kept[0]["max_entry_price"] * kept[0]["max_quantity"] <= 550_000 * 0.25


def test_max_entry_price_is_rounded_down_to_stay_within_the_limit():
    """割り切れない場合は切り捨てる。切り上げると上限を超えてしまう。"""
    # 306円 → max_quantity=400 → 137,500 ÷ 400 = 343.75 → 343円
    kept, _ = drop_unaffordable([{"symbol": "5137.T", "last_price": 306.0}], SETTINGS, capital=550_000.0)
    assert kept[0]["max_quantity"] == 400
    assert kept[0]["max_entry_price"] == 343.0
    assert kept[0]["max_entry_price"] * kept[0]["max_quantity"] <= 550_000 * 0.25


def test_main_records_the_excluded_candidates_in_the_database(tmp_path):
    """買えないので外した銘柄を、あとから数えられる形で残すこと。

    context.json は git 管理外で毎回上書きされ、実行ログも Actions の保持期間で
    消える。「あの日は何件が買えなくて外れたのか」を後から測れるよう、
    株価が取れなかった件と同じく data_gaps に残す。
    """
    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with (
        patch("investment.jobs.build_context.connect", side_effect=lambda: FakeConn()),
        patch(
            "investment.jobs.build_context.select_screened",
            return_value=[{"symbol": "1111.T"}, {"symbol": "2222.T"}],
        ),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 550_000}),
        patch("investment.jobs.build_context.select_capital", return_value=550_000.0),
        patch(
            "investment.jobs.build_context.fetch_last_price",
            side_effect=lambda s: 1200.0 if s == "1111.T" else 2704.0,
        ),
        patch("investment.jobs.build_context.record_gaps") as record,
        patch("investment.jobs.build_context.OUTPUT", tmp_path / "context.json"),
    ):
        assert main() == 0

    recorded = record.call_args.args[1]
    assert len(recorded) == 1
    scope, detail = recorded[0]
    assert scope == "candidate_unaffordable:2222.T"
    assert "270,400" in detail  # 100株の金額


# --- 枠（じっくり / 回転）をAIに伝える --------------------------------------


def test_assemble_tells_the_ai_each_bucket_and_how_many_slots_are_free():
    """枠ごとの利確・損切り・期限・空き枠数をAIに渡すこと。

    AIはどちらの枠で買うかを選ぶ必要があり、選んだ枠によって
    利確幅・損切り幅が変わる。空き枠が無い枠には提案できない。
    """
    ctx = assemble([], [], {"JPY": 550_000}, SETTINGS, SCREEN, dropped=[], capital=550_000.0)
    by_name = {b["name"]: b for b in ctx["buckets"]}

    assert by_name["じっくり"] == {
        "name": "じっくり", "take_profit_pct": 0.22, "stop_loss_pct": 0.08,
        "max_holding_days": None, "slots": 2, "used": 0, "free": 2,
    }
    assert by_name["回転"] == {
        "name": "回転", "take_profit_pct": 0.10, "stop_loss_pct": 0.05,
        "max_holding_days": 10, "slots": 2, "used": 0, "free": 2,
    }


def test_assemble_counts_used_slots_per_bucket():
    """保有している銘柄を、その銘柄の枠に数えること。"""
    positions = [
        {"symbol": "1111.T", "bucket": "回転"},
        {"symbol": "2222.T", "bucket": "回転"},
        {"symbol": "3333.T", "bucket": "じっくり"},
    ]
    ctx = assemble([], positions, {}, SETTINGS, SCREEN, dropped=[], capital=550_000.0)
    by_name = {b["name"]: b for b in ctx["buckets"]}

    assert (by_name["回転"]["used"], by_name["回転"]["free"]) == (2, 0)
    assert (by_name["じっくり"]["used"], by_name["じっくり"]["free"]) == (1, 1)


def test_positions_without_a_bucket_are_counted_as_patient_not_ignored():
    """枠の記録が無い保有を、黙って0扱いにしないこと。

    数え落とすと「枠が空いている」と誤って伝えることになり、
    上限を超えて買う提案が出てしまう。
    """
    ctx = assemble([], [{"symbol": "1111.T"}], {}, SETTINGS, SCREEN, dropped=[], capital=550_000.0)
    by_name = {b["name"]: b for b in ctx["buckets"]}
    assert by_name["じっくり"]["used"] == 1


def test_free_slots_never_exceed_the_overall_remaining_capacity():
    """枠ごとの空きの合計が、全体の残り枠を超えないこと。

    3銘柄すべて回転枠なら、回転0・じっくり2で合計2だが、全体の残りは1。
    そのままAIに見せると、矛盾した数を根拠に判断させることになる。
    （実際に上限を守らせているのは apply_decision.process のほう。
    ここは表示を正すだけ。）
    """
    positions = [{"symbol": f"{i}.T", "bucket": "回転"} for i in range(3)]
    ctx = assemble([], positions, {}, SETTINGS, SCREEN, dropped=[], capital=550_000.0)
    by_name = {b["name"]: b for b in ctx["buckets"]}

    assert by_name["回転"]["free"] == 0
    assert by_name["じっくり"]["free"] == 1   # 頭打ち前なら2
    assert sum(b["free"] for b in ctx["buckets"]) <= 4 - len(positions)


def test_positions_with_an_unknown_bucket_name_are_still_counted():
    """知らない枠の名前でも数え落とさないこと。

    読まれないキーに入れてしまうと、「数えたつもりで数え落とす」という、
    この関数がいちばん避けたい形になる。
    """
    ctx = assemble([], [{"symbol": "1111.T", "bucket": "なんとなく"}],
                   {}, SETTINGS, SCREEN, dropped=[], capital=550_000.0)
    by_name = {b["name"]: b for b in ctx["buckets"]}
    assert by_name["じっくり"]["used"] == 1
    assert sum(b["used"] for b in ctx["buckets"]) == 1
