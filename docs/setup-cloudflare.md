# Cloudflare の設定手順（スマホから使えるようにする）

この文書は、**画面（今日やること／保有／収支）をインターネットに置いて、
自分の iPhone からだけ使えるようにする**ための手順です。

上から順に、飛ばさずにやってください。所要時間は 1〜2 時間くらいです。
途中で分からなくなっても、**設定はあとから何度でも直せます。**
消えて困るものは作りません。

---

## 0. 先に知っておくこと（ここだけは読んでから始める）

### 0-1. iPhone のホーム画面のアプリでは、もう一度ログインが必要です

このあと **Cloudflare Access**（＝「決めた人以外はこのURLを開けない」ようにする鍵）を
かけます。iPhone では、

- **Safari のタブで開いたとき**と
- **ホーム画面に追加したアイコンから開いたとき**

で、**ログインの記憶（cookie）が別々に保管されます**（Apple の仕組みで、共有されません）。

つまり **Safari でログイン済みでも、ホーム画面のアイコンから開くと、
そこでもう一度ログインを求められます。** これは異常ではありません。

さらに、**ホーム画面のアイコンから開いた状態でログイン画面に飛ぶと、
iPhone は「アプリの中の小さなブラウザ」でログイン画面を開きます。**
このとき、ログインし終わっても元のアプリに戻ってこられないことがあります
（iPhone でよく報告されている症状です）。

そのため、この手順書では次の対策を取ります。

| 対策 | どこでやるか |
|---|---|
| ログインの有効期限を **1か月** にして、ログインし直す回数を最小にする | 手順5-4 |
| ホーム画面に追加したあと、**その場でログインが通ることを必ず確かめる** | 手順6-2 |

**もしホーム画面のアイコンからログインできなかった場合の逃げ道**は、
「9. うまくいかないとき」の一番下に書いてあります。
**先に読んでおく必要はありません。** 詰まったときに戻ってきてください。

### 0-2. 秘密の値をこの文書に書き足さないこと

これから、**他人に見られたら困る文字列**をいくつか扱います
（データベースの接続文字列、GitHub のトークン、通知の秘密鍵）。

- **この文書（`docs/setup-cloudflare.md`）に、実際の値を書き込まないでください。**
  この文書は Git に入っていて、履歴からは消せません
- 貼り付ける先は、Cloudflare の画面と GitHub の画面だけです
- 手元に控えるなら、パスワード管理アプリか、Git に入らない場所に

### 0-3. `VAPID_PRIVATE_KEY` は Cloudflare に入れません

通知には**公開鍵**と**秘密鍵**という一対の文字列を使います。

- **公開鍵**（`VAPID_PUBLIC_KEY`）は **Cloudflare** に入れます。
  これは画面（ブラウザ）に配られる前提のもので、見られても問題ありません
- **秘密鍵**（`VAPID_PRIVATE_KEY`）は **GitHub の Secrets** に入れます。
  **絶対に Cloudflare に入れないでください**

理由: Cloudflare に入れた環境変数は、`functions/api/state.js` が
`env.VAPID_PUBLIC_KEY` として読み、**画面に配ります**。
うっかり `VAPID_PUBLIC_KEY` という名前で秘密鍵を入れると、
**秘密鍵がブラウザに配られてしまいます。**
そうなったら鍵を作り直すしかありません。

---

## 1. 出てくる言葉（1行ずつ）

分からない言葉が出てきたら、ここに戻ってきてください。

| 言葉 | 意味 |
|---|---|
| デプロイ | 手元にあるファイルを、インターネット上の場所に置いて、URLで開けるようにすること |
| Cloudflare Pages | HTML などのファイルを預かって、URLで配ってくれるサービス。今回の「置き場所」 |
| 環境変数 | プログラムに外から渡す設定値（名前と値の組）。**ファイルに書かずに済ませたい秘密の値**を渡すのに使う |
| ビルド | 素材のファイルを、配れる形に変換する作業。今回は変換するものが無いので**行いません** |
| ビルド出力ディレクトリ | 「このフォルダの中身を配ってください」と指定するフォルダ。今回は `web` |
| API | 画面の裏側で、データを読んだり書いたりする窓口。今回は `functions/` の中のファイルたち |
| エンドポイント | API の窓口ひとつひとつの住所。例: `/api/state`（いまの状態を読む窓口） |
| Cloudflare Access | 「決めたメールアドレスの人以外は、このURLを開けない」ようにする鍵 |
| GitHub Actions | GitHub の上で、決めた時刻やボタンで自動的にプログラムを動かす仕組み |
| トークン | 「私はこの人です」と機械に名乗るための、長いパスワードのような文字列 |
| Secrets | GitHub 側で秘密の値を預かる場所。入れたあとは自分でも中身を見られない |
| PWA | ホーム画面にアイコンを置いて、アプリのように開けるようにしたウェブページ |
| 通知（プッシュ通知） | アプリを開いていなくても iPhone に届くお知らせ |
| ワークフロー | GitHub Actions で動く一連の作業の定義。`.github/workflows/` の中のファイル |

---

## 2. 全体の形（何がどこで動くか）

