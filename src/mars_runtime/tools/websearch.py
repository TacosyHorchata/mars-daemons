"""WebSearch — DuckDuckGo HTML scrape (no API key needed).

Minimal v0 implementation. Swap in Brave/Serper later by setting
`MARS_SEARCH_API_KEY` + provider — kept out of v0 to avoid auth ceremony.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import unquote

import httpx

from . import Tool, ToolOutput, register

_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(html: str) -> str:
    return unescape(_TAG_RE.sub("", html)).strip()


def _extract_url(href: str) -> str:
    # DuckDuckGo wraps results as /l/?uddg=<encoded-url>&...
    match = re.search(r"uddg=([^&]+)", href)
    return unquote(match.group(1)) if match else href


def _websearch(input_: dict[str, Any]) -> ToolOutput:
    query = input_["query"]
    max_results = input_.get("max_results", 5)

    try:
        resp = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (mars-daemons)"},
            timeout=10.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        return ToolOutput(f"search failed: {e}", is_error=True)

    lines: list[str] = []
    for i, (href, title, snippet) in enumerate(_RESULT_RE.findall(resp.text)):
        if i >= max_results:
            break
        lines.append(f"{i + 1}. {_clean(title)}\n   {_extract_url(href)}\n   {_clean(snippet)}")

    return ToolOutput("\n\n".join(lines) or "(no results)")


register(
    Tool(
        name="websearch",
        description="Search the web (DuckDuckGo). Returns top N results with titles, urls, snippets.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        fn=_websearch,
    )
)
