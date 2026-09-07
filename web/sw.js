// このファイル（Service Worker）は通知を受け取って表示するためだけに使う。
//
// 【意図して "fetch" は横取りしない・何もキャッシュしない】
// この画面（今日やること／保有／収支）が出す数字は、いま持っている株数・
// いまの株価・いまの提案であって、どれも「古いものを見せると実際の売買を
// 間違えさせる」種類の情報。PWAというと「オフラインでも開けるようにキャッシュ
// すべきでは」と足したくなるが、ここでは逆に、キャッシュした画面が
// 一瞬でも出てしまうことの害（古い株価で買い増してしまう、決着した提案が
// まだ残って見える、など）の方が、オフラインで開けないことの害より大きいと
// 判断している。次にこのファイルを触る人へ：fetch を扱いたくなったら、
// まずこのコメントと task-9 の計画を読み直してください。
//
// 送られてくる通知の中身は src/investment/notify.py の send() が作る
// JSON文字列（{ "title": "…", "body": "…", "url": "/" }）。

// 新しいバージョンの Service Worker をすぐに使う（更新のたびにタブを
// 全部閉じないと切り替わらない、という事態を避けるため）。
// キャッシュを持たないので、古いバージョンに切り替えて事故になる心配もない。
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// 【ここが唯一の安全装置であることについて】
// この仕組みで実際に利用者の資産を守っているのは「損切りの知らせが
// ちゃんと届くこと」。通知の中身（JSON）の読み取りが失敗しても、
// 通知そのものは必ず出す（内容を「新しい知らせがあります」に落とすだけで、
// 通知を諦めない）。ここで例外を握りつぶさずに投げてしまうと、
// event.waitUntil が失敗し、通知が一切表示されなくなる。
self.addEventListener("push", (event) => {
  let parsed = {};
  try {
    parsed = event.data ? event.data.json() : {};
  } catch (err) {
    // 中身が壊れていた（JSONとして読めなかった）。中身を諦めるだけで、
    // 通知を出すこと自体はこの下で続ける。
    parsed = {};
  }
  if (!parsed || typeof parsed !== "object") {
    parsed = {};
  }

  const title = typeof parsed.title === "string" && parsed.title ? parsed.title : "投資";
  const body = typeof parsed.body === "string" && parsed.body ? parsed.body : "新しい知らせがあります";
  const url = typeof parsed.url === "string" && parsed.url ? parsed.url : "/";

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      // icon-192.png は用意していない（このタスクで作るのは icon-180.png と
      // icon-512.png のみ）。無い画像を指すと通知に絵が出ないだけで害は
      // 無いが、どうせなら実在するファイルを指す。
      icon: "/icon-512.png",
      data: { url },
    })
  );
});

// 通知をタップしたら、対応する画面を開く。
//   損切りの知らせ → /holdings.html（保有）
//   新しい提案の知らせ → /（今日やること）
// どちらの画面を開くかは notify.py 側が url に入れて送ってくる値で決まる
// （ここでは判定しない。url をそのまま使うだけ）。
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    (async () => {
      try {
        // 既に同じ画面のタブが開いていれば、増やさずにそれを前面に出す。
        const allClients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        for (const client of allClients) {
          if (client.url.endsWith(url) && "focus" in client) {
            return client.focus();
          }
        }
        return self.clients.openWindow(url);
      } catch (err) {
        // ここで失敗しても（openWindow が使えない状況など）通知を出す処理
        // 自体は既に終わっている。タップ後に画面が開かないだけで済むよう、
        // 最後にもう一度だけ素朴に開こうとする。
        return self.clients.openWindow("/");
      }
    })()
  );
});
