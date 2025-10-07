# app.py
# pip install streamlit aiohttp beautifulsoup4 pandas openpyxl

import streamlit as st
import asyncio
import aiohttp
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urldefrag, urlparse
import pandas as pd
import time, re, os
from io import BytesIO
from typing import Set, Dict

# ---------- CONFIG ----------
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
}
VALID_IMAGE_EXTS = ("jpg", "jpeg", "png", "gif", "webp", "svg")
EXCLUDE_KEYWORDS = ["logo", "icon", "favicon"]

# ---------- HELPERS ----------
def normalize_url(u: str) -> str:
    u, _ = urldefrag(u)
    parsed = urlparse(u)
    return parsed._replace(query="", fragment="").geturl()

def abs_url(base: str, u: str) -> str:
    return urljoin(base, u)

def same_host(u: str, base_host: str) -> bool:
    try:
        return urlparse(u).netloc == base_host
    except:
        return False

def is_image_url(url: str) -> bool:
    if not url:
        return False
    path = url.split("?")[0].lower()
    return any(path.endswith(f".{ext}") for ext in VALID_IMAGE_EXTS)

def is_excluded(url: str) -> bool:
    return any(kw in url.lower() for kw in EXCLUDE_KEYWORDS)

def extract_images_from_html(base_url: str, html: str, keywords: list) -> Set[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    imgs = set()
    def keyword_match(text: str) -> bool:
        if not keywords:
            return True
        text = (text or "").lower()
        return any(k in text for k in keywords)

    # <img> tags
    for img in soup.find_all("img"):
        candidates = [img.get(k) for k in ["src", "data-src", "data-original", "data-lazy"]]
        alt_text = (img.get("alt") or "") + " " + (img.get("title") or "")
        for c in candidates:
            if c:
                full = normalize_url(abs_url(base_url, c))
                if is_image_url(full) and not is_excluded(full):
                    if keyword_match(full) or keyword_match(alt_text):
                        imgs.add(full)
        srcset = img.get("srcset")
        if srcset:
            for part in srcset.split(","):
                url = part.strip().split()[0]
                full = normalize_url(abs_url(base_url, url))
                if is_image_url(full) and not is_excluded(full):
                    if keyword_match(full) or keyword_match(alt_text):
                        imgs.add(full)

    # Inline styles
    for tag in soup.find_all(style=True):
        urls = re.findall(r"url\(([^)]+)\)", tag["style"])
        text = tag.get_text(" ")
        for u in urls:
            u = u.strip(' "\'')
            full = normalize_url(abs_url(base_url, u))
            if is_image_url(full) and not is_excluded(full):
                if keyword_match(full) or keyword_match(text):
                    imgs.add(full)

    # <style> blocks
    for style_tag in soup.find_all("style"):
        urls = re.findall(r"url\(([^)]+)\)", style_tag.get_text())
        for u in urls:
            u = u.strip(' "\'')
            full = normalize_url(abs_url(base_url, u))
            if is_image_url(full) and not is_excluded(full):
                if keyword_match(full):
                    imgs.add(full)

    # Meta tags
    for meta in soup.find_all("meta", content=True):
        c = meta["content"]
        if c:
            full = normalize_url(abs_url(base_url, c))
            if is_image_url(full) and not is_excluded(full):
                if keyword_match(full):
                    imgs.add(full)

    # <link> tags
    for link in soup.find_all("link", href=True):
        href = link["href"]
        if href:
            full = normalize_url(abs_url(base_url, href))
            if is_image_url(full) and not is_excluded(full):
                if keyword_match(full):
                    imgs.add(full)

    # Video posters
    for video in soup.find_all("video", poster=True):
        p = video["poster"]
        title = video.get("title") or ""
        if p:
            full = normalize_url(abs_url(base_url, p))
            if is_image_url(full) and not is_excluded(full):
                if keyword_match(full) or keyword_match(title):
                    imgs.add(full)

    return imgs

def extract_links(base_url: str, html: str) -> Set[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        links.add(normalize_url(abs_url(base_url, a["href"])))
    return links

# ---------- NETWORK ----------
async def fetch_text(session, url, timeout=10):
    try:
        async with session.get(url, timeout=ClientTimeout(total=timeout)) as r:
            if "text" in r.headers.get("Content-Type", "").lower():
                return await r.text(errors="ignore")
    except:
        pass
    return ""

async def get_head_size(session, url, timeout=6):
    try:
        async with session.head(url, timeout=ClientTimeout(total=timeout), allow_redirects=True) as r:
            cl = r.headers.get("content-length")
            if cl and cl.isdigit():
                return int(cl)/1024.0
    except:
        pass
    return 0.0

async def fetch_sizes_async(urls, concurrency=20, timeout=8):
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS, timeout=ClientTimeout(total=timeout)) as session:
        async def worker(u):
            async with sem:
                try:
                    return round(await get_head_size(session, u, timeout), 2)
                except:
                    return 0.0
        return await asyncio.gather(*[worker(u) for u in urls])

def fetch_sizes(urls, concurrency=20, timeout=8):
    return asyncio.run(fetch_sizes_async(urls, concurrency, timeout))

# ---------- CRAWLER ----------
async def crawl_site(base_url, max_pages, concurrency, progress_state, keywords=[]):
    base_host = urlparse(base_url).netloc
    seen, to_visit, found_images = set(), [base_url], set()
    sem = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS, timeout=ClientTimeout(total=15)) as session:
        async def visit(url):
            async with sem:
                if st.session_state.get("stop_flag"):
                    return [], []
                progress_state["current_activity"] = f"Fetching {url}"
                html = await fetch_text(session, url, timeout=12)
                return list(extract_images_from_html(url, html, keywords)), list(extract_links(url, html))

        while to_visit and len(seen) < max_pages:
            if st.session_state.get("stop_flag"): break
            batch = []
            for _ in range(min(concurrency, len(to_visit), max_pages - len(seen))):
                u = to_visit.pop(0)
                if u not in seen and same_host(u, base_host):
                    seen.add(u)
                    batch.append(u)
            if not batch: break
            results = await asyncio.gather(*[visit(u) for u in batch])
            for imgs, links in results:
                found_images.update(imgs)
                for l in links:
                    if l not in seen and same_host(l, base_host) and len(seen) < max_pages:
                        to_visit.append(l)
            progress_state["pages_visited"] = len(seen)
            progress_state["images_found"] = len(found_images)

    return list(found_images), len(seen)

