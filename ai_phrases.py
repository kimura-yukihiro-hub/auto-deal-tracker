"""AI の書いた日本語によく出る言い回し。下書きのコードでの検査（mechanical_checks）で使う。

一覧は coji/natural-japanese（MIT License, Copyright (c) 2026 coji）の scripts/lint.py の FORBIDDEN_PHRASES から、
人間の文章にもよく出る「弱い印」の語（重要なのは・このように・不可欠・ポイントは・さて、）を除いて写したもの。
https://github.com/coji/natural-japanese
同じファイルを note-auto-writer・auto-deal-tracker・subsc-checker に置いている（直すときは 3 つとも）。
"""

from __future__ import annotations

import re

AI_PHRASES: tuple[str, ...] = (
    # 結論の押し付け・まとめ口調
    "と言えるでしょう", "と言えるだろう", "と言えます", "ということになるでしょう", "のではないでしょうか",
    "大切なのは", "結論から言うと", "結論として", "いかがでしたか", "いかがでしょうか", "まとめると", "総じて",
    # 過剰な強調・持ち上げ
    "非常に重要", "極めて重要", "言うまでもなく", "言うまでもありません", "まさしく",
    # 定型導入・空疎な接続
    "それでは、", "このような中", "ここで注目したいのは", "見ていきましょう", "紹介していきます",
    "解説していきます", "深掘りしていきます",
    # 予防線・免責的な言い回し
    "一概には言えません", "個人差がありますが", "あくまで一例ですが",
    # 中身の代わりに姿勢だけを言う
    "正面から扱う", "正面から見る", "正面から書く",
    # 空虚な形容
    "核心的", "鍵となる", "根本的な", "多角的", "包括的", "総合的",
    # 空虚な動詞・予告口調
    "掘り下げる", "深掘りする", "言語化する", "について見ていく", "を探求する",
)

_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def found_ai_phrases(text: str) -> list[str]:
    """本文に出てくる言い回し（出てきた順、重ならない）。コード（``` や ` で囲んだ所）は見ない。"""
    plain = _CODE.sub(" ", text)
    hits = [(plain.find(p), p) for p in AI_PHRASES if p in plain]
    return [p for _, p in sorted(hits)]


def ai_phrase_problem(text: str) -> str | None:
    """直し役に渡す一文。なければ None。"""
    hits = found_ai_phrases(text)
    if not hits:
        return None
    return ("AI の書いた文章によく出る言い回しがある。削っても意味が変われば元の内容で言い換え、変わらなければ削る: "
            + "、".join(f"「{p}」" for p in hits[:8]))
