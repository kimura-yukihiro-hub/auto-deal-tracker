#!/usr/bin/env python3
"""Gemini API で「選び方の記事」の下書きを作り、GitHub Issue で知らせるスクリプト。

使い方（GitHub Actions から呼び出す）:
    python ai_draft.py            # 日曜日なら下書きを作成し、作成したパスを GITHUB_OUTPUT に出力
    python ai_draft.py --notify P # 作成した下書き P について GitHub Issue を作成

環境変数:
    GEMINI_API_KEY : Google AI Studio の API キー（未設定なら何もしない）
    GEMINI_MODEL   : 使うモデル（既定: gemini-3.6-flash）
    FORCE_AI_DRAFT : "1" なら曜日に関係なく作成（動作確認用）

方針:
- その週のセールで多かった製品について、記事の「型」（選び方・2製品の比較・後悔しないチェック・場面別の組み合わせ・
  セール前の準備）を順番に変えながら作る（週刊セールまとめと内容がかぶらないように）
- 執筆役とは別に確認役（Gemini を別の指示で呼ぶ）が採点し、だめなら 1 回だけ書き直させて、良いほうを下書きにする
- どの型でも「結論の早見表」「楽天の商品欄（編集者が記入）」「体験欄」「短いタイトル」の基準を守る
- 下書きは draft: true（非公開）で articles/<AI が付けた英小文字の名前>.md に保存する。人が確認して公開する
- AI には data/deals_log.json の直近7日分（公開ニュースのタイトル・要約・URL）と、既存記事の一覧を渡す
- 一般的な知識で書いた部分のうち、数字やルールなど確認が必要なところには【要確認：…】を付けさせる
- 渡していない URL へのリンクは自動で外す（AI が URL を作り出すのを防ぐ）
- 失敗してもサイトの更新には影響しない（常に終了コード 0）
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_phrases import ai_phrase_problem  # noqa: E402  AI によく出る言い回し（natural-japanese の一覧）

BASE_DIR = Path(__file__).resolve().parent
ARTICLES_DIR = BASE_DIR / "articles"
DEALS_LOG_PATH = BASE_DIR / "data" / "deals_log.json"
HISTORY_PATH = BASE_DIR / "data" / "ai_draft_history.json"  # どの型で書いたか（同じ型が続かないように）
FEATURED_PATH = BASE_DIR / "data" / "featured.json"
JST = timezone(timedelta(hours=9))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DRAFT_WEEKDAY = 6  # 日曜日
MAX_INPUT_ITEMS = 25
TIMEOUT = 120
MAX_TITLE_LENGTH = 32
AVOID_RECENT_FORMATS = 2   # 直近この本数で使った型は避ける
RECENT_FOR_PROMPT = 6      # AI に「構成を変えて」と見せる直近の記事数

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EDITOR_MEMO = "【編集者メモ：選んだ理由や、自分が重視したいポイントを一言追記】"
# 早見表・おすすめ商品の欄に置く。商品は人（または Claude）が楽天市場で実在・販売中を確かめてから入れる
PRODUCT_MEMO = "【編集者メモ：楽天市場で商品を選び、商品名・リンク・価格を記入】"
# 体験を書く場所に置く。使ったことのある製品の体験だけを書く
EXPERIENCE_MEMO = "【編集者メモ：自分が使っている製品や、使って良かったこと・困ったことを一言】"

SYSTEM_PROMPT = """あなたはセール情報サイト「セールの見張り番」の編集アシスタントです。
編集者が仕上げるための「製品の選び方」記事の下書きを日本語で作成します。

読者: スマホ・パソコン・家電を少しでもお得に買いたい人。機械に詳しくない人や、仕事でパソコンを使う人が中心。
- 専門用語はなるべく使わず、使う場合は短く言い換えや説明を添える（例: 「PD対応（スマホやノートPCを速く充電できる規格）」）。
- 「どんな人に向いているか」「何が便利か」を、生活や仕事の場面に結びつけて書く。
- 言い回し: たとえの動詞（炙り出す・飛ばす・波に飲まれる など）や大げさな名詞（構造を捨てる・〜を実現する など）を使わず、何がどう変わるかをふつうの言葉で書く。「〜することができる」「〜ようにするための」「〜を活用する」のような回りくどい言い方をせず、「〜できる」「〜ための」「〜を使う」と短く書く。

厳守するルール:
- 個別の商品の価格・割引率・仕様・セール期間は、与えられたニュースのデータに書かれていることだけを使う。推測で書かない。
- 選び方の説明には一般的な知識を使ってよい。ただし、具体的な数字の目安・法律や規格のルール・安全に関わる情報など、
  間違えると読者が困る内容には、その文の直後に「【要確認：何を確認すべきか】」と書き、編集者が確認できるようにする。
  自信のない内容は書かない。
- リンクには、与えられたニュースの URL と、既存記事のパス（/articles/〜/）だけを使う。URL を作り出したり変えたりしない。
- 自分が商品を使ったかのような体験談や感想は書かない。
  代わりに、編集者が意見を書き足す場所に「""" + EDITOR_MEMO + """」と書く（記事全体で1〜2か所）。