# ---------- STREAMLIT UI ----------
st.set_page_config(page_title="Image Scanner", layout="wide", page_icon="🖼")
st.title("Scraping Tool For Websites")

if "all_images" not in st.session_state: st.session_state["all_images"] = []
if "stop_flag" not in st.session_state: st.session_state["stop_flag"] = False
if "last_scan_time" not in st.session_state: st.session_state["last_scan_time"] = None

with st.sidebar:
    st.header("Crawl Controls")
    base_url = st.text_input("Base URL")
    keywords_input = st.text_input("Keywords (comma-separated)")
    keywords = [k.strip().lower() for k in keywords_input.split(",") if k.strip()]
    max_pages = st.number_input("Max pages", 1, 5000, 50)
    concurrency = st.slider("Concurrency", 1, 50, 12)
    start = st.button("Start Scan")
    stop = st.button("Stop Scan")
    st.markdown("---")
    st.header("Size Filter")
    min_size = st.number_input("Min KB", 0.0)
    max_size = st.number_input("Max KB (0=no max)", 0.0)
    apply_size = st.button("Apply Size Filter")

status_col, main_col = st.columns([1,3])
with status_col:
    pages_metric = st.empty()
    images_metric = st.empty()
    activity = st.empty()
    last_scan = st.empty()

with main_col:
    st.subheader("Image Previews / Results")

if stop: st.session_state["stop_flag"] = True

if start and base_url:
    st.session_state["all_images"] = []
    st.session_state["stop_flag"] = False
    st.session_state["last_scan_time"] = None
    progress_state = {"pages_visited":0, "images_found":0, "current_activity":"Starting..."}

    async def runner():
        return await crawl_site(base_url, max_pages, concurrency, progress_state, keywords)

    imgs, pages = asyncio.run(runner())
    st.session_state["all_images"] = imgs
    st.session_state["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    pages_metric.metric("Pages visited", pages)
    images_metric.metric("Images found", len(imgs))
    activity.success("Crawl completed.")

all_images = st.session_state.get("all_images", [])
df = pd.DataFrame({"URL": all_images})

if not df.empty:
    activity.info("Fetching sizes and processing filenames...")
    df["Size (KB)"] = fetch_sizes(df["URL"].tolist(), concurrency=min(50, concurrency*2))
    df["Filename"] = df["URL"].apply(lambda u: u.split("/")[-1].split("?")[0])
    df = df.sort_values("Size (KB)", ascending=False).drop_duplicates(subset="Filename", keep="first").reset_index(drop=True)

    # Split filename into up to 4 parts (non-alphanumeric)
    base_names = df["Filename"].apply(lambda f: os.path.splitext(f)[0])
    split_parts = base_names.apply(lambda x: pd.Series(re.findall(r'[A-Za-z0-9]+', x)[:4]))
    split_parts.columns = [f"Part{i+1}" for i in range(split_parts.shape[1])]

    # File type at the end
    filetypes = df["Filename"].apply(lambda f: os.path.splitext(f)[1].lstrip(".").lower())
    df = pd.concat([df[["URL","Filename","Size (KB)"]], split_parts], axis=1)
    df["FileType"] = filetypes

    # Apply size filter
    if apply_size:
        if max_size>0:
            df = df[(df["Size (KB)"] >= min_size) & (df["Size (KB)"] <= max_size)]
        else:
            df = df[df["Size (KB)"] >= min_size]

if df.empty:
    st.info("No images to display.")
else:
    last_scan.info(f"Last scan: {st.session_state.get('last_scan_time')}")
    cols = st.columns(3)
    for idx, (_, row) in enumerate(df.head(300).iterrows()):
        col = cols[idx % 3]
        with col:
            caption = f"{row['Filename']} — {row['Size (KB)']:.1f} KB"
            try:
                st.image(row["URL"], caption=caption, use_container_width=True)
            except:
                st.write(caption)
                st.write(f"[Open]({row['URL']})")

    # Export CSV & Excel
    csv = df.to_csv(index=False)
    st.download_button("Download CSV", csv, f"images_{time.strftime('%Y%m%d_%H%M%S')}.csv", "text/csv")
    xls_buf = BytesIO()
    with pd.ExcelWriter(xls_buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Results")
    st.download_button("Download Excel", xls_buf.getvalue(), f"images_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
