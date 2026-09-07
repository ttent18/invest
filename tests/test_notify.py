"""通知の送信のテスト。実際には送らず、送信の関数を差し替えて確認する。

差し替えるのは外部の通知サーバーへの送信だけで、宛先の読み込み・
失効した宛先の削除・失敗の記録は実際の処理を通す。
"""

from unittest.mock import patch

from investment.notify import send


class FakeWebPushException(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.response = type("R", (), {"status_code": status_code})()


def _subs() -> list[dict]:
    return [
        {"endpoint": "https://push.test/a", "p256dh": "k1", "auth": "a1"},
        {"endpoint": "https://push.test/b", "p256dh": "k2", "auth": "a2"},
    ]


def test_send_delivers_to_every_registered_device():
    with (
        patch("investment.notify.select_push_subscriptions", return_value=_subs()),
        patch("investment.notify.webpush") as push,
        patch("investment.notify.VAPID_PRIVATE_KEY", "dummy"),
        patch("investment.notify.VAPID_SUBJECT", "mailto:test@example.com"),
    ):
        ok, failed = send(conn=None, title="提案が3件", body="タップして確認")

    assert (ok, failed) == (2, 0)
    assert push.call_count == 2


def test_send_removes_a_device_that_no_longer_exists():
    """通知サーバーが404/410を返したら、その宛先は失効している。

    残したまま送り続けると毎回失敗が記録され、本当の失敗が埋もれる。
    """
    def fake_push(subscription_info, **kwargs):
        if subscription_info["endpoint"].endswith("/a"):
            raise FakeWebPushException(410)

    with (
        patch("investment.notify.select_push_subscriptions", return_value=_subs()),
        patch("investment.notify.WebPushException", FakeWebPushException),
        patch("investment.notify.webpush", side_effect=fake_push),
        patch("investment.notify.delete_push_subscription") as delete,
        patch("investment.notify.record_gap") as gap,
        patch("investment.notify.VAPID_PRIVATE_KEY", "dummy"),
        patch("investment.notify.VAPID_SUBJECT", "mailto:test@example.com"),
    ):
        ok, failed = send(conn=None, title="t", body="b")

    assert (ok, failed) == (1, 1)
    delete.assert_called_once()
    assert delete.call_args.args[1] == "https://push.test/a"
    gap.assert_not_called()


def test_send_records_a_failure_that_is_not_an_expired_device():
    """一時的な失敗は宛先を消さず、記録だけ残す。"""
    with (
        patch("investment.notify.select_push_subscriptions", return_value=_subs()[:1]),
        patch("investment.notify.WebPushException", FakeWebPushException),
        patch("investment.notify.webpush", side_effect=FakeWebPushException(500)),
        patch("investment.notify.delete_push_subscription") as delete,
        patch("investment.notify.record_gap") as gap,
        patch("investment.notify.VAPID_PRIVATE_KEY", "dummy"),
        patch("investment.notify.VAPID_SUBJECT", "mailto:test@example.com"),
    ):
        ok, failed = send(conn=None, title="t", body="b")

    assert (ok, failed) == (0, 1)
    delete.assert_not_called()
    gap.assert_called_once()


def test_send_does_nothing_when_no_device_is_registered():
    with patch("investment.notify.select_push_subscriptions", return_value=[]):
        assert send(conn=None, title="t", body="b") == (0, 0)


def test_send_records_a_gap_when_the_key_is_missing_instead_of_crashing():
    """鍵が未設定でも、分析や朝の確認を巻き添えにしないこと。

    通知は補助であって本体ではない。届かなくてもアイコンを開けば同じ情報が見える。
    """
    with (
        patch("investment.notify.select_push_subscriptions", return_value=_subs()),
        patch("investment.notify.VAPID_PRIVATE_KEY", ""),
        patch("investment.notify.record_gap") as gap,
    ):
        ok, failed = send(conn=None, title="t", body="b")

    assert (ok, failed) == (0, 0)
    gap.assert_called_once()
    assert "鍵" in gap.call_args.kwargs["detail"]
