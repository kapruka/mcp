"""MCP tool: kapruka_list_categories."""

import json

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.client import KaprukaClient, handle_api_error
from src.server import mcp

# ── Helpers ───────────────────────────────────────────────────────────────────

# Fields stripped from upstream category objects before returning to clients.
# Counts and internal IDs are deliberately withheld to discourage catalog
# enumeration / scraping.
_STRIP_FIELDS = (
    "id", "slug", "parent_id", "path", "depth",
    "product_count", "seo_title", "url", "image_url",
)


def _sanitize(categories: list[dict], max_depth: int, current_depth: int = 1) -> list[dict]:
    """Return a copy of the tree with only `name`, `url`, and (optionally) `children`."""
    out: list[dict] = []
    for cat in categories:
        node: dict = {"name": cat.get("name", "")}
        url = cat.get("url")
        if url:
            node["url"] = url
        children = cat.get("children", []) or []
        if children and current_depth < max_depth:
            node["children"] = _sanitize(children, max_depth, current_depth + 1)
        out.append(node)
    return out


def _tree_to_markdown(categories: list[dict], indent: int = 0) -> list[str]:
    lines: list[str] = []
    prefix = "  " * indent
    for cat in categories:
        name = cat["name"]
        url = cat.get("url")
        line = f"{prefix}- [{name}]({url})" if url else f"{prefix}- {name}"
        lines.append(line)
        children = cat.get("children", [])
        if children:
            lines.extend(_tree_to_markdown(children, indent + 1))
    return lines


# ── Input model ───────────────────────────────────────────────────────────────


class ListCategoriesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    depth: int = Field(
        default=1,
        description="How many levels of sub-categories to include (1 or 2). Default 1.",
        ge=1,
        le=2,
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' for human-readable output, 'json' for raw structured data.",
    )

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


# ── Tool ─────────────────────────────────────────────────────────────────────


@mcp.tool(
    name="kapruka_list_categories",
    annotations={
        "title": "List Kapruka Product Categories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_list_categories(params: ListCategoriesInput) -> str:
    """List top-level Kapruka product categories by name with browse URLs.

    Returns category names (usable as the `category` filter on kapruka_search_products)
    plus the public Kapruka.com URL for each category landing page — useful for shopping
    agents that want to send users directly to a category to browse. Internal IDs and
    product counts are not exposed. Results are cached for 30 minutes server-side.

    Args:
        params (ListCategoriesInput):
            - depth (int): Sub-category levels to include, 1 or 2 (default 1)
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Category tree in the requested format.

        JSON schema:
        {
          "categories": [
            {
              "name": str,
              "url": str,                  # kapruka.com category landing page
              "children": [{"name": str, "url": str, "children": [...]}]
            }
          ]
        }

        Error: "Error: <message>" on failure.
    """
    try:
        client = KaprukaClient()
        data = await client.call("categories", depth=params.depth)
    except Exception as e:
        return handle_api_error(e)

    raw: list[dict] = data.get("categories", [])
    if not raw:
        return "No categories available."

    categories = _sanitize(raw, params.depth)

    if params.response_format == "json":
        return json.dumps({"categories": categories}, indent=2, ensure_ascii=False)

    lines = ["## Kapruka Categories", ""]
    lines.extend(_tree_to_markdown(categories))
    return "\n".join(lines)
