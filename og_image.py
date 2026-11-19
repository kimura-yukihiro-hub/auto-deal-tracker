"""記事ごとの SNS 用画像（OGP、1200×630）を作る。X などで記事を共有したときに、記事の題が入った画像が出る。

同じファイルを auto-deal-tracker（og_image.py）と subsc-checker（tools/og_image.py）に置いている（直すときは両方）。
Pillow と日本語フォントが要る。Windows では游ゴシック、GitHub Actions では fonts-noto-cjk を入れて使う。
作れないとき（フォントがないなど）は make_card が False を返す。呼ぶ側は、サイト共通の画像のままにする。
"""

from __future__ import annotations

import re
from pathlib import Path

W, H = 1200, 630
FONT_DIR = Path("C:/Windows/Fonts")
NOTO_DIR = Path("/usr/share/fonts/opentype/noto")
FONT_BOLD = [FONT_DIR / "YuGothB.ttc", FONT_DIR / "meiryob.ttc", NOTO_DIR / "NotoSansCJK-Bold.ttc"]
FONT_REG = [FONT_DIR / "YuGothM.ttc", FONT_DIR / "meiryo.ttc", NOTO_DIR / "NotoSansCJK-Regular.ttc", NOTO_DIR / "NotoSansCJK-Medium.ttc"]


def fonts_available() -> bool:
    return any(p.exists() for p in FONT_BOLD) and any(p.exists() for p in FONT_REG)


def _font(cands: list[Path], size: int):
    from PIL import ImageFont
    for p in cands:
        if p.exists():
            return ImageFont.truetype(str(p), size)
    raise FileNotFoundError("日本語フォントがない")


def _wrap(text: str, font, max_w: int) -> list[str]:
    lines, line = [], ""
    for ch in text:
        if font.getlength(line + ch) > max_w and line:
            # 句読点や閉じ括弧は行頭に来ないように、前の行に付ける
            if ch in "、。，．）」』】！？…ー":
                line += ch
                continue
            lines.append(line)
            line = ch
        else:
            line += ch
    if line:
        lines.append(line)
    return lines


def make_card(title: str, site: str, tagline: str, out: Path, color: str = "#2563eb") -> bool:
    """白地に、左の色帯・サイト名・記事の題（最大 3 行）・説明の一行を置いた画像を out に保存する。"""
    try:
        from PIL import Image, ImageDraw
        c = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        img = Image.new("RGB", (W, H), "#ffffff")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 24, H], fill=c)                          # 左の色帯
        d.rectangle([24, H - 96, W, H], fill=(243, 246, 252))      # 下の帯
        site_font = _font(FONT_BOLD, 40)
        d.text((88, 64), site, font=site_font, fill=c)
        # 題は、3 行に収まる大きさまで小さくする
        # 「｜」の前と後は行を分ける（例: 「ロボットとスティック掃除機どっち？」「掃除の手間で比べた結論」）
        # 日本語の間の空白（「選び方 容量の目安」）も区切りとして扱う
        parts = [x.strip() for x in re.split("[｜|]|(?<=[^ -~])[ 　](?=[^ -~])", title) if x and x.strip()]
        wrap_all = lambda f: [l for part in parts for l in _wrap(part, f, W - 176)]
        for size in (76, 68, 60, 54):
            tf = _font(FONT_BOLD, size)
            lines = wrap_all(tf)
            if len(lines) <= 3:
                break
        if len(lines) > 3:
            lines = lines[:3]
            lines[-1] = lines[-1][:-1] + "…"
        line_h = int(size * 1.4)
        y = 150 + (3 - len(lines)) * line_h // 2
        for l in lines:
            d.text((88, y), l, font=tf, fill=(31, 41, 51))
            y += line_h
        d.text((88, H - 70), tagline, font=_font(FONT_REG, 30), fill=(91, 102, 115))
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, optimize=True)
        return True
    except Exception as e:  # 画像がなくてもサイトは作る
        print(f"[INFO] SNS 用画像を作れませんでした（{type(e).__name__}: {e}）: {out.name}")
        return False
