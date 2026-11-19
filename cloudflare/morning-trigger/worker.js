// 毎朝の GitHub Actions を、Cloudflare Workers の Cron Triggers から開始する Worker。
//
// GitHub Actions の schedule は混雑時に数時間遅れたり、実行されなかったりするため
// （note-auto-writer は 2026-11-17〜19 に一度も schedule で動かなかった）、
// 時刻に正確な Cloudflare から workflow_dispatch（手動実行と同じ扱い）で起動する。
// 各リポジトリの schedule は予備として残している（同時実行は concurrency で順番待ち、note は 1 日の本数に上限あり）。
//
// Cloudflare の設定:
//   - シークレット GITHUB_TOKEN: auto-deal-tracker と note-auto-writer を対象にした fine-grained トークン
//     （権限は Actions: Read and write のみ）
//   - Cron Triggers: wrangler.toml の [triggers]（UTC）。下の JOBS と同じ時刻にする
//
// 反映: このフォルダーで `npx wrangler@4 deploy`（シークレットはそのまま残る）

const OWNER = "kimura-yukihiro-hub";

// Cron の式（UTC）ごとに、起動するワークフロー
const JOBS = {
  "17 20 * * *": { repo: "note-auto-writer", workflow: "daily.yml", label: "note の毎日の記事（日本時間 5:17）" },
  "40 21 * * *": { repo: "auto-deal-tracker", workflow: "update.yml", label: "セールの見張り番のサイト更新（日本時間 6:40）" },
};

async function dispatch(env, job) {
  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${job.repo}/actions/workflows/${job.workflow}/dispatches`,
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
    // Cloudflare の「ログ」に残る。トークンの期限切れ・権限不足（403）のときはここに出る
    throw new Error(`${job.label} の実行依頼に失敗しました: HTTP ${res.status} ${await res.text()}`);
  }
  console.log(`${job.label} の実行を依頼しました`);
}

export default {
  async scheduled(event, env, ctx) {
    const job = JOBS[event.cron];
    if (!job) {
      console.log(`対応するワークフローがない Cron です: ${event.cron}`);
      return;
    }
    ctx.waitUntil(dispatch(env, job));
  },

  // URL を開かれても実行はしない（誰でも何度も起動できてしまうのを防ぐ）
  async fetch() {
    return new Response("morning-trigger: 毎朝決まった時刻に GitHub Actions を開始します。\n", {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  },
};
