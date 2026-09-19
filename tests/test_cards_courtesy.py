"""Options card: the home-currency courtesy line and footer note.

Why this exists: on 2026-09-02 the "one price currency" rule moved every
overseas customer's card to USD, and the courtesy figure the chat TEXT carries
("USD 16.56 (≈ JPY 2,544)") never reached the image — customers saw yen in
the text and dollars only on the picture. The caller (eagle) now passes the
rate it used, the card prints the same figure, and the response echoes what
was printed so the caller can verify it. These tests pin that contract.
"""

import io
import json

import pytest
from PIL import Image

from src import cards as cards_mod
from src.cards import CardItem, card_filename, fmt_courtesy, render_card
from src.tools import cards as cards_tool
from src.tools.cards import RenderOptionsCardInput, kapruka_render_options_card


# ---------- formatter: same rounding rule as eagle's formatLocal ----------

def test_fmt_courtesy_whole_units():
    assert fmt_courtesy(16.56, 153.6, "JPY") == "≈ JPY 2,544"


def test_fmt_courtesy_coarser_as_numbers_grow():
    assert fmt_courtesy(18.07, 0.74, "gbp") == "≈ GBP 13"
    assert fmt_courtesy(17.96, 1386.2, "KRW") == "≈ KRW 24,900"      # ≥10k → nearest 100
    assert fmt_courtesy(120.0, 1386.2, "KRW") == "≈ KRW 166,000"     # ≥100k → nearest 1,000


# ---------- renderer ----------

def _item(ref, note=None, price=16.56, cur="USD"):
    return CardItem(ref=ref, product_id=f"P{ref}", name=f"Product {ref}", price_amount=price, currency=cur, image=None, price_note=note)


def test_render_grows_for_note_and_accepts_footer():
    plain = Image.open(io.BytesIO(render_card([_item(1), _item(2)])))
    noted = Image.open(io.BytesIO(render_card([_item(1, "≈ JPY 2,544"), _item(2, "≈ JPY 1,450")], footer_note="Checkout charges USD")))
    assert noted.width == plain.width
    assert noted.height == plain.height + cards_mod.NOTE_H


def test_filename_changes_with_note_and_footer():
    a = card_filename([_item(1)])
    b = card_filename([_item(1, "≈ JPY 2,544")])
    c = card_filename([_item(1, "≈ JPY 2,544")], footer_note="Checkout charges USD")
    assert len({a, b, c}) == 3, "a note or footer must produce a different cached card"


# ---------- tool ----------

class _StubClient:
    prices = {"PROD1": 16.56, "PROD2": 9.44}

    async def call(self, endpoint, **params):
        pid = params["product_id"]
        cur = params.get("currency", "LKR")
        amount = self.prices[pid] if cur == "USD" else self.prices[pid] * 300
        return {"name": f"Product {pid}", "price": {"amount": amount, "currency": cur}, "images": [], "url": f"https://www.kapruka.com/x/{pid}"}


@pytest.fixture(autouse=True)
def _stub(monkeypatch, tmp_path):
    monkeypatch.setattr(cards_tool, "KaprukaClient", _StubClient)
    monkeypatch.setattr(cards_mod, "_CARD_DIR", tmp_path)


async def _run(**kw):
    params = RenderOptionsCardInput(items=[{"product_id": "PROD1", "ref": 1}, {"product_id": "PROD2", "ref": 2}], **kw)
    return json.loads(await kapruka_render_options_card(params))


@pytest.mark.asyncio
async def test_usd_card_prints_and_echoes_courtesy():
    out = await _run(currency="USD", courtesy={"currency": "jpy", "per_usd": 153.6}, footer_note="Checkout charges USD")
    assert out["courtesy"] == {"currency": "JPY", "per_usd": 153.6}
    assert [i["price_note"] for i in out["items"]] == ["≈ JPY 2,544", "≈ JPY 1,450"]
    assert out["card_url"].endswith(".jpg")


@pytest.mark.asyncio
async def test_lkr_card_ignores_courtesy():
    out = await _run(currency="LKR", courtesy={"currency": "JPY", "per_usd": 153.6})
    assert out["courtesy"] is None
    assert all(i["price_note"] is None for i in out["items"])


@pytest.mark.asyncio
async def test_no_courtesy_is_the_old_shape():
    out = await _run(currency="USD")
    assert out["courtesy"] is None
    assert all(i["price_note"] is None for i in out["items"])


def test_courtesy_validation():
    with pytest.raises(ValueError):
        RenderOptionsCardInput(items=[{"product_id": "PROD1", "ref": 1}], currency="USD", courtesy={"currency": "JPYX", "per_usd": 1})
    with pytest.raises(ValueError):
        RenderOptionsCardInput(items=[{"product_id": "PROD1", "ref": 1}], currency="USD", courtesy={"currency": "JPY", "per_usd": 0})