- おすすめ商品の名前・リンク・価格は自分で決めない。商品を入れる欄には「""" + PRODUCT_MEMO + """」と書き、
  どんな人向けの欄か（例: 「とにかく安く」「通勤向け」）と、選ぶときの条件だけを書く。
- セール情報を紹介するときは、出典メディア名とリンクを付ける。
- 誇張表現（「絶対」「最強」「必ず得する」など）を使わない。
- 本文は Markdown。見出しは ## と ### を使い、# は使わない。HTML タグは使わない。
- リンクは半角の [文字](URL) で書く（全角の ［］ や （） は使わない）。見出し・表・リストの前後には空行を入れる。
- 【要確認：…】には、編集者が調べやすいように「何を・どこで確認すればよいか」を具体的に書く。"""

# 記事の型。どの型でも「結論の早見表」「楽天の商品欄」「体験欄」を入れる（選び方記事の標準）
FORMATS = [
    {
        "name": "選び方",
        "topic": "セールの対象として多く出てきた製品の種類（例: ロボット掃除機、ノートパソコン）を1つ選ぶ",
        "title": "「〇〇の選び方｜△△別のおすすめ」の形（△△は使い方や予算）",
        "structure": """2. 「## 結論：使い方別のおすすめ早見表」: 「こんな人に | おすすめ | 価格の目安」の3列の表を1つ。行は使い方や予算で3〜4行。
   「おすすめ」と「価格の目安」の列は、すべて「""" + PRODUCT_MEMO + """」。表の下に「詳しくは後半の『予算別のおすすめ』で紹介しています」と一文。
3. 「選び方1：〜」のような ## 見出しで、選ぶときのポイントを3〜5個（向いている人や使う場面に結びつける）
4. 「買う前のチェックポイント」（番号付きリスト）
5. 「## 予算別のおすすめ〇〇」: 早見表と同じ区分で ### 見出しを作り、それぞれに向いている人と見るポイントを2〜3文と「""" + PRODUCT_MEMO + """」""",
    },
    {
        "name": "2製品の比較",
        "topic": "ニュースに出てきた製品のうち、よく比べられる2つ（具体的な2製品、または2つのタイプ。例: ノイキャン付きとなし、11インチと13インチ）を選ぶ",
        "title": "「AとB、どっちを買う？｜〇〇で比べた結論」の形",
        "structure": """2. 「## 結論：こんな人はA、こんな人はB」: 「こんな人に | 選ぶほう | 価格の目安」の3列の表（3〜4行）。価格の目安の列は「""" + PRODUCT_MEMO + """」。
3. 「## 違い1：〜」のような見出しで、違いを3〜4個（どんな場面で差が出るかを書く）。比較表を1つ入れる
4. 「## どっちでもいい人・どっちも合わない人」
5. 「## おすすめの商品」: A と B それぞれ ### 見出しで、向いている人を2〜3文と「""" + PRODUCT_MEMO + """」""",
    },
    {
        "name": "後悔しないチェック",
        "topic": "セールで買われやすいが、選び間違えやすい製品の種類を1つ選ぶ",
        "title": "「〇〇で後悔しないために｜買う前の△つのチェック」の形",
        "structure": """2. 「## 結論：後悔しない〇〇の早見表」: 「こんな人に | おすすめ | 価格の目安」の3列の表（3〜4行）。「おすすめ」と「価格の目安」は「""" + PRODUCT_MEMO + """」。
3. 「## よくある後悔1：〜」のような見出しで、買ったあとに多い後悔を4〜5個。それぞれ「なぜ起きるか」「買う前にどこを見れば防げるか」
4. 「## 買う前のチェックリスト」（番号付きリスト）
5. 「## 後悔しにくいおすすめ」: 早見表と同じ区分の ### 見出しで、2〜3文と「""" + PRODUCT_MEMO + """」""",
    },
    {
        "name": "場面別の組み合わせ",
        "topic": "ニュースのセール品が役立つ生活や仕事の場面（例: 在宅ワーク、新生活、出張・旅行）を1つ選び、そろえたい製品を3〜4種類決める",
        "title": "「〇〇にそろえたい△点｜予算別の組み合わせ」の形",
        "structure": """2. 「## 結論：予算別の組み合わせ早見表」: 「予算 | そろえるもの | 合計の目安」の3列の表（2〜3行）。「合計の目安」は「""" + PRODUCT_MEMO + """」。
3. 製品の種類ごとに ## 見出しを作り、それぞれ「なぜこの場面で必要か」「選ぶときのポイント」を書き、最後に「""" + PRODUCT_MEMO + """」
4. 「## 優先順位：予算が限られるなら」（どれから買うか）""",
    },
    {
        "name": "セール前の準備",
        "topic": "ニュースに出てきた大きなセール（例: ブラックフライデー、初売り、プライムデー）か、セールで値下がりしやすい製品の種類を選ぶ",
        "title": "「〇〇セールで狙いたい△△｜損しないための準備」の形",
        "structure": """2. 「## 結論：狙い目の早見表」: 「狙うもの | こんな人に | 価格の目安」の3列の表（3〜4行）。「価格の目安」は「""" + PRODUCT_MEMO + """」。
3. 「## セール前にやっておくこと」（番号付きリスト。価格の記録、ポイントの条件の確認など）
4. 「## 狙い目1：〜」のような見出しで、狙うものを3〜4個。それぞれ見るポイントと「""" + PRODUCT_MEMO + """」
5. 「## よくある失敗」（安く見えて得していない例など）""",
    },
]

