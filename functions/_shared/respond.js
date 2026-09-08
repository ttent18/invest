// 画面に返す応答を組み立てる。
//
// 失敗のときに返すのは「画面に出す日本語」だけにする。データベースの
// エラー本文などをそのまま返すと、接続先や表の構造が画面から見えてしまう。

export function ok(data) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export function fail(status, message) {
  return new Response(JSON.stringify({ message }), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
