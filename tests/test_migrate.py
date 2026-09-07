"""本番でマイグレーションが実行されることを守るテスト。

2026-09-07 に、apply_migrations() を呼ぶのがテストだけだったため、
v3 で追加した bucket 列が本番に無いまま「そんな列は無い」で止まった。
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from investment.jobs.migrate import main

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_main_applies_every_migration():
    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with (
        patch("investment.jobs.migrate.connect", side_effect=lambda: FakeConn()),
        patch("investment.jobs.migrate.apply_migrations") as apply,
    ):
        assert main() == 0

    apply.assert_called_once()


@pytest.mark.parametrize("workflow", ["analyze.yml", "screen.yml", "morning.yml"])
def test_every_database_workflow_runs_migrations_first(workflow: str):
    """データベースを使うワークフローは、必ず先に構造を更新すること。

    これを忘れると、列を足した日に本番だけが古い構造のまま動き、
    「そんな列は無い」で止まる。順序も見る（更新が後ろにあっては意味がない）。
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    if "DATABASE_URL" not in text:
        pytest.skip(f"{workflow} はデータベースを使いません")

    assert "investment.jobs.migrate" in text, f"{workflow} にマイグレーションの手順がありません"
    first_db_use = min(
        text.index(m)
        for m in ("investment.jobs.build_context", "investment.jobs.run_screen",
                  "investment.jobs.morning_check", "investment.jobs.apply_decision")
        if m in text
    )
    assert text.index("investment.jobs.migrate") < first_db_use, (
        f"{workflow} で、マイグレーションがデータベースを使う処理より後になっています"
    )


@pytest.mark.parametrize("workflow", ["analyze.yml", "morning.yml"])
def test_fills_are_applied_before_anything_reads_the_positions(workflow: str):
    """記録の反映を、保有を読む処理より先に行うこと。

    反映しないまま分析すると、既に買った銘柄をもう一度勧めることになる。
    朝の確認も、買ったばかりの銘柄を見落とす。
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    assert "investment.jobs.apply_fills" in text, f"{workflow} に反映の手順がありません"

    readers = ("investment.jobs.build_context", "investment.jobs.morning_check")
    first_reader = min(text.index(m) for m in readers if m in text)
    assert text.index("investment.jobs.apply_fills") < first_reader, (
        f"{workflow} で、記録の反映が保有を読む処理より後になっています"
    )


@pytest.mark.parametrize("workflow", ["analyze.yml", "morning.yml"])
def test_the_notification_keys_reach_the_step_that_sends_them(workflow: str):
    """通知を送るステップに鍵が渡っていること。

    渡し忘れると、鍵が無いという記録だけが毎回積もる。
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    assert "VAPID_PRIVATE_KEY" in text
    assert "VAPID_SUBJECT" in text
