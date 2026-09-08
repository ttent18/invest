import { neon } from "@neondatabase/serverless";

// Neon への接続。
//
// Cloudflare の環境では通常のPostgres接続（TCP）が使えないため、
// Neon が用意しているHTTP経由のドライバを使う。
// 接続文字列は Cloudflare の環境変数から取る。画面側には一切渡さない。

export function db(env) {
  if (!env.DATABASE_URL) {
    throw new Error("DATABASE_URL が設定されていません");
  }
  return neon(env.DATABASE_URL);
}
