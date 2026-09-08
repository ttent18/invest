"""通知が本当に iPhone まで届くかを、20秒で確かめるためだけのジョブ。

**なぜ必要か:** 通知は分析（20〜40分）や朝の確認のいちばん最後に送られる。
「30分待って、来なかった」では、次のどれが原因なのか切り分けられない。

- 通知の宛先が1件も登録されていない
  （ホーム画面のアイコンからではなく Safari のタブで「通知を受け取る」を押した、等）
- 送る側の秘密鍵（VAPID_PRIVATE_KEY）と、画面に配られた公開鍵が対になっていない
- 宛先は登録されているが、Apple 側で失効している
- そもそも分析が失敗していて、送る段まで届いていない

このジョブは、**上の1つ目から3つ目だけ**を切り離して確かめる。
GitHub の Actions の画面に「登録されている宛先: N 件」「送れた: N 件」と出るので、
通知が来ないときにどこを直せばよいかが分かる。

**本番の通知とは文面を変えてある。** 練習の通知を、本物の
「損切りに届きました」と読み違えると危ない。
"""

import sys
from datetime import datetime, timedelta, timezone

from investment.db import connect, select_push_subscriptions
from investment.notify import send as notify_send

JST = timezone(timedelta(hours=9))


def main() -> int:
    with connect() as conn:
        subscriptions = select_push_subscriptions(conn)
        print(f"登録されている通知の宛先: {len(subscriptions)} 件")

        if not subscriptions:
            print(
                "宛先が1件も登録されていません。\n"
                "iPhone の場合、**ホーム画面に追加したアイコンから開いて**"
                "「通知を受け取る」を押す必要があります。\n"
                "Safari のタブで押しても登録されません（Apple の仕様）。"
            )
            return 1

        now = datetime.now(JST).strftime("%-H:%M")
        sent, failed = notify_send(
            conn,
            title="【練習】通知は届いています",
            body=f"{now} に送った、動作を確かめるための通知です。本物の知らせではありません",
            url="/",
        )

    print(f"送れた: {sent} 件 / 送れなかった: {failed} 件")

    if sent == 0:
        print(
            "1件も送れませんでした。次を疑ってください。\n"
            "  1. VAPID_PRIVATE_KEY / VAPID_SUBJECT が GitHub Secrets に無い\n"
            "  2. 送る側の秘密鍵と、Cloudflare に入れた公開鍵が対になっていない\n"
            "     （どちらか片方だけ作り直すと、こうなります）\n"
            "  3. VAPID_SUBJECT が `mailto:` で始まっていない"
        )
        return 1

    print("iPhone に通知が出れば成功です。出なければ、iPhone 側の通知の許可を確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