```
  iPhone のホーム画面のアイコン
        │  開く
        ▼
  Cloudflare Access（鍵）── 自分以外はここで止まる
        │  通過
        ▼
  Cloudflare Pages
    ├── 画面のファイル      … リポジトリの web/ の中身
    └── API                 … リポジトリの functions/ の中身
            │
            ├── /api/state          → Neon（データベース）を読む
            ├── /api/fills          → Neon に「買った/売った」を記録
            ├── /api/proposals/{番号}/skip → Neon に「見送った」を記録
            ├── /api/subscribe      → Neon に通知の宛先を記録
            └── /api/analyze        → GitHub Actions の analyze を起動

  GitHub Actions（毎朝 7:00 と、ボタンを押したとき）
    └── Python が Neon を読み書きし、通知を送る
```

**大事な点:** 画面と API は Cloudflare、計算と通知は GitHub Actions、
データは Neon（データベース）にあります。**3か所に設定を入れます。**

| 設定を入れる場所 | 何を入れるか | この文書のどこ |
|---|---|---|
| Cloudflare Pages | `DATABASE_URL` / `GITHUB_TOKEN` / `VAPID_PUBLIC_KEY` | 手順4 |
| GitHub Secrets | `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT`（＋既にある `DATABASE_URL` など） | 手順5 |
| Cloudflare Access | 自分のメールアドレスだけ許可 | 手順6 |

---

## 3. 手順1: GitHub のトークンを作る

**なぜ:** 画面の「いま分析して」ボタンは、Cloudflare から GitHub に
「analyze というワークフローを動かして」と頼みます
（`functions/api/analyze.js`）。そのときに名乗るための文字列が要ります。

**なぜ権限を絞るか:** この文字列が漏れたときに、できることを小さくするためです。
「全部できるトークン」が漏れると、リポジトリの中身を書き換えられます。

### 3-1. 作る

1. GitHub を開いて、右上の自分のアイコン → **Settings**
2. 左の一番下の **Developer settings**
3. **Personal access tokens** → **Fine-grained tokens**
   （fine-grained ＝「細かく権限を選べる」という意味）
4. 右上の **Generate new token**
5. 次のように入力します

| 欄 | 入れる値 |
|---|---|
| Token name | `investment-pwa-analyze`（自分が見て分かる名前なら何でもよい） |
| Expiration | **必ず期限を選ぶ**。`90 days` でよい。`No expiration` は選ばない |
| Description | `画面の「いま分析して」ボタン用`（空でもよい） |
| Resource owner | 自分のアカウント（`ttent18`） |
| Repository access | **Only select repositories** を選び、`ttent18/invest` **だけ**を選ぶ |

6. **Repository permissions** を開きます。ここが一番大事です

| 権限 | 設定 |
|---|---|
| **Actions** | **Read and write** |
| Metadata | **Read-only**（Actions を選ぶと自動で付きます。そのままでよい） |
| **それ以外すべて** | **No access**（初期状態のまま、何も触らない） |

   - `Contents` や `Secrets` に権限を付けないこと。**付ける必要がありません**
   - **Organization permissions** は何も触らない

7. 一番下の **Generate token** を押す

### 3-2. 控える

- 画面に `github_pat_` で始まる長い文字列が出ます
- **この画面を閉じると、二度と見られません**
- **すぐにコピーして**、手順4-3 で Cloudflare に貼り付けるまで、
  どこかに一時的に置いておいてください（貼り付けたら消してよい）
- **この文書には書かないこと**

> 期限が切れると「いま分析して」だけが効かなくなります（他は動き続けます）。
> そのときは同じ手順で作り直し、手順4-3 の値を差し替えてください。

---

## 4. 手順2: Cloudflare Pages のプロジェクトを作る

### 4-1. Neon の接続文字列を先に用意する

**なぜ先に:** プロジェクト作成の直後に貼り付けたいからです。

