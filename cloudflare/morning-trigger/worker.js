// 毎朝のサイト更新を、Cloudflare Workers の Cron Triggers から開始する Worker。
//
// GitHub Actions の schedule は混雑時に数時間遅れることがあるため、
// 時刻に正確な Cloudflare から workflow_dispatch（手動実行と同じ扱い）で起動する。
// GitHub 側の schedule は予備として残している（同時実行は concurrency で順番待ちになる）。
//
// Cloudflare ダッシュボードでの設定:
//   - シークレット GITHUB_TOKEN: このリポジトリだけを対象にした fine-grained トークン
//     （権限は Actions: Read and write のみ）
//   - Cron Trigger: 40 21 * * *（UTC。日本時間 毎日 6:40）
//
// このファイルはリポジトリでの管理用。実際に動くのは Cloudflare に貼り付けたコード。

const OWNER = "kimura-yukihiro-hub";
const REPO = "auto-deal-tracker";
const WORKFLOW = "update.yml";

async function dispatch(env) {
  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "morning-trigger-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  if (res.status !== 204) {
    // Cloudflare の「ログ」に残る。トークンの期限切れ・権限不足のときはここに出る
    throw new Error(`GitHub への実行依頼に失敗しました: HTTP ${res.status} ${await res.text()}`);
  }
  console.log("GitHub Actions の実行を依頼しました");
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },

  // URL を開かれても実行はしない（誰でも何度も起動できてしまうのを防ぐ）
  async fetch() {
    return new Response("morning-trigger: 毎朝 6:40（日本時間）にサイト更新を開始します。\n", {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  },
};