USER_PROMPT_TEMPLATE = """以下は {start} から {end} までに主要IT・家電メディアで報じられた、スマホ・パソコン・家電のセール・値下げ関連ニュースです。

{items}

当サイトにすでにある記事（同じテーマは選ばない）:
{existing}

最近の AI 下書き（これと見出しの流れ・書き出し・言い回しを変える）:
{recent}

今回の記事の型: 「{format_name}」
テーマの選び方: {format_topic}
セール情報の一覧（週刊まとめ）は別の記事で公開しているので、セールの紹介が中心の記事にはしないでください。

構成（「結局どれを買えばいいの？」に早く答える順番にする）:
1. 導入（2〜3文）+ 「""" + EXPERIENCE_MEMO + """」。導入の最初の1文は40字以内で、読者の手が止まる一文（よくある悩みの言い当て、意外な事実など）。
   「今回は」「この記事では」「〜について解説します」で始めない。「〜ありませんか」「〜でしょうか」のような問いかけで始めない。
{format_structure}
   いずれか1つの区分の最後に「""" + EXPERIENCE_MEMO + """」。
6. 「### 最近のセールニュースから」: 上のニュースのうち、関係するセール情報を1〜3件、出典リンク付きで紹介する
   （価格はニュース掲載時点で、終わっている可能性があると書く）。該当がなければこの見出しごと省く
7. 「まとめ」（箇条書き）と、購入前に価格・在庫・セール期間をリンク先で確認するよう促す一文。
   最後の1文は「いかがでしたか」「参考になれば幸いです」のような決まり文句にせず、読者が次にやることが分かる一文にする
関係のある既存記事があれば、本文中でそのパスにリンクしてよい。
無駄な文（前置き、同じことの言い換え、見出しの直後の要約）は書かない。
見出しは短くし、【要確認：…】や価格の注記は見出しに入れず、本文に書く。

次の JSON 形式だけを出力してください（前後に説明文を付けない）:
{{"title": "記事タイトル（{format_title}。""" + str(MAX_TITLE_LENGTH) + """字以内）", "slug": "URL 用の英小文字とハイフンの名前（例: robot-vacuum-guide）", "description": "記事の紹介文（80〜120字）", "tags": ["タグ1", "タグ2"], "body": "本文（Markdown）",
  "product_searches": [{{"keyword": "楽天市場で商品を探す言葉（例: PCモニター 23.8インチ フルHD）", "min_price": 下限の円（なければ null）, "max_price": 上限の円（なければ null）}}]}}
product_searches は、結論の早見表の行と同じ順番・同じ数にする（各行に合う商品を、編集者が楽天市場で探すための条件）。
JSON には "x_posts" も入れる: X（旧Twitter）で記事を告知する文章を3つ（それぞれ100文字以内、URL は書かない、ハッシュタグは2つまで）。
切り口を変える（1つ目: 読者の迷いや失敗の代弁 / 2つ目: 記事で分かること / 3つ目: セールの時期との関係）。宣伝くささや誇張は避ける。"""

REVIEW_SYSTEM = """あなたはセール情報サイト「セールの見張り番」の厳しい編集長です。AI が書いた下書きを、自分では書き直さずに採点します。
次の観点を1〜5点で採点する。点数の目安（甘くつけない）:
  5 = プロの編集者が一切手を入れなくてよい。めったにつけない
  4 = 小さな直しで公開できる
  3 = 普通の下書き。何か所か直しが必要（多くの下書きはここ）
  2 以下 = 書き直しが必要
満点をつける項目があっても、改善点は必ず3つ以上 must_fix か nice_to_have に挙げる。
- 基準: 冒頭に「## 結論」の早見表があるか。商品欄が【編集者メモ：楽天市場で商品を選び…】になっていて、AI が商品名・価格を勝手に決めていないか
- 正確さ: ニュースにない価格・割引率・仕様を書いていないか。数字の目安や安全・規格の話に【要確認：…】が付いているか
- 読者: 機械に詳しくない人に分かる言葉か。使う場面に結びついているか
- 書き出し: 最初の1文が「〜ありませんか」「〜でしょうか」のようなありきたりの問いかけになっていないか。読者の手が止まる一文か
- 見出し: 見出しだけ読んでも内容が分かるか。見出しに【要確認】や長い注記が入っていないか
- タイトル: 短く、読者が「自分のことだ」と思えるか
- 無駄のなさ: 削っても意味が変わらない文がないか（あればそのまま wasteful_sentences に書き出す）
- 独自性: 最近の下書きと見出しの流れ・言い回しが似ていないか
出力は指定の JSON だけ。"""