1. [console.neon.tech](https://console.neon.tech) を開いてログイン
2. このプロジェクトのデータベースを選ぶ
3. **Dashboard**（または **Connection Details**）の
   **Connection string** をコピー
4. `postgresql://` で始まる長い文字列です。**これも文書に書かないこと**

> これは GitHub Secrets に入っているものと同じ、本番のデータベースへの
> 接続文字列です。GitHub Secrets は入れたあと中身を見られないので、
> 見られない場合は Neon の画面から取り直してください。

### 4-2. プロジェクトを作る

1. [dash.cloudflare.com](https://dash.cloudflare.com) を開いてログイン
   （アカウントが無ければ、ここで無料で作れます）
2. 左のメニューから **Workers & Pages**
   （新しい画面では **Compute** の中にあります）
3. **Create application** → **Pages** タブ → **Connect to Git**
4. GitHub でログインを求められます。**Install & Authorize** を押します

   **ここで何を許可することになるか:**
   - Cloudflare の GitHub アプリ（`Cloudflare Workers and Pages`）が、
     **選んだリポジトリの中身を読めるようになります**
     （中身を取ってきて配るので、読めないと動きません）
   - コミットに「ビルドが成功／失敗した」という印を付けます
   - **必ず `Only select repositories` を選んで、`ttent18/invest` だけにしてください。**
     `All repositories` を選ぶと、他の全部のリポジトリまで読めるようになります
   - 非公開リポジトリでも動きます（Cloudflare の公式ドキュメントに
     「private and public repositories are supported」とあります）
   - あとから取り消せます: GitHub の
     Settings → Applications → Installed GitHub Apps

5. リポジトリの一覧から **`invest`** を選び、**Begin setup**

### 4-3. ビルドの設定（そのまま入力してください）

**Set up builds and deployments** の画面で、次のとおりに入れます。

| 欄 | 入れる値 |
|---|---|
| Project name | `invest`（URLの一部になります。あとから変えられません） |
| Production branch | **`main`** |
| Framework preset | **None**（一覧の一番上。何も選ばない） |
| Build command | **空のまま**（何も入力しない） |
| Build output directory | **`web`** |
| Root directory (advanced) → Path | **空のまま**（何も入力しない） |

**それぞれの理由:**

- **Production branch が `main`** …… 本番として配るのは `main` ブランチの中身だから。
  他のブランチは「お試し用のURL」になります（手順6で、そちらにも鍵をかけます）
- **Framework preset が None** …… React などの枠組みを使っていないから。
  何か選ぶと、存在しないビルドコマンドが勝手に入って失敗します
- **Build command が空** …… 変換するものが無いから。
  素の HTML と JavaScript を、そのまま配るだけです
- **Build output directory が `web`** …… **`web/` の中身だけ**を配るため。
  **ここを空にしたり `/` にしたりすると、リポジトリ全体
  （`src/` の中身も `journal/` の中身も）がインターネットに公開されます。**
  必ず `web` と入れてください
- **Root directory が空** …… `functions/` はリポジトリの一番上にあり、
  Cloudflare は一番上から探すからです（次の 4-4 を読んでください）

4-4 と 4-5 を読んでから、**Save and Deploy** を押します。

### 4-4. 【重要】`functions/` は今の場所（リポジトリの一番上）で正しい

「配るのは `web/` なのに、`functions/` は `web/` の外にある。これで API は動くのか？」
—— **動きます。今の置き場所が正しい形です。**

Cloudflare の公式ドキュメントにこう書かれています。

> Make sure that the `/functions` directory is at the root of your Pages project
> (and not in the static root, such as `/dist`).
> （`/functions` フォルダは Pages プロジェクトの一番上に置くこと。
> `/dist` のような配布用フォルダの中ではない）
>
> — https://developers.cloudflare.com/pages/functions/get-started/

つまり **`functions/` を `web/` の中に移動してはいけません。**
移動すると API として認識されなくなり、そのうえ API のソースコード
（`DATABASE_URL` の使い方などが書かれたファイル）が、
そのままインターネットに配られてしまいます。

**このリポジトリの現状:**

```
invest/
├── functions/      ← ここ（一番上）で正しい。触らない
│   ├── _shared/
│   └── api/
├── web/            ← Build output directory に指定するのはこちら
└── package.json
```

**確認方法:** デプロイ後、手順8-2 で「数字が出るか」を見れば分かります。
数字が出れば `functions/` は正しく API として動いています。

### 4-5. ビルドコマンドが空でも `npm install` は走ります（そのままでよい）

`package.json` があるので、Cloudflare は**ビルドコマンドが空でも**
依存パッケージを `npm install` で入れます。

> Cloudflare Pages installs your project dependencies, builds the project,
> and deploys it to Cloudflare's global network.
>
> — https://developers.cloudflare.com/pages/get-started/git-integration/

**これは止めないでください。必要な動作です。**
`functions/_shared/db.js` が `@neondatabase/serverless`
（Neon に接続するための部品）を読み込んでいるので、
これが入っていないと API が動きません。

- **`npm run build` は走りません。** 走るのは、Build command 欄に書いたものだけです。
  空にしてあるので、変換は何も起きません
- `SKIP_DEPENDENCY_INSTALL` という環境変数は**設定しないでください**。
  設定すると `@neondatabase/serverless` が入らず、API が全滅します
- `vitest`（テスト用の部品）も一緒に入りますが、動かないだけで害はありません

> **もしビルドが「build command が無い」という趣旨のエラーで失敗したら**、
> Build command 欄に `exit 0` とだけ入れて、もう一度デプロイしてください
> （`exit 0` は「何もせず成功しました」という意味の合図です）。
> Cloudflare のドキュメントには両方の書き方が載っています。
> まず空で試し、駄目なら `exit 0` にする、の順で構いません。

### 4-6. デプロイの結果を見る

- **Save and Deploy** を押すと、黒い画面にログが流れます
- 数分で **Success!** と出て、`https://invest.pages.dev` のような
  **URL が表示されます**
- **このURLを控えてください。** 以降ずっと使います
- **この時点ではまだ、URLを知っている人は誰でも開けます。**
  手順6で鍵をかけるまで、人に教えないでください
- **この時点で開いても、数字は出ません**（データベースの設定がまだ）。
  それで正常です

失敗したら → 「9. うまくいかないとき」へ。

---

## 5. 手順3: Cloudflare に環境変数を登録する

**なぜここに置くか:** これらは**画面（ブラウザ）からは読めない場所**に置く必要が
あります。ブラウザに配られるものは、全部見えると考えてください。
Cloudflare の環境変数は、API（`functions/` の中）からだけ読めます。

### 5-1. 登録する場所

1. Cloudflare の **Workers & Pages** → 作った **invest** を選ぶ
2. **Settings** タブ → **Variables and Secrets**
   （画面によっては **Environment variables**）
3. **Production** の側であることを確かめる
4. **Add** を押して、1つずつ入れます

### 5-2. 入れるもの（3つ）

| 名前 | 何のためか | どこから取るか | 種類 |
|---|---|---|---|
| `DATABASE_URL` | データベース（Neon）に繋ぐため。これが無いと保有も提案も何も出ない | Neon の画面の **Connection string**（手順4-1） | **Secret** |
| `GITHUB_TOKEN` | 「いま分析して」ボタンから GitHub Actions を起動するため | 手順3で作ったトークン（`github_pat_` で始まる） | **Secret** |
| `VAPID_PUBLIC_KEY` | 通知の公開鍵。画面に配られる前提のもので、見られても問題ない | `docs/setup-push.md` の手順で作って控えたもの（`~/vapid/` に置いたなら、`vapid --applicationServerKey` が出した値） | Text でも Secret でもよい |

- **名前は1文字も違えないこと。** 大文字・小文字も含めて、上の表のとおりに。
  `DATABASE_URL` を `DATABASEURL` と書くと、API は「設定されていません」として失敗します
- 値を貼るとき、**前後に空白や改行が混ざらないように**気を付けてください
- **Secret を選ぶと、登録後は自分でも中身を見られなくなります。** それでよいのです。
  見たくなったら Neon や GitHub から取り直します

### 5-3. 入れてはいけないもの

| 名前 | なぜ入れないか |
|---|---|
| `VAPID_PRIVATE_KEY` | **`functions/api/state.js` は `VAPID_PUBLIC_KEY` を画面に配ります。** 名前を間違えて秘密鍵を入れると、秘密鍵がインターネットに配られます。秘密鍵は GitHub Secrets（手順6）です |
| `SKIP_DEPENDENCY_INSTALL` | 設定すると `@neondatabase/serverless` が入らず、API が全部止まります |
| `CLAUDE_CODE_OAUTH_TOKEN` | Cloudflare 側では使いません。GitHub Actions だけが使います |

### 5-4. 反映させる

**環境変数を足しただけでは反映されません。**
入れ終わったら、必ずもう一度デプロイし直します。

1. **Deployments** タブを開く
2. 一番上（最新）のデプロイの右の **…** → **Retry deployment**
3. Success! になるまで待つ

---

## 6. 手順4: GitHub Secrets（通知を送る側）

**なぜ:** 通知を実際に送るのは、Cloudflare ではなく **GitHub Actions 上の Python**
（`src/investment/notify.py`）です。だから通知の秘密鍵は GitHub 側に入れます。

`.github/workflows/morning.yml` と `analyze.yml` が、
`secrets.VAPID_PRIVATE_KEY` と `secrets.VAPID_SUBJECT` を読んでいます。

### 6-1. 入れる場所

1. GitHub で `ttent18/invest` を開く
2. **Settings** タブ → 左の **Secrets and variables** → **Actions**
3. **New repository secret** を押す

### 6-2. 入れるもの

| 名前 | 値 | どこから取るか |
|---|---|---|
| `VAPID_PRIVATE_KEY` | 通知の**秘密鍵** | `docs/setup-push.md` の手順で作って控えたもの |
| `VAPID_SUBJECT` | **`mailto:` で始まる自分のメールアドレス** | 自分で書く |

- `VAPID_SUBJECT` は必ず **`mailto:` から始めます**。
  例の形: `mailto:` のあとに自分のメールアドレスを続けるだけです。
  `mailto:` を忘れると、通知サービスに受け付けてもらえません
- これは「この通知の送り主は誰か」を示すもので、通知サービス側が
  問題を見つけたときの連絡先として使います

### 6-3. 公開鍵と秘密鍵は一対です

**Cloudflare に入れた `VAPID_PUBLIC_KEY` と、GitHub に入れた
`VAPID_PRIVATE_KEY` は必ず同じときに作った一対でなければならず、
片方だけ入れ替えると通知は黙って届かなくなります**
（エラーも出ません。ただ届かなくなります）。

作り直すときは、**必ず両方を同時に差し替えてください。**
差し替えたあとは、iPhone で「通知を受け取る」をもう一度押す必要があります。

### 6-4. すでに入っているはずのもの（確認だけ）

同じ Secrets の画面に、次があることを確かめてください。無ければ足します。

| 名前 | 使う場所 |
|---|---|
| `DATABASE_URL` | 全部のワークフロー |
| `CLAUDE_CODE_OAUTH_TOKEN` | `analyze.yml`（AIが判断する部分） |

---

## 7. 手順5: Cloudflare Access で鍵をかける

**なぜ絶対に必要か:**

いまの状態では、**URLを知った人は誰でも次のことができます。**

| できてしまうこと | どの窓口 |
|---|---|
| 保有している銘柄と、いくら儲かっている／損しているかを全部見る | `/api/state` |
| **「いま分析して」を何度でも押して、GitHub Actions を回し続ける** | `/api/analyze` |
| **勝手に「この銘柄を買った」という記録を入れる**（保有と現金が実際と食い違う） | `/api/fills` |
| 提案を勝手に「見送った」ことにする | `/api/proposals/{番号}/skip` |

**`/api/analyze` と `/api/fills` に、パスワードの類は一切ありません。**
守っているのは Cloudflare Access だけです。ここは飛ばさないでください。

### 7-1. 【重要】ボタン1つでは足りません

Cloudflare Pages の設定にある **Enable access policy** というボタンは、
**「お試し用のURL」だけを守ります。**
**本番のURL（`invest.pages.dev`）は守りません。**

Cloudflare の公式ドキュメントにこう書かれています。

> Note that this will only protect your preview deployments ... and not your
> `*.pages.dev` domain or custom domain.
> （これはお試し用の配布だけを守り、`*.pages.dev` のURLは守りません）
>
> — https://developers.cloudflare.com/pages/configuration/preview-deployments/

本番のURLも守るには、**下の 7-2 を最後までやる必要があります。**
手順は Cloudflare の公式ドキュメント
https://developers.cloudflare.com/pages/platform/known-issues/#enable-access-on-your-pagesdev-domain
に載っているものと同じです。

### 7-2. 本番のURLに鍵をかける

1. Cloudflare の **Workers & Pages** → **invest** → **Settings** → **General**
2. **Enable access policy** を押す
   （この時点ではまだ「お試し用のURL」だけが守られています）
3. 出てきた案内の **Manage** を押す。**Zero Trust** の画面に移ります
   - Zero Trust を初めて使う場合、**チーム名（team name）を決めてください**と
     言われます。好きな英字（例: 自分のニックネーム）でよく、
     `〇〇.cloudflareaccess.com` というログイン用のURLになります。
     料金プランを聞かれたら **Free** を選びます（50人まで無料）
4. **Access** → **Applications** の一覧から、**自分のプロジェクト**を選ぶ
5. **Configure** を押す
6. **Public hostname** の **Subdomain** の欄に入っている
   **アスタリスク（`*`）を消して、空にして** **Save** を押す
   - 「同じ名前のアプリがある」というエラーが出たら、
     **Application name** を `invest-production` などに変えてから保存します
   - **これで本番のURL（`invest.pages.dev`）が守られました**
7. Pages の設定に戻る:
   **Workers & Pages** → **invest** → **Settings** → **General** →
   **Enable access policy** を**もう一度押す**
8. **Zero Trust** → **Access** → **Applications** に、
   **2つ**並んでいることを確かめる

| アプリ | 守る対象 |
|---|---|
| `invest.pages.dev` | **本番のURL**（iPhone から使うもの） |
| `*.invest.pages.dev` | お試し用のURL |

**2つ揃っていなければ、まだ守られていません。** 6 に戻ってやり直してください。

### 7-3. 自分だけが通れるようにする

**2つのアプリそれぞれ**について、次を行います。

1. アプリを選んで **Configure** → **Policies**
2. ポリシー（通す条件）を確認、または **Add a policy** で作る
3. 次のように設定します

| 欄 | 設定 |
|---|---|
| Policy name | `自分だけ` |
| Action | **Allow** |
| Include → Selector | **Emails** |
| Include → Value | **自分のメールアドレス**（1つだけ） |

4. **Save**

- **Include に `Everyone` や `Emails ending in` が入っていないか、必ず見てください。**
  入っていたら消します。**そこが開いていると、鍵をかけた意味がなくなります**
- 複数の条件が並んでいる場合、**どれか1つに当てはまれば通れます**。
  余計な行は消してください

### 7-4. ログインの方法と、有効期限を1か月にする

1. **Zero Trust** → **Settings** → **Authentication** で、
   ログインの方法（login method）を確かめます
   - **One-time PIN**（メールに届く数字を入れる方式）は**最初から使えます。
     追加の設定は要りません。まずはこれで構いません**
   - Google アカウントでログインしたい場合は、
     **Add new** → **Google** を選んで設定します。手間は増えますが、
     ログインは速くなります
2. **手順7-2 で作った 2つのアプリそれぞれ**について、
   **Configure** → **Settings**（または **Session Duration**）で
   **Session Duration を `1 month` にします**

   **なぜ:** 初期値は 24時間です。24時間ごとに、iPhone の
   ホーム画面のアプリの中でログインし直すことになります。
   **これは 0-1 に書いた「戻ってこられないことがある」問題を、
   毎日引く**ことを意味します。1か月にすれば、月に1回で済みます

---

## 8. 手順6: 他人が開けないことを、実際に確かめる

**設定したつもりで開いたままだった、が一番怖い失敗です。**
「保存した」だけでは確かめたことになりません。**必ず実際に開いてください。**

### 8-1. 自分は開けること

1. パソコンの普段のブラウザで、`https://invest.pages.dev` を開く
2. **ログイン画面が出る** → 自分のメールアドレスを入れる →
   メールに届いた数字（またはGoogleログイン）で通る
3. **「今日やること」の画面が出る** → 成功

出なければ → 7-3 の Include に自分のメールアドレスが入っているか確認。

### 8-2. 他人は開けないこと（**絶対にやること**）

1. **プライベートウィンドウ**（シークレットウィンドウ）を開く
   - Safari: ファイル → 新規プライベートウインドウ
   - Chrome: ファイル → 新しいシークレット ウィンドウ
2. そこに `https://invest.pages.dev` を貼って開く
3. **期待する結果: ログイン画面が出て、先に進めない**
4. さらに、**API の窓口も直接叩いてみます。**
   同じプライベートウィンドウで
   `https://invest.pages.dev/api/state` を開く
5. **期待する結果: ここでもログイン画面が出る**（JSONの数字が見えたら失敗）

**どちらかで中身が見えたら、それは世界中の誰でも見られる状態です。**
その場で 7-2 に戻ってやり直してください。
**特に手順7-2 の 6（アスタリスクを消す）を飛ばしていないか**を確かめてください。

- プライベートウィンドウでも通ってしまう場合、
  同じブラウザに既にログインが残っていることがあります。
  そのときは**別の端末**（家族のスマホなど）か、
  **モバイル回線のスマホのブラウザ**で試してください

---

## 9. 手順7: iPhone に入れる

### 9-1. ホーム画面に追加する

1. iPhone の **Safari** で `https://invest.pages.dev` を開く
   （**Chrome ではなく Safari。** ホーム画面への追加は Safari から行います）
2. ログインする
3. 画面の下（機種によっては上）の **共有ボタン（□ の中に ↑ の絵）** を押す
4. メニューを下にたどって **「ホーム画面に追加」** を押す
5. 名前が **投資** になっていることを確かめて、右上の **追加**
6. ホーム画面にアイコンが増えます

**なぜホーム画面に追加するのか:**
**iPhone では、ホーム画面に追加したアイコンから開いた場合にしか通知が届きません。**
Safari のタブで開いているだけでは、絶対に届きません。
**これは Apple の仕様で、回避する方法はありません。**

（画面自体もこのことを知っていて、タブで開いている間は
「通知を受け取るには、ホーム画面への追加が必要です」という案内を出し、
「通知を受け取る」ボタンを表示しません。`web/index.html` の
`renderInstallBanner` がその処理です。）

### 9-2. アイコンから開いて、もう一度ログインする

1. **ホーム画面のアイコンを押します**（Safari のタブからではなく）
2. **ここでもう一度ログインを求められます。これは正常です**
   （0-1 に書いた、記憶が別々に保管される件）
3. ログインして、「今日やること」の画面が出れば成功です

**ここでログインできない／ログイン後に画面が戻ってこない場合は、
先に進まず「10. うまくいかないとき」の一番下を見てください。**

### 9-3. 通知を許可する

1. **ホーム画面のアイコンから開いた状態**で、
   「今日やること」の画面を下にたどります
2. **「通知」** というカードの中の **「通知を受け取る」** ボタンを押します
3. iPhone が「"投資" は通知を送信します。よろしいですか？」と聞いてきます →
   **「許可」** を押します
4. ボタンの文字が **「通知を受け取ります」** に変わり、
   押せなくなれば登録完了です

**慎重に押してください:**
**この確認は一度きりです。ここで「許可しない」を押すと、
アプリ側からはもう二度と聞き直せません。**
間違えた場合は、iPhone の
**設定 → 通知 → 投資 → 「通知を許可」をオン** で直せます
（アプリの一覧の中から「投資」を探してください）。

**「通知を受け取る」ボタンが押せない（灰色）場合:**
`VAPID_PUBLIC_KEY` が Cloudflare に入っていないか、名前が間違っています。
手順5-2 に戻ってください。画面には
「通知の設定がまだ済んでいません。」と出ているはずです。

---

## 10. 動いていることの確かめ方（上から順に、1つずつ）

**必ず上から順にやってください。** 下の項目は、上の項目が通っていることを
前提にしています。上が駄目なのに下を試すと、原因が分からなくなります。

### 10-1. 画面が開く

| | |
|---|---|
| **何をする** | ホーム画面のアイコンから開く |
| **成功** | 「今日やること」という見出しと、下に「今日やること／保有／収支」の3つの切り替えが出る |
| **失敗したら疑うところ** | Cloudflare の Deployments が Success になっているか。Build output directory が `web` になっているか（`web` でないと、HTMLが見つからず 404 になる） |

### 10-2. 数字が出る（＝ `DATABASE_URL` が正しい）

| | |
|---|---|
| **何をする** | 下の「保有」を押して、保有の画面を開く |
| **成功** | 保有が0件なら **「いま保有している銘柄はありません。」** と出る。持っていれば銘柄が並ぶ。**赤い「いまの状態を読み込めませんでした」が出ないこと** |
| **失敗したら疑うところ** | ① `DATABASE_URL` の名前・値。② 手順5-4 の「Retry deployment」を忘れていないか。③ `functions/` を動かしていないか（一番上にあること） |

> 「いま保有している銘柄はありません。」と「読み込めませんでした」は**まったく別**です。
> 前者は「本当に0件」、後者は「データベースに繋がっていない」。
> API は、問い合わせが1つでも失敗したら全体を失敗にする作りなので
> （`functions/api/state.js`）、**空っぽの画面が出ているなら、
> それは本当に空っぽです。**

### 10-3. 他人が開けない（Access）

| | |
|---|---|
| **何をする** | 手順8-2 をもう一度やる（プライベートウィンドウで URL と `/api/state` の両方） |
| **成功** | どちらもログイン画面で止まる |
| **失敗したら疑うところ** | 手順7-2 の 6（Subdomain のアスタリスクを消す）を飛ばしている。または 7-3 の Include に `Everyone` が残っている |

### 10-4. 「いま分析して」が動く（＝ `GITHUB_TOKEN` が正しい）

| | |
|---|---|
| **何をする** | 「今日やること」の画面の一番下の **「いま分析して」** を押す |
| **成功** | 画面に **「分析を始めました。数分かかります」** と出る。GitHub の **Actions** タブを見ると、**analyze** が動き出している |
| **失敗したら疑うところ** | 「GitHubへの接続の設定がされていません」→ `GITHUB_TOKEN` が Cloudflare に無い。「GitHubへの接続が拒否されました。トークンの設定を確認してください」→ トークンの権限（Actions が Read and write か）か、期限切れ。「分析の設定が見つかりません」→ リポジトリ名か `analyze.yml` の名前が違う |

> 分析は 20〜40分かかることがあります（AIが1銘柄ずつ調べるため）。
> 押したあと、画面を閉じてしまって構いません。

### 10-5. 通知が届く

| | |
|---|---|
| **何をする** | 10-4 の分析が最後まで終わるのを待つ（GitHub の Actions で緑のチェックが付くまで）。提案が1件以上できると通知が飛びます |
| **成功** | iPhone に通知が届く。押すと「今日やること」の画面が開く |
| **失敗したら疑うところ** | ① ホーム画面のアイコンから開いて「通知を受け取る」を押したか。② 公開鍵と秘密鍵が一対か（6-3）。③ `VAPID_SUBJECT` が `mailto:` で始まっているか。④ GitHub Actions のログに通知のエラーが出ていないか |

> **提案が0件のときは通知は来ません。** これは正しい動作です。
> 「届かない」と判断する前に、GitHub Actions のログで
> 提案が何件できたかを見てください。

### 10-6. 記録が反映される

| | |
|---|---|
| **何をする** | 提案の「買った」を押して、株数と値段を入れて記録する |
| **成功（すぐ）** | 「今日やること」の画面の一番上に **「反映できていない記録が1件あります」** と出る |
| **成功（あとで）** | **翌朝7:00 の morning ワークフローのあと**、または**「いま分析して」を押したあと**に、その表示が消えて「保有」の画面に出る |
| **失敗したら疑うところ** | いつまでも「反映できていない記録が…あります」のまま → GitHub Actions の morning が失敗している。Actions タブで赤い×が付いていないか見る |

**なぜすぐ保有に出ないのか:**
記録した時点では `fills` という「受付簿」に1行書かれるだけで、
保有株数や現金の計算は行いません（`functions/api/fills.js`）。
その計算は GitHub Actions 上の Python
（`morning.yml` と `analyze.yml` の「記録された売買を反映する」の段）が行います。
**同じ計算を2か所に持たないため**に、こうしてあります。

**急ぎたいときは「いま分析して」を押してください。**
analyze も、分析の前に必ず記録を反映します。

### 10-7. 二重に記録されない

| | |
|---|---|
| **何をする** | 「買った」の記録ボタンを、続けて2回押してみる |
| **成功** | 記録は1件のまま。エラーにもならない |
| **失敗したら疑うところ** | 2件になったら、データベースの `fills` 表の `client_key` に一意制約が付いていない。`migrations/` が適用されているか（GitHub Actions の「データベースの構造を最新にする」の段）を確認 |

---

## 11. うまくいかないとき

### 11-1. ログの見かた

| 何のログか | どこを見るか |
|---|---|
| **画面が配られるまで**（ビルド・デプロイの失敗） | Cloudflare → **Workers & Pages** → **invest** → **Deployments** → 該当の行 → **View details**。黒い画面に全部出ます |
| **API の失敗**（数字が出ない、記録できない） | Cloudflare → **Workers & Pages** → **invest** → **Logs**（または **Functions** → **Real-time Logs**）。**開いている間に**画面を操作すると、`console.error` の中身が流れます |
| **分析・通知・記録の反映の失敗** | GitHub → `ttent18/invest` → **Actions** タブ。赤い×の行を開き、失敗した段（ステップ）を押すと中身が出ます |

- Cloudflare のログは**開いている間しか流れません**。
  先にログの画面を開いてから、iPhone で操作してください
- API のエラーメッセージは、画面には短い日本語しか出しません
  （データベースの構造などが漏れないようにするため）。
  **本当の原因は Cloudflare のログにあります**

### 11-2. よくある間違いと、その症状

| 症状 | 疑うところ | 直しかた |
|---|---|---|
| 画面は開くが、赤い「いまの状態を読み込めませんでした」 | `DATABASE_URL` の名前か値。または環境変数を入れたあとデプロイし直していない | 手順5-2 と 5-4 |
| 画面すら開かない（404 / Not Found） | Build output directory が `web` になっていない | 手順4-3 |
| **リポジトリの中身（`src/` や `journal/`）まで見えてしまう** | Build output directory が空か `/` になっている | **すぐに `web` に直してデプロイし直す**。手順4-3 |
| 「いま分析して」で「GitHubへの接続の設定がされていません」 | `GITHUB_TOKEN` が Cloudflare に無い、または名前違い | 手順5-2 |
| 「いま分析して」で「GitHubへの接続が拒否されました」 | トークンの権限が足りない（Actions が Read and write でない）か、期限切れ | 手順3 で作り直して、手順5-2 で差し替え |
| 「いま分析して」で「分析の設定が見つかりません」 | リポジトリ名か、`analyze.yml` というファイル名が合っていない | `functions/api/analyze.js` の宛先と、`.github/workflows/analyze.yml` の名前を見比べる |
| **通知だけが届かない** | ① ホーム画面に追加していない ② アイコンからではなくタブから「通知を受け取る」を押した ③ 公開鍵と秘密鍵が一対でない | 手順9-1・9-3、そして 6-3 |
| 「通知を受け取る」が灰色で押せない | `VAPID_PUBLIC_KEY` が Cloudflare に無い | 手順5-2 |
| 通知は来るが、押しても画面が開かない | 通知の中の行き先。実害は小さい | GitHub Actions のログで、送った通知の中身を確認 |
| 「反映できていない記録が…あります」が何日も残る | morning ワークフローが失敗している | GitHub → Actions → morning の赤い行を開く |
| 保有の数字が実際と食い違う | **他人に `/api/fills` を叩かれた可能性**。または自分の入力間違い | まず手順8-2 で Access が効いているか確認。効いていなければ最優先で直す |
| しばらく使っていなかったら、開いても何も出ない／読み込めない | **Access のログインの期限切れ**（`DATABASE_URL` ではありません） | いったんアプリを閉じて開き直し、ログインし直す。頻繁なら手順7-4 で期限を `1 month` に |
| ビルドが `npm` のエラーで失敗する | `package.json` と `package-lock.json` が食い違っている | 手元で `npm install` を実行し、`package-lock.json` の変更をコミットして push |

### 11-3. ホーム画面のアイコンからログインできないとき

0-1 に書いた、iPhone 特有の問題です。次の順に試してください。

1. **アプリを完全に閉じてから開き直す**
   （ホームバーを上にスワイプして、アプリのカードを上に払う）
2. **ホーム画面のアイコンを削除して、追加し直す**
   - アイコンを長押し → 「ブックマークを削除」
   - Safari で `https://invest.pages.dev` を開いて**ログインしてから**、
     手順9-1 をやり直す
3. **Safari の設定を確かめる**
   - 設定 → Safari → **「サイト越えトラッキングを防ぐ」を一時的にオフ**にして、
     もう一度アイコンから開いてみる（ログインが通ったら戻してよい）
4. **ログインの方法を One-time PIN に変える**
   （手順7-4）。Google ログインより経由する場所が少なく、通りやすいことがあります
5. **それでも駄目なら**
   - **Safari のタブから使う**という選び方もあります。
     画面（提案の確認・記録・収支）は普通に使えます。
     **ただし通知は絶対に届きません**（9-1 の理由）
   - この場合、損切りの知らせが届かなくなります。
     **朝に自分で開いて確認する習慣が必要**になります

---

## 12. 最後に、消してよいもの

設定が全部終わって、10 の確認が通ったら:

- **手元に一時的に控えた `GITHUB_TOKEN` の文字列を消してください**
  （Cloudflare に入っていれば、手元の控えは不要です）
- Neon の接続文字列の一時的な控えも同じです
- **VAPID の鍵（`~/vapid/`）は消さないでください。**
  公開鍵と秘密鍵が一対でなくなると通知が止まり、作り直しになります

---

## 付録: 参照した公式ドキュメント

| 内容 | URL |
|---|---|
| `functions/` はプロジェクトの一番上に置く | https://developers.cloudflare.com/pages/functions/get-started/ |
| ビルドコマンド・出力ディレクトリ・ルートディレクトリの意味 | https://developers.cloudflare.com/pages/configuration/build-configuration/ |
| GitHub と繋いでデプロイする手順、依存パッケージの自動インストール | https://developers.cloudflare.com/pages/get-started/git-integration/ |
| GitHub 連携で許可される範囲、取り消しかた | https://developers.cloudflare.com/pages/configuration/git-integration/github-integration/ |
| **Enable access policy はお試し用のURLしか守らない** | https://developers.cloudflare.com/pages/configuration/preview-deployments/ |
| **`*.pages.dev` 本番URLにも Access をかける手順** | https://developers.cloudflare.com/pages/platform/known-issues/#enable-access-on-your-pagesdev-domain |
| Access のログインの有効期限（初期値は24時間） | https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/session-management/ |
| Access のログイン記憶（cookie）の仕組み | https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/ |
