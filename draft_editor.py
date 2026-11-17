"""AI 下書きを、GitHub Issue のコメントで仕上げて公開する（スマホから使うため）。

「📝 AI下書きの確認」の Issue に、リポジトリの持ち主がコメントすると .github/workflows/draft-comment.yml から呼ばれる。
コメントには、1 行に 1 つずつ次の指示を書ける:

    1 https://item.rakuten.co.jp/ショップ/商品/      … 1 番の欄に商品を入れる（楽天アプリの共有リンク a.r10.to も可）
    1 https://a.r10.to/xxxx 短い商品名               … 商品名を自分で決めるとき
    価格 1 12800                                     … 1 番の欄の価格を直す（ページから取れなかったときなど）
    体験 27インチを毎日使っていて、文字が見やすい      … 体験の欄（【編集者メモ：自分が使っている…】）に入れる
    メモ 価格より画面の見やすさを優先しました          … 選んだ理由の欄（【編集者メモ：選んだ理由…】）に入れる
    直す 2年使っています → 3年使っています            … 本文の文字を置き換える（ちょうど 1 か所あるときだけ）
    削除 一般的なデスクで使う場合、                    … 本文から文字を消す（ちょうど 1 か所あるときだけ）
    タイトル 在宅ワークが変わるPCモニターの選び方       … タイトルを変える
    要確認OK                                          … 【要確認：…】の印をすべて外す（内容を確かめてから）
    公開                                              … 下書きのメモを消して公開する
    一覧                                              … 今の状態だけを返信する

欄の番号は、入れたあとも変わらない（入れた欄には <!--slot:番号--> という見えない印を残し、公開のときに消す）。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_draft import EDITOR_MEMO, EXPERIENCE_MEMO, MAX_TITLE_LENGTH, PRODUCT_MEMO, _yaml_value
from rakuten_links import fetch_item, resolve

BASE_DIR = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
SITE_URL = "https://auto-deal-tracker.pages.dev"
SLOT_MARK_RE = re.compile(r"\s*<!--slot:(\d+)-->")
CHECK_RE = re.compile(r"【要確認[^】]*】")
PRICE_TEXT_RE = re.compile(r"(?:約)?[\d,]+円（\d+月\d+日時点）|価格はリンク先で確認してください")
PRICE_HEADER_WORDS = ("価格", "合計", "目安", "予算")


# ---------------------------------------------------------------------------
# 欄（スロット）の見つけ方
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    no: int
    label: str
    table_lines: list[int] = field(default_factory=list)
    section_lines: list[int] = field(default_factory=list)

    def filled(self, lines: list[str]) -> bool:
        return all(PRODUCT_MEMO not in lines[i] for i in self.table_lines + self.section_lines)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _is_slot_line(line: str) -> bool:
    return PRODUCT_MEMO in line or bool(SLOT_MARK_RE.search(line))


def _table_header(lines: list[str], i: int) -> list[str]:
    """表の行 i に対する見出し行（表の 1 行目）のセル。"""
    j = i
    while j > 0 and lines[j - 1].lstrip().startswith("|"):
        j -= 1
    return _cells(lines[j])


def _heading_before(lines: list[str], i: int) -> str:
    for j in range(i, -1, -1):
        m = re.match(r"^#{2,4}\s*(.+)$", lines[j])
        if m:
            return SLOT_MARK_RE.sub("", m.group(1)).strip()
    return ""


def find_slots(lines: list[str]) -> list[Slot]:
    """商品を入れる欄の一覧。早見表の行と後半の欄が同じ数なら、同じ番号でまとめる（1 回で両方入る）。"""
    table_rows = [i for i, l in enumerate(lines) if l.lstrip().startswith("|") and _is_slot_line(l)]
    sections = [i for i, l in enumerate(lines) if not l.lstrip().startswith("|") and _is_slot_line(l)]
    slots: list[Slot] = []
    if table_rows and len(table_rows) == len(sections):
        for n, (t, s) in enumerate(zip(table_rows, sections), 1):
            slots.append(Slot(n, _cells(lines[t])[0], [t], [s]))
        return slots
    for t in table_rows:
        slots.append(Slot(len(slots) + 1, _cells(lines[t])[0], [t], []))
    for s in sections:
        slots.append(Slot(len(slots) + 1, _heading_before(lines, s), [], [s]))
    return slots


# ---------------------------------------------------------------------------
# 欄に商品を入れる
# ---------------------------------------------------------------------------

def price_text(price: int | None, today) -> str:
    return f"約{price:,}円（{today.month}月{today.day}日時点）" if price else "価格はリンク先で確認してください"


def fill_slot(lines: list[str], slot: Slot, item: dict, today) -> None:
    name = (item.get("name") or "楽天市場で見る").replace("|", "／").replace("[", "［").replace("]", "］")
    link = f"[{name}]({item['url']})"
    price = price_text(item.get("price"), today)
    mark = f" <!--slot:{slot.no}-->"
    for i in slot.table_lines:
        header = _table_header(lines, i)
        cells = _cells(lines[i])
        for j, cell in enumerate(cells):
            if PRODUCT_MEMO in cell:
                is_price = j < len(header) and any(w in header[j] for w in PRICE_HEADER_WORDS)
                cells[j] = price if is_price else link
        cells[-1] += mark
        lines[i] = "| " + " | ".join(cells) + " |"
    for i in slot.section_lines:
        lines[i] = lines[i].replace(PRODUCT_MEMO, f"おすすめ：{link}　{price}") + mark


def set_price(lines: list[str], slot: Slot, price: int, today) -> bool:
    changed = False
    for i in slot.table_lines + slot.section_lines:
        new = PRICE_TEXT_RE.sub(price_text(price, today), lines[i])
        changed |= new != lines[i]
        lines[i] = new
    return changed


# ---------------------------------------------------------------------------
# 指示の読み取りと実行
# ---------------------------------------------------------------------------

SP = r"[\s　]"  # 半角・全角のスペース
ARROW = r"(?:→|->|⇒|＞)"


def parse_commands(comment: str) -> list[tuple]:
    cmds: list[tuple] = []
    for raw in comment.splitlines():
        original = raw.strip()
        line = original.replace("　", " ")  # 指示の見分けにだけ使う。直す文字は original から取る
        if not line:
            continue
        if m := re.match(rf"^直す{SP}*[:：]?{SP}*(.+?){SP}*{ARROW}{SP}*(.*)$", original):
            cmds.append(("replace", m.group(1), m.group(2)))
        elif m := re.match(rf"^削除{SP}*[:：]?{SP}*(.+)$", original):
            cmds.append(("replace", m.group(1), ""))
        elif m := re.match(rf"^タイトル{SP}*[:：]?{SP}*(.+)$", original):
            cmds.append(("title", m.group(1).strip()))
        elif m := re.match(r"^(\d+)\s*[:：.、)]?\s*(https?://\S+)\s*(.*)$", line):
            cmds.append(("product", int(m.group(1)), m.group(2), m.group(3).strip()))
        elif m := re.match(r"^価格\s*(\d+)\s*[:：]?\s*([\d,]+)\s*円?$", line):
            cmds.append(("price", int(m.group(1)), int(m.group(2).replace(",", ""))))
        elif m := re.match(rf"^体験{SP}*[:：]?{SP}*(.+)$", original):
            cmds.append(("experience", m.group(1).strip()))
        elif m := re.match(rf"^メモ{SP}*[:：]?{SP}*(.+)$", original):
            cmds.append(("memo", m.group(1).strip()))
        elif re.match(r"^要確認\s*(OK|ok|ＯＫ|オーケー|済み?)$", line):
            cmds.append(("checks_ok",))
        elif line in ("公開", "公開する"):
            cmds.append(("publish",))
        elif line in ("一覧", "状態", "確認"):
            cmds.append(("status",))
    return cmds


def split_front_matter(text: str) -> tuple[str, str]:
    m = re.match(r"^(---\n.*?\n---\n)(.*)$", text, flags=re.S)
    return (m.group(1), m.group(2)) if m else ("", text)


def publish_text(text: str, today) -> tuple[str | None, list[str]]:
    """公開できる形にする。できないときは (None, 理由)。"""
    front, body = split_front_matter(text)
    problems = []
    if PRODUCT_MEMO in body:
        problems.append("商品の欄がまだ残っています（下の「商品の欄」を見てください）")
    if CHECK_RE.search(body):
        problems.append("【要確認：…】が残っています。内容を確かめてから「要確認OK」と書いてください")
    if problems:
        return None, problems
    body = re.sub(r"<!--(?!slot:).*?-->\n*", "", body, flags=re.S)  # 公開前メモ
    body = SLOT_MARK_RE.sub("", body)
    body = body.replace(EXPERIENCE_MEMO, "").replace(EDITOR_MEMO, "")
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(l.rstrip() for l in body.splitlines())).strip() + "\n"
    front = re.sub(r"(?m)^draft:.*\n", "", front)
    front = re.sub(r"(?m)^date:.*$", f"date: {today.isoformat()}", front)
    return front + "\n" + body, []


def _without_notes(body: str) -> str:
    """公開前メモ（<!-- … -->）を除いた本文。"""
    return re.sub(r"<!--(?!slot:).*?-->", "", body, flags=re.S)


def status_text(text: str) -> str:
    _, body = split_front_matter(text)
    lines = body.splitlines()
    visible = _without_notes(body).splitlines()
    title = re.search(r"(?m)^title:\s*(.*)$", text)
    out = [f"**タイトル**: {title.group(1).strip() if title else '（なし）'}", "", "**商品の欄**"]
    slots = find_slots(lines)
    for s in slots:
        out.append(f"- {'✅' if s.filled(lines) else '⬜'} **{s.no}**: {s.label}")
    if not slots:
        out.append("- （なし）")
    exp, memo = body.count(EXPERIENCE_MEMO), body.count(EDITOR_MEMO)
    out.append(f"\n**体験の欄**: 残り {exp} か所 / **選んだ理由の欄**: 残り {memo} か所（空のまま公開すると、その欄は消えます）")
    checks = []
    for l in visible:
        if CHECK_RE.search(l):
            checks.append(re.sub(r"\s+", " ", l.strip().lstrip("#|-* "))[:120])
    if checks:
        out.append(f"\n**【要確認】が {len(checks)} か所あります**（確かめたら「要確認OK」）")
        out += [f"- {c}" for c in checks[:10]]
    else:
        out.append("\n**【要確認】**: なし")
    return "\n".join(out)


def replace_once(text: str, old: str, new: str) -> tuple[str, int]:
    """公開前メモと見えない印（<!-- … -->）の外で old を探し、ちょうど 1 か所なら new に置き換える。

    戻り値は (新しい文章, 見つかった数)。見つかった数が 1 でなければ文章は変えない。
    """
    masked = re.sub(r"<!--.*?-->", lambda m: "\0" * len(m.group(0)), text, flags=re.S)
    positions = [m.start() for m in re.finditer(re.escape(old), masked)]
    if len(positions) != 1:
        return text, len(positions)
    i = positions[0]
    return text[:i] + new + text[i + len(old):], 1


HELP = """<details><summary>書き方</summary>

