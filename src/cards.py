"""Product options card renderer — pure Pillow compositing, no AI.

Given 1-4 products (photo + name + price + a caller-assigned ref number), draws
a single JPEG "menu" image: photos side by side, a numbered badge on each photo
corner, and name + price on a white caption strip UNDER each photo (never
overlaid — product photos have unpredictable contrast). A footer strip repeats
the reply instruction so the image is self-explanatory even without a caption.

Used by the kapruka_render_options_card MCP tool (WhatsApp sales agents send
the card so customers reply "2" instead of clicking product links). Cards are
content-addressed on disk: hash(ids+refs+prices+currency) → serve-forever.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ── Layout constants (px) ────────────────────────────────────────────────────
CELL_W = 360
IMG_H = 360
CAPTION_H = 108
FOOTER_H = 46
GAP = 8
MARGIN = 8

BADGE_R = 26  # radius
BADGE_BG = (38, 33, 92)  # deep navy — high contrast on any product photo
BADGE_FG = (255, 255, 255)
BG = (255, 255, 255)
CAPTION_NAME = (68, 68, 65)
CAPTION_PRICE = (28, 28, 26)
FOOTER_FG = (120, 119, 112)
HAIRLINE = (229, 229, 224)

_CARD_DIR = Path(os.getenv("CARD_DIR", "cards"))
# 180 days: the Falcon console hotlinks these URLs in chat history, so pruning
# at 7 days made older conversations show broken images (noticed 2026-08-14).
# ~135KB/card at current volume is a few MB/day — retention is cheap.
_CARD_MAX_AGE_S = 180 * 24 * 3600


def _font_path(bold: bool) -> str | None:
    """Resolve a usable TTF: env override → DejaVu (Linux) → Arial (Windows)."""
    env = os.getenv("CARD_FONT_BOLD" if bold else "CARD_FONT")
    candidates = [env] if env else []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_path(bold)
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _fit_on_white(img: Image.Image, w: int, h: int) -> Image.Image:
    """Contain-fit (never crop the product) centered on a white square."""
    img = img.convert("RGB")
    img.thumbnail((w, h), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), BG)
    canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    return canvas


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _fmt_price(amount: float, currency: str) -> str:
    if currency == "LKR":
        return f"LKR {amount:,.0f}"
    return f"{currency} {amount:,.2f}"


def _reply_hint(refs: list[int]) -> str:
    if len(refs) == 1:
        return f"Reply {refs[0]} to choose"
    return f"Reply {', '.join(str(r) for r in refs[:-1])} or {refs[-1]} to choose"


class CardItem:
    """One product cell. `image` is the already-downloaded photo bytes (or None
    for a photo-less placeholder cell)."""

    def __init__(self, ref: int, product_id: str, name: str, price_amount: float,
                 currency: str, image: bytes | None):
        self.ref = ref
        self.product_id = product_id
        self.name = name
        self.price_amount = price_amount
        self.currency = currency
        self.image = image


def render_card(items: list[CardItem]) -> bytes:
    """Compose the card JPEG. Pure CPU (Pillow); ~50-150ms for 3 cells."""
    n = len(items)
    if not 1 <= n <= 4:
        raise ValueError("1-4 items per card")

    width = MARGIN * 2 + CELL_W * n + GAP * (n - 1)
    height = MARGIN + IMG_H + CAPTION_H + FOOTER_H + MARGIN
    card = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(card)

    f_badge = _font(30, bold=True)
    f_name = _font(21)
    f_price = _font(30, bold=True)
    f_footer = _font(17)

    for i, it in enumerate(items):
        x0 = MARGIN + i * (CELL_W + GAP)

        # Photo (contain-fit; gray placeholder when the download failed).
        if it.image:
            try:
                photo = _fit_on_white(Image.open(io.BytesIO(it.image)), CELL_W, IMG_H)
            except Exception:
                photo = Image.new("RGB", (CELL_W, IMG_H), (241, 239, 232))
        else:
            photo = Image.new("RGB", (CELL_W, IMG_H), (241, 239, 232))
        card.paste(photo, (x0, MARGIN))

        # Numbered badge, top-left corner of the photo.
        bx, by = x0 + 12 + BADGE_R, MARGIN + 12 + BADGE_R
        draw.ellipse((bx - BADGE_R, by - BADGE_R, bx + BADGE_R, by + BADGE_R), fill=BADGE_BG)
        num = str(it.ref)
        tw = draw.textlength(num, font=f_badge)
        draw.text((bx - tw / 2, by - 21), num, font=f_badge, fill=BADGE_FG)

        # Caption strip UNDER the photo: name (1 line, ellipsized) + price.
        cy = MARGIN + IMG_H
        name = _ellipsize(draw, it.name.strip(), f_name, CELL_W - 20)
        draw.text((x0 + 10, cy + 14), name, font=f_name, fill=CAPTION_NAME)
        draw.text((x0 + 10, cy + 48), _fmt_price(it.price_amount, it.currency),
                  font=f_price, fill=CAPTION_PRICE)

        # Hairline between cells.
        if i > 0:
            lx = x0 - GAP // 2
            draw.line((lx, MARGIN, lx, MARGIN + IMG_H + CAPTION_H), fill=HAIRLINE, width=1)

    # Footer strip.
    fy = MARGIN + IMG_H + CAPTION_H
    draw.line((MARGIN, fy, width - MARGIN, fy), fill=HAIRLINE, width=1)
    draw.text((MARGIN + 6, fy + 13), "kapruka.com", font=f_footer, fill=FOOTER_FG)
    hint = _reply_hint([it.ref for it in items])
    hw = draw.textlength(hint, font=f_footer)
    draw.text((width - MARGIN - 6 - hw, fy + 13), hint, font=f_footer, fill=FOOTER_FG)

    out = io.BytesIO()
    card.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def card_filename(items: list[CardItem]) -> str:
    """Content-addressed name: same products+refs+prices → same file forever."""
    key = "|".join(f"{it.ref}:{it.product_id}:{it.price_amount}:{it.currency}" for it in items)
    return hashlib.sha1(key.encode()).hexdigest()[:16] + ".jpg"


def save_card(items: list[CardItem]) -> str:
    """Render (or reuse cached) card; returns the filename under CARD_DIR."""
    _CARD_DIR.mkdir(parents=True, exist_ok=True)
    name = card_filename(items)
    path = _CARD_DIR / name
    if not path.is_file():
        path.write_bytes(render_card(items))
        _prune()
    return name


def card_path(name: str) -> Path:
    return _CARD_DIR / name


def _prune() -> None:
    """Drop cards older than 7 days (best effort)."""
    try:
        cutoff = time.time() - _CARD_MAX_AGE_S
        for p in _CARD_DIR.glob("*.jpg"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except Exception:
        logger.warning("card prune failed", exc_info=True)
