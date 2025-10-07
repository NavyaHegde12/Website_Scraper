# backend.py
# Async backend crawler — collects only image URLs (no file sizes).
import asyncio
import aiohttp
from urllib.parse import urljoin, urldefrag, urlparse
from bs4 import BeautifulSoup
import streamlit as st

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
}


def normalize_url(u: str) -> str:
    u, _ = urldefrag(u)
    return u


def abs_url(base: str, u: str) -> str:
    return urljoin(base, u)


def same_host(u: str, base_host: str) -> bool:
    try:
        return urlparse(u).netloc == base_host
    except Exception:
        return False


def extract_images_from_html(base_url: str, html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    imgs = set()

    valid_ext = ("jpg", "jpeg", "png", "gif", "webp", "svg")

    def is_valid_image(url: str):
        url = url.lower().split('?')[0]
        return any(url.endswith(f".{ext}") for ext in valid_ext)

    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or
               img.get("data-original") or img.get("data-lazy"))
        if src:
            full = normalize_url(abs_url(base_url, src))
            if is_valid_image(full):
                imgs.add(full)

        srcset = img.get("srcset")
        if srcset:
            for item in srcset.split(","):
                part = item.strip().split()
                if part:
                    candidate = normalize_url(abs_url(base_url, part[0]))
                    if is_valid_image(candidate):
                        imgs.add(candidate)

    return imgs


def extract_links(base_url: str, html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = normalize_url(abs_url(base_url, a["href"]))
        links.add(href)
    return links


async def fetch_text(session: aiohttp.ClientSession, url: str, timeout: int = 10):
    try:
        async with session.get(url, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "").lower()
            if "text" in ctype or "html" in ctype:
                return await r.text()
            return ""
    except asyncio.CancelledError:
        raise
    except Exception:
        return ""


async def scan_site(
    base_url: str,
    max_pages: int = 100,
    max_concurrency: int = 12,
    progress_state: dict | None = None
):
    base_host = urlparse(base_url).netloc
    seen_pages = set()
    to_visit = [base_url]
    found_images = set()
    sem = asyncio.Semaphore(max_concurrency)

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:

        async def visit(url):
            async with sem:
                if st.session_state.get("stop_flag"):
                    return [], []
                if progress_state is not None:
                    progress_state["current_activity"] = f"Scraping: {url}"
                html = await fetch_text(session, url)
                if not html:
                    return [], []
                imgs = extract_images_from_html(url, html)
                links = extract_links(url, html)
                return list(imgs), list(links)

        while to_visit and len(seen_pages) < max_pages:
            if st.session_state.get("stop_flag"):
                break

            batch = []
            for _ in range(min(max_concurrency, len(to_visit), max_pages - len(seen_pages))):
                if not to_visit:
                    break
                url = to_visit.pop(0)
                if url in seen_pages:
                    continue
                if not same_host(url, base_host):
                    continue
                seen_pages.add(url)
                batch.append(url)

            if not batch:
                break

            tasks = [visit(u) for u in batch]
            results = await asyncio.gather(*tasks)

            for imgs, links in results:
                for img in imgs:
                    found_images.add(img)
                for link in links:
                    if link not in seen_pages and same_host(link, base_host) and len(seen_pages) < max_pages:
                        to_visit.append(link)

            if progress_state is not None:
                progress_state["pages_visited"] = len(seen_pages)
                progress_state["images_found"] = len(found_images)

    st.session_state["all_images"] = list(found_images)
    return list(found_images), len(seen_pages)