```
1 https://a.r10.to/xxxx
2 https://item.rakuten.co.jp/ショップ/商品/ 短い商品名
価格 2 12800
体験 実際に使っていて良かったこと
メモ 選んだ理由
直す 古い文字 → 新しい文字
削除 消したい文字
タイトル 新しいタイトル
要確認OK
公開
```
楽天市場アプリの「共有」で取ったリンク（a.r10.to）もそのまま使えます。1 行に 1 つずつ、何行でもまとめて書けます。
「直す」「削除」は、下書きの文字をそのままコピーして書いてください（同じ文字が 2 か所以上あるときは、前後も含めて長めに）。
</details>"""


def run(comment: str, path: Path, today) -> tuple[str, bool, bool]:
    """コメントを実行して (返信, ファイルを変えたか, 公開したか)。指示がなければ返信は空。"""
    cmds = parse_commands(comment)
    if not cmds:
        return "", False, False
    text = path.read_text(encoding="utf-8")
    front, body = split_front_matter(text)
    lines = body.split("\n")
    log: list[str] = []
    changed = published = False
    want_publish = any(c[0] == "publish" for c in cmds)

    for cmd in cmds:
        kind = cmd[0]
        slots = {s.no: s for s in find_slots(lines)}
        if kind == "product":
            _, no, url, name = cmd
            slot = slots.get(no)
            if slot is None:
                log.append(f"⚠️ {no} 番の欄はありません")
                continue
            if slot.filled(lines):
                log.append(f"⚠️ {no} 番（{slot.label}）はもう入っています。変えるときは PC で編集してください")
                continue
            real = resolve(url)
            if not real:
                log.append(f"⚠️ {no} 番: 楽天市場のリンクではないか、開けませんでした: {url}")
                continue
            item = fetch_item(real)
            if name:
                item["name"] = name
            fill_slot(lines, slot, item, today)
            changed = True
            got = f"「{item['name'] or '（商品名を取れませんでした）'}」 {price_text(item.get('price'), today)}"
            log.append(f"✅ {no} 番（{slot.label}）に入れました: {got}")
        elif kind == "price":
            _, no, price = cmd
            slot = slots.get(no)
            if slot and slot.filled(lines) and set_price(lines, slot, price, today):
                changed = True
                log.append(f"✅ {no} 番の価格を {price:,} 円にしました")
            else:
                log.append(f"⚠️ {no} 番の価格を直せませんでした（先に商品を入れてください）")
        elif kind in ("experience", "memo"):
            memo = EXPERIENCE_MEMO if kind == "experience" else EDITOR_MEMO
            joined = "\n".join(lines)
            if memo in joined:
                lines = joined.replace(memo, cmd[1], 1).split("\n")
                changed = True
                log.append(f"✅ {'体験' if kind == 'experience' else '選んだ理由'}の欄に入れました")
            else:
                log.append(f"⚠️ {'体験' if kind == 'experience' else '選んだ理由'}の欄はもう残っていません")
        elif kind == "replace":
            _, old, new = cmd
            label = "削除" if new == "" else "直す"
            joined, found = replace_once("\n".join(lines), old, new)
            short = old if len(old) <= 30 else old[:30] + "…"
            if found == 1:
                lines = joined.split("\n")
                changed = True
                log.append(f"✅ {label}: 「{short}」" + (f" → 「{new[:30]}」" if new else " を消しました"))
            elif found == 0:
                log.append(f"⚠️ {label}: 「{short}」が見つかりませんでした。下書きの文字をそのままコピーして書いてください")
            else:
                log.append(f"⚠️ {label}: 「{short}」が {found} か所あるので、直しませんでした。前後の文字も含めて長めに書いてください")
        elif kind == "title":
            new_title = _yaml_value(cmd[1])
            front = re.sub(r"(?m)^title:.*$", lambda _: f"title: {new_title}", front, count=1)
            changed = True
            note = f"（{len(new_title)} 字。{MAX_TITLE_LENGTH} 字以内がおすすめです）" if len(new_title) > MAX_TITLE_LENGTH else ""
            log.append(f"✅ タイトルを「{new_title}」にしました{note}")
        elif kind == "checks_ok":
            joined = "\n".join(lines)
            count = len(CHECK_RE.findall(_without_notes(joined)))
            if count:
                lines = re.sub(r"[ 　]*【要確認[^】]*】", "", joined).split("\n")
                changed = True
            log.append(f"✅ 【要確認】の印を {count} か所外しました")

    text = front + "\n".join(lines)
    if want_publish:
        new, problems = publish_text(text, today)
        if new is None:
            log += [f"⛔ まだ公開できません: {p}" for p in problems]
        else:
            text, changed, published = new, True, True
            slug = path.stem
            log.append(f"🎉 公開しました。2〜3分でサイトに反映されます: {SITE_URL}/articles/{slug}/")
    if changed:
        path.write_text(text, encoding="utf-8")

    reply = "\n".join(log)
    if not published:
        reply += "\n\n---\n" + status_text(text) + "\n\n" + HELP
    return reply, changed, published


def main() -> int:
    comment = os.environ.get("COMMENT_BODY", "")
    issue_body = os.environ.get("ISSUE_BODY", "")
    m = re.search(r"articles/[a-z0-9-]+\.md", issue_body)
    out_reply = Path(os.environ.get("REPLY_FILE", "reply.md"))
    if not m:
        out_reply.write_text("⚠️ この Issue から下書きのファイルが分かりませんでした。", encoding="utf-8")
        return 0
    path = BASE_DIR / m.group(0)
    if not path.exists():
        out_reply.write_text(f"⚠️ `{m.group(0)}` が見つかりません（名前を変えたか、削除した可能性があります）。", encoding="utf-8")
        return 0
    reply, changed, published = run(comment, path, datetime.now(JST).date())
    out_reply.write_text(reply, encoding="utf-8")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\npublished={'true' if published else 'false'}\n")
            f.write(f"has_reply={'true' if reply else 'false'}\nfile={m.group(0)}\n")
    print(reply or "（指示なし）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
