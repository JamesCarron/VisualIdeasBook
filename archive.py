from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

SITEMAP_URL = "https://idea-milanicreative.beehiiv.com/sitemap.xml"
POST_PATH_MARKER = "/p/"
USER_AGENT = "VisualIdeas/1.0 (Personal Archive Tool)"


def fetch_post_urls(timeout: float = 30.0) -> list[str]:
    """Fetch the Beehiiv sitemap and return all post URLs (/p/* pattern).

    Returns URLs in sitemap order. Beehiiv's sitemap lists post URLs
    individually so a single fetch is sufficient — no pagination needed.
    """
    resp = httpx.get(
        SITEMAP_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "xml")
    urls: list[str] = []
    for loc in soup.find_all("loc"):
        url = loc.get_text(strip=True)
        if POST_PATH_MARKER in url:
            urls.append(url)
    return urls
