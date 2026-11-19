# セールの見張り番：作業の決まり

## 仕組み
- 毎朝 GitHub Actions（`.github/workflows/update.yml`）が `fetch_and_build.py` を動かし、RSS・楽天ランキング・`articles/*.md` から `dist/` を作って公開する。
- `dist/` は自動で作られるので直接編集しない。直すときは元（`fetch_and_build.py`・`articles.py`・`articles/*.md`・`data/featured.json`）を直す。
- `data/deals_log.json`・`data/ai_draft_history.json` などは自動更新が書き換える。手で整形し直さない。

## 記事
- 書き方は `articles/_template.md`。ファイル名は半角英小文字・数字・ハイフン。`draft: true` は公開されない。
- 価格・セール期間は「掲載時点」と分かるように書き、出典のない数字や「最安」「No.1」などの断定は書かない。
- 公開する記事に【編集者メモ】【要確認】を残さない（ビルドが警告する）。
- AI の下書きを仕上げて公開するときは `finish-draft` スキルの手順で行う（体験・選んだ理由はユーザー本人の言葉だけ。【要確認】は公式の情報で確かめてから外す）。

## 広告（PR）とアフィリエイト
- 楽天市場のリンクは `affiliate.py` でもしもアフィリエイトのリンクに変える。リンクの属性（`rel="nofollow sponsored noopener"`）と PR 表記は消さない（景品表示法のステマ規制のため）。
- セール判定（`digest.py` の `is_sale_title`）を変えたら、`tests/test_affiliate_and_sale.py` に例を足す。

## 確認とプッシュ
- 変えたら `python -m pytest` を通す（テストが落ちるとサイトは更新されない）。
- `main` へのプッシュは、生成処理や記事の変更なら本番サイトの作り直しがすぐ走る。プッシュはユーザーに確かめてから行う。
- 秘密情報（楽天のキー、Gemini のキーなど）は GitHub の Secrets にだけ置き、コードやログに書かない。
