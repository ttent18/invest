"""通知の経路だけを試すジョブ（investment.jobs.notify_test）のテスト。

このジョブは「通知が来ない」ときに、原因を切り分けるための道具である。
道具そのものが黙って嘘をつくと、切り分けの役に立たない。
"""

from unittest.mock import patch

from investment.jobs import notify_test


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run(subscriptions, send_result):
    with (
        patch.object(notify_test, "connect", return_value=_Conn()),
        patch.object(
            notify_test, "select_push_subscriptions", return_value=subscriptions
        ),
        patch.object(notify_test, "notify_send", return_value=send_result) as sender,
    ):
        code = notify_test.main()
    return code, sender


def test_it_fails_when_no_device_is_registered(capsys):
    """宛先が0件なら、送らずに失敗として終わること。

    ここで成功として終わると、「送れた（が0件だった）」を成功と読み違える。
    通知が来ない一番よくある原因が、まさに宛先0件（Safari のタブで
    「通知を受け取る」を押した）なので、ここははっきり落とす。
    """
    code, sender = _run([], (0, 0))

    assert code == 1
    sender.assert_not_called()
    out = capsys.readouterr().out
    assert "0 件" in out
    assert "ホーム画面" in out, "宛先0件のときに、直し方が案内されていません"


def test_it_succeeds_when_at_least_one_device_received_it(capsys):
    code, sender = _run([{"endpoint": "https://example/1"}], (1, 0))

    assert code == 0
    sender.assert_called_once()
    assert "送れた: 1 件" in capsys.readouterr().out


def test_it_fails_when_nothing_could_be_sent(capsys):
    """宛先はあるのに1件も送れなかったら、失敗として終わること。

    公開鍵と秘密鍵が対になっていない場合がこれに当たる。
    成功として終わると、鍵の食い違いに永久に気づけない。
    """
    code, _ = _run([{"endpoint": "https://example/1"}], (0, 1))

    assert code == 1
    out = capsys.readouterr().out
    assert "対に" in out, "鍵が対になっていない可能性が案内されていません"


def test_the_practice_notice_says_it_is_not_a_real_one():
    """本物の知らせと読み違えないよう、練習であることが文面に入っていること。

    「損切りに届きました」と読み違えると、持っていない株を売ろうとする。
    """
    _, sender = _run([{"endpoint": "https://example/1"}], (1, 0))

    _, kwargs = sender.call_args
    assert "練習" in kwargs["title"]
    assert "本物の知らせではありません" in kwargs["body"]
