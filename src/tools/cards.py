"""MCP tool: kapruka_render_options_card — numbered product-menu JPEGs for chat."""

import asyncio
import json
import logging
import os

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.client import KaprukaClient, handle_api_error
from src.cards import CardItem, save_card
from src.server import mcp
from src.tools.orders import SUPPORTED_CURRENCIES

logger = logging.getLogger(__name__)

_BASE_URL = os.getenv("CARD_BASE_URL", "https://mcp.kapruka.com").rstrip("/")
_IMG_TIMEOUT = 15.0


class CardProduct(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    product_id: str = Field(..., description="Kapruka product ID (from a search result).", min_length=3, max_length=80)
    ref: int = Field(..., description="The reference number to print on this product's badge (customer replies with it). Assign sequentially per conversation and NEVER reuse a number.", ge=1, le=99)


class RenderOptionsCardInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    items: list[CardProduct] = Field(..., description="1-4 products to show side by side.", min_length=1, max_length=4)
    currency: str = Field(default="LKR", description=f"Price currency: {', '.join(SUPPORTED_CURRENCIES)}.")

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"currency must be one of: {', '.join(SUPPORTED_CURRENCIES)}")
        return v

    @field_validator("items")
    @classmethod
    def validate_unique_refs(cls, v: list[CardProduct]) -> list[CardProduct]:
        refs = [i.ref for i in v]
        if len(set(refs)) != len(refs):
            raise ValueError("ref numbers must be unique within one card")
        return v


# kapruka.com images sit behind Cloudflare bot protection: a UA-less request
# gets a 403 challenge page (text/html) instead of the JPEG. A browser UA
# passes. Guard on content-type too so an error page never reaches Pillow.
_IMG_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


async def _fetch_image(url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=_IMG_TIMEOUT, follow_redirects=True, headers=_IMG_HEADERS) as http:
            r = await http.get(url)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and r.content and ctype.startswith("image/"):
                return r.content
            logger.warning("card image fetch rejected: %s (HTTP %s, %s)", url, r.status_code, ctype)
    except Exception:
        logger.warning("card image fetch failed: %s", url)
    return None


@mcp.tool(
    name="kapruka_render_options_card",
    annotations={
        "title": "Render Product Options Card (Image)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_render_options_card(params: RenderOptionsCardInput) -> str:
    """Render 1-4 products as ONE shareable JPEG "menu" card and return its URL.

    The card shows each product's photo with a big numbered badge (the ref you
    assign), and its name + price printed under the photo. Built for chat
    commerce (WhatsApp): send the image, tell the customer "reply 1, 2 or 3",
    and they pick without opening links. No AI is involved — the image is
    server-composited from the live catalog data, so prices match what the
    product tools return.

    Ref numbering contract: refs are yours to assign — use sequential numbers
    per conversation and NEVER reuse one (if the first card was 1-3, the next
    card starts at 4). A number must keep meaning the same product for the whole
    conversation.

    Args:
        params (RenderOptionsCardInput):
            - items (list[CardProduct]): 1-4 of {product_id, ref}
            - currency (str): LKR (default), USD, GBP, AUD, CAD, EUR

    Returns:
        str: JSON:
        {
          "card_url": str,              # public JPEG URL — send this as the image
          "items": [{"ref": int, "product_id": str, "name": str,
                      "price": {"amount": float, "currency": str}, "url": str}],
          "unavailable": [str]          # product_ids that failed to load (omitted from card)
        }

        Error: "Error: <message>" when no product could be loaded.
    """
    client = KaprukaClient()

    async def fetch(p: CardProduct):
        try:
            data = await client.call("product", product_id=p.product_id, currency=params.currency)
            price = data.get("price") or {}
            images = data.get("images") or []
            img = await _fetch_image(images[0] if images else "")
            return p, data, img
        except Exception as e:
            logger.warning("card product fetch failed %s: %s", p.product_id, e)
            return p, None, None

    fetched = await asyncio.gather(*(fetch(p) for p in params.items))

    card_items: list[CardItem] = []
    meta: list[dict] = []
    unavailable: list[str] = []
    for p, data, img in fetched:
        if not data or not (data.get("price") or {}).get("amount"):
            unavailable.append(p.product_id)
            continue
        price = data["price"]
        card_items.append(CardItem(
            ref=p.ref,
            product_id=p.product_id,
            name=str(data.get("name") or p.product_id),
            price_amount=float(price["amount"]),
            currency=str(price.get("currency") or params.currency),
            image=img,
        ))
        meta.append({
            "ref": p.ref,
            "product_id": p.product_id,
            "name": data.get("name"),
            "price": {"amount": float(price["amount"]), "currency": price.get("currency") or params.currency},
            "url": data.get("url"),
        })

    if not card_items:
        return handle_api_error(Exception("none of the requested products could be loaded"))

    try:
        # Pillow is sync CPU work — keep the event loop free.
        name = await asyncio.to_thread(save_card, card_items)
    except Exception as e:
        logger.error("card render failed: %s", e, exc_info=True)
        return f"Error: card rendering failed ({e}). Fall back to a text list with links."

    return json.dumps({
        "card_url": f"{_BASE_URL}/cards/{name}",
        "items": meta,
        "unavailable": unavailable,
    }, ensure_ascii=False)
