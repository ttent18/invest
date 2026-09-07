# 通知（Web Push）の準備

## 1. 鍵を作る

「この通知は確かに自分の仕組みが送った」と証明するための鍵を作る。
1回だけ作れば、あとはずっと同じものを使う。

    export PATH="$HOME/.local/bin:$PATH"
    mkdir -p ~/vapid && cd ~/vapid
    uv run --with pywebpush vapid --gen

    # ブラウザに渡す公開鍵（画面に埋め込むので、見えて構わない）
    uv run --with pywebpush vapid --applicationServerKey

    # GitHub Secrets に入れる秘密鍵（1行の文字列にする）
    uv run --with pywebpush python - <<'EOF'
    from py_vapid import Vapid01
    from py_vapid.utils import b64urlencode
    v = Vapid01.from_file("private_key.pem")
    raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    print(b64urlencode(raw))
    EOF

**秘密鍵は誰にも見せない。** 公開鍵は画面に埋め込むので、見えて構わない。

## 2. GitHub に登録する

リポジトリの Settings → Secrets and variables → Actions → New repository secret

| 名前 | 値 |
|---|---|
| `VAPID_PRIVATE_KEY` | 上で出た秘密鍵 |
| `VAPID_SUBJECT` | `mailto:自分のメールアドレス` |

`VAPID_SUBJECT` は「送り主は誰か」を示すもの。通知サーバーが問題を見つけたときの
連絡先として使われる。メールアドレスの前に `mailto:` を付けること。

公開鍵は計画2-B（画面）で Cloudflare 側に登録する。それまで手元に控えておく。
