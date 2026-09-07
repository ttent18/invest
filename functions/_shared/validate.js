// POST /api/fills が受け取った内容の検証。
//
// ここは純粋関数（データベースにもネットワークにも一切触らない）。
// 「入力の形が正しいか」だけをここで確認する。保有株数や現金の計算は
// 一切行わない（それは Python 側の仕事）。
//
// 返り値は、利用者に見せてよい日本語のエラー文の配列。
// 問題が無ければ空配列を返す。

// 日本株かどうかは「.T」で終わるかどうかで決める。大文字・小文字が
// 混ざって送られてきても同じ銘柄として扱うため、比較の前に大文字へ
// そろえる。symbol から通貨（円かドルか）を決める側（fills.js）も、
// 必ずこの関数を通して同じ規則を使うこと（規則がずれると、日本株なのに
// 100株単位の検査を素通りしたり、通貨がドル扱いになったりする）。
export function isJapaneseStock(symbol) {
  return typeof symbol === "string" && symbol.toUpperCase().endsWith(".T");
}

function isReadableDate(value) {
  // 数値（1970年からのミリ秒として解釈できてしまう）や真偽値のような、
  // 「日時のつもりで送ったのではない値」を弾く。文字列だけを日時として扱う。
  if (typeof value !== "string") return false;
  const t = new Date(value).getTime();
  return !Number.isNaN(t);
}

export function validateFill(fill) {
  const errors = [];
  const { client_key, proposal_id, symbol, side, quantity, price, fee, traded_at } = fill ?? {};

  // 二重送信よけの鍵。これが無いと「2回押しても1回分しか記録しない」が
  // 実現できない。
  if (typeof client_key !== "string" || client_key.trim() === "") {
    errors.push("二重に記録されるのを防ぐための鍵（client_key）が指定されていません。もう一度お試しください");
  }

  // 銘柄コード。以降の日本株判定に使うので、無ければここで弾く。
  if (typeof symbol !== "string" || symbol.trim() === "") {
    errors.push("銘柄コードを入力してください");
  }

  // 売り買いの種類。
  if (side !== "buy" && side !== "sell") {
    errors.push("買ったか売ったか（side）を正しく指定してください");
  }

  // 株数。まず「1以上の整数か」を確認し、それを満たす場合に限って
  // 日本株の売買単位（100株単位）を確認する。137株のように整数だが
  // 単位に合わない場合と、0株や負の株数のように整数として成立しない
  // 場合を、別々の理由として1件ずつ返すため。
  if (!Number.isInteger(quantity) || quantity < 1) {
    errors.push("株数は1以上の整数で入力してください");
  } else if (isJapaneseStock(symbol) && quantity % 100 !== 0) {
    errors.push("株数は100株単位で入れてください（日本株は100株ずつしか売買できません）");
  }

  // 値段。0円や、入力ミスで負の値になっているものを弾く。
  if (!(typeof price === "number" && Number.isFinite(price) && price > 0)) {
    errors.push("値段は0より大きい金額を入力してください");
  }

  // どの提案（枠）に対する売買かを表す proposal_id。
  // 買いは、どの提案から引き継ぐかが分からないと記録できないので必須。
  // 売りは、提案を経由せずに手放すこともあるので必須にしない。
  // ただし、buy/sell を問わず「指定されているのに数字として成立していない
  // 値（"abc" や 1.5 や {} など）」は、そのままデータベースに渡すと
  // 型のエラーで失敗するので、ここで弾く。
  const hasProposalId = proposal_id !== null && proposal_id !== undefined;
  if (side === "buy" && !hasProposalId) {
    errors.push("買いは、どの提案（枠）に対する売買かを指定してください");
  } else if (hasProposalId && !(Number.isInteger(proposal_id) && proposal_id >= 1)) {
    errors.push("どの提案に対する売買かが正しく指定されていません");
  }

  // 手数料。省略してもよい（fills.js 側で0として扱う）が、指定された
  // のに0未満だったり数字として成立していない値は弾く。マイナスの
  // 手数料をそのまま記録すると、あとで Python 側が費用として足すときに
  // 現金が黙って増えてしまうため。
  if (fee !== null && fee !== undefined) {
    if (!(typeof fee === "number" && Number.isFinite(fee) && fee >= 0)) {
      errors.push("手数料は0以上の数字で入力してください");
    }
  }

  // 取引日時。入力自体は無くてもよいが、入力されたのに日時として
  // 読めない値は弾く（黙って無視すると、あとで気づけない）。
  if (traded_at !== null && traded_at !== undefined && traded_at !== "" && !isReadableDate(traded_at)) {
    errors.push("取引した日時の形式が正しくありません");
  }

  return errors;
}
