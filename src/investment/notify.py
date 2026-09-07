"""スマホへの通知（Web Push）を送る。

**通知は補助であって本体ではない。** 届かなくても、ホーム画面のアイコンを
開けば同じ情報が見える。そのため、通知の失敗で分析や朝の確認を
巻き添えにしない。失敗した事実は data_gaps に残す。

送るのは「利用者が動く必要がある」ときだけ（設計書 6.2）。
提案0件や、何も起きていない朝には送らない。判断は呼び出し側が行う。
"""

import json
import os

from pywebpush import WebPushException, webpush

from investment.db import delete_push_subscription, record_gap, select_push_subscriptions

# VAPID は「この通知は確かに自分のサーバーが送った」と証明するための鍵。
# 秘密鍵は GitHub Secrets にだけ置く（Cloudflare 側には置かない）。
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "")

# 宛先が失効していることを表す応答。この2つのときだけ宛先を削除する。
EXPIRED_STATUS = (404, 410)


def send(conn, title: str, body: str, url: str = "/") -> tuple[int, int]:
    """登録されている全ての端末に通知を送る。戻り値は (送れた件数, 送れなかった件数)。

    url は通知をタップしたときに開く場所。
    """
    subscriptions = select_push_subscriptions(conn)
    if not subscriptions:
        return 0, 0

    if not VAPID_PRIVATE_KEY or not VAPID_SUBJECT:
        record_gap(
            conn,
            scope="notify:no_key",
            detail=(
                "通知の鍵（VAPID_PRIVATE_KEY / VAPID_SUBJECT）が設定されていないため、"
                f"{len(subscriptions)} 件の宛先に送れませんでした"
            ),
        )
        return 0, 0

    payload = json.dumps({"title": title, "body": body, "url": url})
    ok = failed = 0

    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except WebPushException as exc:
            failed += 1
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in EXPIRED_STATUS:
                delete_push_subscription(conn, sub["endpoint"])
                continue
            record_gap(
                conn,
                scope="notify:failed",
                detail=f"通知を送れませんでした（宛先 {sub['endpoint']}）: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - 理由は下のコメントを参照
            # WebPushException（宛先が失効している場合）以外は、型を絞らずに
            # 全て受け止める。pywebpush は内部で requests（通信そのものの
            # 失敗）や、鍵の形式が壊れているときの ValueError など、こちらが
            # 網羅しきれない種類の例外を投げてくる。ここは通知という補助機能
            # であり、投げ直して main() まで落としてしまうと、既に保存済みの
            # 提案や朝の確認の判定まで巻き添えにしてしまう（このファイル冒頭
            # の約束を破る）。狭く捕まえる利点（想定外の型に気づけること）は
            # 代わりに record_gap で必ず記録することで確保する。
            failed += 1
            record_gap(
                conn,
                scope="notify:failed",
                detail=f"通知を送れませんでした（宛先 {sub['endpoint']}）: {exc}",
            )
        else:
            ok += 1

    return ok, failed