REVIEW_PROMPT = """【最近の AI 下書き】
{recent}

【今回の下書き（型: {format_name}）】
タイトル: {title}

{body}

次の JSON だけを出力:
{{"scores": {{"基準": 1〜5, "正確さ": 1〜5, "読者": 1〜5, "書き出し": 1〜5, "見出し": 1〜5, "タイトル": 1〜5, "無駄のなさ": 1〜5, "独自性": 1〜5}},
  "must_fix": ["必ず直す点（どこを・どう直すか）"], "nice_to_have": ["直すとさらに良くなる点"],
  "wasteful_sentences": ["削るべき文をそのまま"],
  "pass": true または false（すべて4以上で must_fix が空なら true）}}"""

REVISION_PROMPT = """{base}

【前回の下書き】
{previous}

【編集長からの修正指示（必ずすべて反映する）】
{feedback}

修正指示を反映した完成版を、同じ JSON 形式で出力してください。"""


# ---------------------------------------------------------------------------
# 入力データ
# ---------------------------------------------------------------------------

def load_week_entries(today) -> list[dict]:
    try:
        log = json.loads(DEALS_LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    start = (today - timedelta(days=6)).isoformat()
    entries = [e for e in log if isinstance(e, dict) and start <= e.get("date", "") <= today.isoformat()]
    return entries[:MAX_INPUT_ITEMS]


def format_items(entries: list[dict]) -> str:
    lines = []
    for i, e in enumerate(entries, 1):
        lines.append(
            f"[{i}] タイトル: {e.get('title', '')}\n"
            f"    出典: {e.get('source', '')} / 日付: {e.get('date', '')}\n"
            f"    要約: {e.get('summary', '')}\n"
            f"    URL: {e.get('link', '')}"
        )
    return "\n".join(lines)


def load_existing_articles() -> list[dict]:
    """既存の記事（下書きも含む）の名前・タイトル・タグ。同じテーマの下書きを作らないために AI に渡す。"""
    articles = []
    for path in sorted(ARTICLES_DIR.glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        title = re.search(r"(?m)^title:\s*(.+)$", text)
        tags = re.search(r"(?m)^tags:\s*(.+)$", text)
        articles.append({
            "slug": path.stem,
            "title": title.group(1).strip() if title else path.stem,
            "tags": tags.group(1).strip() if tags else "",
        })
    return articles


def format_existing(articles: list[dict]) -> str:
    return "\n".join(f"- {a['title']}（パス: /articles/{a['slug']}/、タグ: {a['tags']}）" for a in articles) or "（なし）"


def load_history() -> list[dict]:
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_history(history: list[dict], entry: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps((history + [entry])[-50:], ensure_ascii=False, indent=2), encoding="utf-8")


def choose_format(history: list[dict]) -> dict:
    """直近で使った型を避け、まだ使っていない（または一番昔に使った）型を選ぶ。最初は「選び方」から。"""
    recent = [h.get("format") for h in history[-AVOID_RECENT_FORMATS:]]
    last_used = {h.get("format"): i for i, h in enumerate(history)}
    candidates = [f for f in FORMATS if f["name"] not in recent] or FORMATS
    return min(candidates, key=lambda f: last_used.get(f["name"], -1))


def format_recent(history: list[dict]) -> str:
    lines = [f"- {h.get('title', '')}（型: {h.get('format', '')}、見出し: {' / '.join(h.get('headings', [])[:8])}）"
             for h in history[-RECENT_FOR_PROMPT:]]
    return "\n".join(lines) or "（なし）"


def extract_headings(body: str) -> list[str]:
    return [re.sub(r"^#+\s*", "", l).strip() for l in body.splitlines() if re.match(r"^#{2,3}\s", l)]


def choose_slug(raw, today) -> str:
    """AI が付けた名前を検証し、使えなければ日付入りの名前にする。既存のファイルとは重ならないようにする。"""
    slug = str(raw or "").strip().lower()
    if not SLUG_RE.match(slug) or len(slug) > 50:
        slug = f"guide-draft-{today.isoformat()}"
    if (ARTICLES_DIR / f"{slug}.md").exists():
        slug = f"{slug}-{today.isoformat()}"
    return slug


def already_drafted(today) -> bool:
    """今日の下書きを作成済みか（下書きの公開前メモに入れた目印で判断する）。"""
    marker = f"ai-draft: {today.isoformat()}"
    return any(marker in p.read_text(encoding="utf-8") for p in ARTICLES_DIR.glob("*.md"))


# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------

def _extract_text(data) -> str:
    """generateContent / Interactions API どちらの応答からもテキストを取り出す。"""
    if isinstance(data, list):  # ストリーミング形式で返った場合
        return "".join(_extract_text(d) for d in data)
    if not isinstance(data, dict):
        return ""
    for cand in data.get("candidates", []) or []:
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if text:
            return text
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    for step in reversed(data.get("steps", []) or data.get("outputs", []) or []):
        contents = step.get("content", []) if isinstance(step, dict) else []
        if isinstance(contents, dict):
            contents = [contents]
        text = "".join(c.get("text", "") for c in contents if isinstance(c, dict) and c.get("type", "text") == "text")
        if text:
            return text
    return ""


# 混雑（500/503/504）が続いたときや、1 日の無料枠の上限に達したときに切り替える予備のモデル（空にすると切り替えない）。
# 1 日の上限はモデルごとに別なので、予備なら書ける。切り替えたら、この実行の残りも予備で行う。
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
_model = {"name": GEMINI_MODEL}


def _switch_to_fallback(reason: str) -> bool:
    if not FALLBACK_MODEL or _model["name"] == FALLBACK_MODEL:
        return False
    print(f"[INFO] {_model['name']} は{reason}ので、{FALLBACK_MODEL} に切り替えます")
    _model["name"] = FALLBACK_MODEL
    return True


def call_gemini(system: str, prompt: str, temperature: float | None = None, retries: int = 4) -> str:
    """429（無料枠の上限）や 503 は少し待って再試行する。それ以外のエラーはそのまま上げる。"""
    overloaded = 0
    for attempt in range(1, retries + 1):
        try:
            return _call_gemini_once(system, prompt, temperature)
        except RuntimeError as e:
            msg = str(e)
            if attempt == retries or not re.search(r"HTTP (429|500|503|504)", msg):
                raise
            if "HTTP 429" in msg and re.search(r"PerDay|per day|daily", msg, flags=re.I):
                if _switch_to_fallback("今日の無料枠の上限に達した"):
                    continue
                raise
            if re.search(r"HTTP (500|503|504)", msg):
                overloaded += 1
                if overloaded >= 2 and _switch_to_fallback("混雑している"):
                    continue
            wait = 60 if "HTTP 429" in msg else 20
            print(f"[INFO] Gemini が混雑・上限のため {wait} 秒待って再試行します（{attempt}/{retries}）")
            time.sleep(wait)
    raise RuntimeError("到達しない")


def _call_gemini_once(system: str, prompt: str, temperature: float | None) -> str:
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    gen_config = {"responseMimeType": "application/json", "maxOutputTokens": 8192}
    if temperature is not None:
        gen_config["temperature"] = temperature
    # 1) 従来の generateContent
    resp = requests.post(
        f"{GEMINI_BASE}/models/{_model['name']}:generateContent",
        headers=headers,
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code == 200:
        return _extract_text(resp.json())
    if resp.status_code not in (404, 405):
        raise RuntimeError(f"Gemini API エラー HTTP {resp.status_code}: {resp.text[:300]}")
    # 2) generateContent が使えない場合は Interactions API
    resp = requests.post(
        f"{GEMINI_BASE}/interactions",
        headers={**headers, "Api-Revision": "2026-05-20"},
        json={
            "model": _model["name"],
            "system_instruction": system,
            "input": prompt,
            "generation_config": {"max_output_tokens": 8192},
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API エラー HTTP {resp.status_code}: {resp.text[:300]}")
    return _extract_text(resp.json())


def parse_response(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("JSON が見つかりません")
    data = json.loads(text[start : end + 1])
    if not data.get("title") or not data.get("body"):
        raise ValueError("title または body がありません")
    return data


# ---------------------------------------------------------------------------
# 安全対策
# ---------------------------------------------------------------------------

def sanitize_body(body: str, allowed_urls: set[str]) -> tuple[str, list[str]]:
    """渡していない URL へのリンクを外し、HTML タグと # 見出しを無害化する。"""
    removed: list[str] = []
    # 全角の角かっこで書かれたリンク ［文字］(URL) を、Markdown のリンク [文字](URL) に直す
    body = re.sub(r"［([^］\n]+)］\s*[（(]([^)）\s]+)[)）]", r"[\1](\2)", body)

    def fix_link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        if url in allowed_urls:
            return m.group(0)
        removed.append(url)
        return label

    body = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", fix_link, body)
    # リンク記法の外に裸で書かれた URL も、許可リストにないものは削除
    def fix_bare(m: re.Match) -> str:
        url = m.group(0)
        if url in allowed_urls:
            return url
        removed.append(url)
        return ""

    body = re.sub(r"(?<!\()https?://[^\s)<>\]]+", fix_bare, body)
    body = re.sub(r"<[^>]+>", "", body)  # HTML タグは使わせない
    body = re.sub(r"(?m)^#(?!#)\s*", "## ", body)  # # 見出しは ## に下げる
    return body.strip(), removed


def mechanical_checks(data: dict) -> list[str]:
    """AI に任せず、コードで確実に見られる基準。"""
    title, body = str(data.get("title", "")), str(data.get("body", ""))
    problems = []
    if len(title) > MAX_TITLE_LENGTH:
        problems.append(f"タイトルが {len(title)} 字と長いので、{MAX_TITLE_LENGTH} 字以内に短くする")
    if not re.search(r"(?m)^##\s*結論", body):
        problems.append("導入の直後に「## 結論：〜」の早見表を入れる")
    if body.count(PRODUCT_MEMO) < 2:
        problems.append(f"商品を入れる欄は、AI が商品名や価格を決めずに「{PRODUCT_MEMO}」と書く（早見表と後半の両方）")
    if EXPERIENCE_MEMO not in body:
        problems.append(f"体験を書く場所に「{EXPERIENCE_MEMO}」を入れる")
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.lstrip().startswith(("#", "|", "-", "*", "【"))]
    if lines:
        first = re.split(r"(?<=[。！？])", lines[0])[0]
        if re.match(r"^(今回は|この記事では|本記事では|こんにちは)", first) or len(first) > 45:
            problems.append(f"導入の最初の1文を、40字以内で読者の手が止まる一文にする（今: 「{first[:40]}」）")
        elif re.search(r"(ありませんか|ないでしょうか|でしょうか|ですよね|ませんか)[。？?]?$", first)                 or re.search(r"(人|方)は(多い|少なくない)(です|でしょう|のでは)", first):
            problems.append(f"導入の最初の1文が、ありきたりの問いかけになっている。言い切りや具体的な場面にする（今: 「{first[:40]}」）")
        if re.search(r"(いかがでしたか|参考になれば幸いです)", lines[-1]):
            problems.append("最後の1文を決まり文句にしない")
    noisy = [h for h in re.findall(r"(?m)^#{2,3}\s*(.+)$", body) if "【" in h or len(h) > 40]
    if noisy:
        problems.append("見出しを短くし、【要確認】や注記は本文に移す: " + " / ".join(f"「{h[:30]}…」" for h in noisy[:3]))
    phrase = ai_phrase_problem(body)
    if phrase:
        problems.append(phrase)
    return problems


def review(data: dict, fmt: dict, history: list[dict]) -> dict:
    """確認役に採点させ、コードでのチェックと合わせて判定する。確認役が失敗したら「未確認」。"""
    try:
        text = call_gemini(REVIEW_SYSTEM, REVIEW_PROMPT.format(
            recent=format_recent(history), format_name=fmt["name"], title=data.get("title", ""), body=data.get("body", "")),
            temperature=0.2)
        result = parse_json_object(text)
    except Exception as e:
        print(f"[INFO] 確認役が採点できませんでした: {type(e).__name__}: {str(e)[:150]}")
        result = {"scores": {}, "must_fix": [], "pass": False, "unreviewed": True}
    must_fix = mechanical_checks(data) + [str(x) for x in (result.get("must_fix") or [])]
    wasteful = [w for w in (result.get("wasteful_sentences") or []) if isinstance(w, str) and w.strip()]
    if wasteful:
        must_fix.append("次の文は削る: " + " / ".join(f"「{w[:50]}」" for w in wasteful[:6]))
    result["must_fix"] = must_fix
    result["pass"] = bool(result.get("pass")) and not must_fix
    return result


def score_of(result: dict) -> tuple:
    scores = [v for v in (result.get("scores") or {}).values() if isinstance(v, (int, float))]
    return (bool(result.get("pass")), not result.get("unreviewed"), -len(result.get("must_fix", [])), sum(scores))


def parse_json_object(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("JSON が見つかりません")
    return json.loads(text[start : end + 1])


def _yaml_value(text: str) -> str:
    return re.sub(r"\s*:\s*", "：", re.sub(r"\s+", " ", str(text))).strip()


def _review_note(result: dict, fmt_name: str) -> str:
    if result.get("unreviewed"):
        verdict = "未確認（確認役が採点できませんでした。いつもより丁寧に読んでください）"
    else:
        verdict = "合格" if result.get("pass") else "不合格のまま（下の点を直してください）"
    lines = [f"  確認役の判定（型: {fmt_name}）: {verdict} / 評価: {result.get('scores') or 'なし'}\n"]
    lines += [f"    - {_yaml_value(x)[:150]}\n" for x in result.get("must_fix", [])[:8]]
    return "".join(lines)


def build_draft_file(data: dict, today, removed: list[str], review_result: dict | None = None, fmt_name: str = "") -> str:
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [_yaml_value(t) for t in tags if str(t).strip()][:5] or ["セール"]
    note = (
        "<!--\n"
        f"ai-draft: {today.isoformat()}\n"
        "この下書きは AI（Gemini）が自動で作成しました。公開前にやること（終わったらこのメモごと削除してください）:\n"
        "  1. 【要確認：…】の部分を調べて、正しければ【要確認：…】だけを消す（間違いや自信のない文は直すか削除）\n"
        "  2. セール情報の部分が元記事と合っているか、各リンク先で確認する\n"
        "  3. 早見表と「予算別のおすすめ」の【編集者メモ：楽天市場で商品を選び…】に、楽天市場で販売中を確かめた商品の\n"
        "     名前・リンク（item.rakuten.co.jp の商品ページ）・価格（〇月〇日時点）を入れる（Claude に頼んでもよい）\n"
        "  4. ほかの【編集者メモ：…】を、自分の言葉で書き換える（使ったことのない商品の感想は書かない）\n"
        "  5. 上の「draft: true」の行を削除する ← これを消さないと公開されません\n"
        "  6. date を公開する日に変える\n"
        + (f"  ※ データにない URL へのリンクを {len(removed)} 件、自動で外しました\n" if removed else "")
        + (_review_note(review_result, fmt_name) if review_result is not None else "")
        + "-->\n\n"
    )
    return (
        "---\n"
        f"title: {_yaml_value(data['title'])}\n"
        f"date: {today.isoformat()}\n"
        f"description: {_yaml_value(data.get('description', ''))}\n"
        f"tags: {', '.join(tags)}\n"
        "draft: true\n"
        "---\n\n" + note + data["body"].strip() + "\n"
    )


# ---------------------------------------------------------------------------
# GitHub への通知
# ---------------------------------------------------------------------------

def annotate(level: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title=AI下書き::{message}")
    else:
        print(f"[{level.upper()}] {message}")


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def notify(draft_path: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    if not (token and repo):
        annotate("notice", "GitHub 外での実行のため Issue は作成しません")
        return
    rel = Path(draft_path).as_posix()
    title_match = re.search(r"(?m)^title:\s*(.+)$", Path(draft_path).read_text(encoding="utf-8"))
    title = title_match.group(1).strip() if title_match else rel
    body = (
        f"AI が記事の下書きを作成しました。\n\n"
        f"**{title}**\n\n"
        f"- ファイル: [{rel}]({server}/{repo}/blob/main/{rel})\n"
        f"- 編集する: [GitHub で編集]({server}/{repo}/edit/main/{rel})\n\n"
        "### 公開までのチェックリスト\n"
        "- [ ] 【要確認：…】の部分を調べて、正しいことを確かめた（間違いは直した）\n"
        "- [ ] セール情報の部分が元記事と合っているか、各リンク先で確認した\n"
        "- [ ] 早見表と「予算別のおすすめ」に、楽天市場で販売中を確かめた商品（名前・リンク・価格と確認日）を入れた\n"
        "- [ ] ほかの【編集者メモ：…】を自分の言葉で書き換えた（体験は使ったことのある製品だけ。なければ削除）\n"
        "- [ ] `draft: true` の行と、一番上の公開前メモ（`<!-- … -->`）を削除し、`date` を公開日にした\n\n"
        "公開しない場合は、このファイルを削除してください。確認が終わったらこの Issue を閉じてください。"
    )
    try:  # スマホから仕上げるときの書き方と、今の欄の一覧
        import draft_editor

        body += (
            "\n\n### 📱 スマホで仕上げるとき\n"
            "この Issue にコメントで指示を書くと、自動で下書きに反映します（返信で結果を知らせます）。"
            "商品は、下の候補から `1 A` のように選ぶか、欄の番号と楽天のリンクを 1 行ずつ書きます。"
            "楽天市場アプリの「共有」のリンクも使えます。\n\n"
            + draft_editor.status_text(Path(draft_path).read_text(encoding="utf-8"),
                                       draft_editor.load_candidates(Path(draft_path).stem))
            + "\n\n" + draft_editor.HELP
        )
    except Exception as e:  # 一覧が作れなくても Issue は作る
        print(f"[INFO] スマホ向けの案内を作れませんでした: {e}")
    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": f"📝 AI下書きの確認: {title}", "body": body},
        timeout=30,
    )
    if resp.status_code == 201:
        annotate("notice", f"Issue を作成しました: {resp.json().get('html_url', '')}")
    else:
        annotate("warning", f"Issue の作成に失敗しました HTTP {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def create_draft() -> None:
    today = datetime.now(JST).date()
    if today.weekday() != DRAFT_WEEKDAY and os.environ.get("FORCE_AI_DRAFT") != "1":
        return
    if not GEMINI_API_KEY:
        annotate("notice", "GEMINI_API_KEY が未設定のため下書きを作成しません")
        return
    if already_drafted(today):
        print("[INFO] 今日の下書きは作成済みのためスキップ")
        return
    entries = load_week_entries(today)
    if len(entries) < 3:
        annotate("notice", f"今週のセール記事が {len(entries)} 件と少ないため下書きを作成しません")
        return

    existing = load_existing_articles()
    history = load_history()
    fmt = choose_format(history)
    print(f"[INFO] 今回の型: {fmt['name']}")
    base_prompt = USER_PROMPT_TEMPLATE.format(
        start=(today - timedelta(days=6)).isoformat(),
        end=today.isoformat(),
        items=format_items(entries),
        existing=format_existing(existing),
        recent=format_recent(history),
        format_name=fmt["name"],
        format_topic=fmt["topic"],
        format_structure=fmt["structure"],
        format_title=fmt["title"],
    )
    allowed = {e.get("link", "") for e in entries if e.get("link")}
    allowed |= {f"/articles/{a['slug']}/" for a in existing}

    # 執筆 → 確認 →（だめなら 1 回だけ書き直し → 確認）→ 良いほうを下書きにする
    data = parse_response(call_gemini(SYSTEM_PROMPT, base_prompt, temperature=0.9))
    first_searches = data.get("product_searches")  # 書き直しの版に検索条件がなかったときに使う
    first_x_posts = data.get("x_posts")
    data["body"], removed = sanitize_body(str(data["body"]), allowed)
    result = review(data, fmt, history)
    print(f"[INFO] 確認役の評価: {result.get('scores')} / 直す点 {len(result['must_fix'])} 件")
    if not result["pass"] and not result.get("unreviewed"):
        try:
            revised = parse_response(call_gemini(SYSTEM_PROMPT, REVISION_PROMPT.format(
                base=base_prompt, previous=json.dumps(data, ensure_ascii=False),
                feedback="\n".join(f"- {f}" for f in result["must_fix"])), temperature=0.7))
            revised["body"], removed2 = sanitize_body(str(revised["body"]), allowed)
            result2 = review(revised, fmt, history)
            print(f"[INFO] 書き直し後の評価: {result2.get('scores')} / 直す点 {len(result2['must_fix'])} 件")
            if score_of(result2) > score_of(result):
                data, removed, result = revised, removed2, result2
        except Exception as e:  # 書き直しに失敗しても、最初の下書きは残す
            print(f"[INFO] 書き直しに失敗したので、最初の下書きを使います: {type(e).__name__}")

    path = ARTICLES_DIR / f"{choose_slug(data.get('slug'), today)}.md"
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(build_draft_file(data, today, removed, result, fmt["name"]), encoding="utf-8")
    make_candidates(path, data.get("product_searches") or first_searches)
    save_x_posts(path.stem, data.get("x_posts") or first_x_posts)
    save_history(history, {"date": today.isoformat(), "format": fmt["name"], "title": data["title"],
                           "headings": extract_headings(data["body"]), "pass": result.get("pass"),
                           "scores": result.get("scores")})
    rel = path.relative_to(BASE_DIR).as_posix()
    annotate("notice", f"下書きを作成しました: {rel}（型: {fmt['name']}、モデル: {_model['name']}、外したリンク: {len(removed)} 件）")
    set_output("draft", rel)


X_POSTS_DIR = BASE_DIR / "data" / "x_posts"  # 記事ごとの X の告知文の案（公開したときの返信に URL を付けて載せる）
X_POST_MAX = 100


def save_x_posts(slug: str, posts) -> None:
    if not isinstance(posts, list):
        return
    cleaned = []
    for post in posts:
        post = re.sub(r"https?://\S+", "", str(post)).strip()
        if post and len(post) <= X_POST_MAX + 20:
            cleaned.append(post)
    if cleaned:
        X_POSTS_DIR.mkdir(parents=True, exist_ok=True)
        (X_POSTS_DIR / f"{slug}.json").write_text(json.dumps(cleaned[:3], ensure_ascii=False, indent=2), encoding="utf-8")


def make_candidates(path: Path, searches) -> None:
    """早見表の行ごとに、楽天市場で商品の候補を探して data/candidates/ に保存する（失敗しても下書きは残す）。"""
    if not isinstance(searches, list) or not searches:
        return
    try:
        import draft_editor
        from rakuten_links import SearchError, search_items

        text = path.read_text(encoding="utf-8")
        slots = draft_editor.find_slots(draft_editor.split_front_matter(text)[1].split("\n"))
        found: dict[str, list] = {}
        for slot, cond in zip(slots, searches):
            if not isinstance(cond, dict) or not str(cond.get("keyword", "")).strip():
                continue
            keyword = str(cond["keyword"]).strip()
            lo, hi = cond.get("min_price"), cond.get("max_price")
            lo = int(lo) if isinstance(lo, (int, float)) and lo > 0 else None
            hi = int(hi) if isinstance(hi, (int, float)) and hi > 0 else None
            try:
                items = search_items(keyword, lo, hi) or (search_items(keyword) if (lo or hi) else [])
            except SearchError as e:
                print(f"[INFO] 楽天の候補を探せませんでした（{slot.no} 番）: {e}")
                return
            if items:
                found[str(slot.no)] = items
            time.sleep(1.2)  # 楽天への連続アクセスを避ける
        if found:
            draft_editor.save_candidates(path.stem, found)
            print(f"[INFO] 楽天の候補を {len(found)} 欄ぶん保存しました")
    except Exception as e:  # 候補は「あると便利」なものなので、失敗しても止めない
        print(f"[INFO] 楽天の候補づくりに失敗しました: {type(e).__name__}: {str(e)[:150]}")


def main() -> int:
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--notify":
            notify(sys.argv[2])
        else:
            create_draft()
    except Exception as exc:  # 失敗してもサイト更新は止めない
        annotate("warning", f"失敗しました: {type(exc).__name__}: {str(exc)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
