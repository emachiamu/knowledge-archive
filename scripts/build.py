#!/usr/bin/env python3
"""
Build script for the Knowledge Archive static site.

What it does, in order:
  1. Parse content/archive.md into categories -> list of (title, url) entries.
  2. For every URL, fetch {title, extract, thumbnail} from the Wikipedia REST
     API, unless it is already in data/cache.json (so re-running the build
     only hits the network for links you just added).
  3. Render the homepage, one page per category, and a search index using
     the Jinja2 templates in templates/.
  4. Copy static assets (CSS/JS) into the output folder.

Usage:
    python scripts/build.py              # normal build, uses cache
    python scripts/build.py --refresh    # re-fetch every URL, ignore cache
    python scripts/build.py --offline    # skip fetching entirely, use cache only

Run this locally, or let the GitHub Actions workflow (.github/workflows/deploy.yml)
run it automatically whenever content/archive.md changes.
"""
import argparse
import json
import re
import shutil
import sys
import time
import random
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
CONTENT_MD = ROOT / "content" / "archive.md"
CACHE_FILE = ROOT / "data" / "cache.json"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "docs"  # GitHub Pages serves from /docs on the main branch

USER_AGENT = "KnowledgeArchive/1.0 (https://github.com/emachiamu/knowledge-archive; personal knowledge archive project)"
SITE_TITLE = "Knowledge Archive"

CATEGORY_RE = re.compile(r"^(.+?)\s*((?:\[\[[a-z0-9\- ]+\]\]\s*,?\s*)+)$")
TAG_RE = re.compile(r"\[\[([a-z0-9\- ]+)\]\]")
ENTRY_RE = re.compile(r"^(.+?):\s*(https?://\S+)\s*$")

# Cache saving interval (number of successful fetches between saves)
CACHE_SAVE_INTERVAL = 25

# Maximum number of retries for failed requests
MAX_RETRIES = 6

# Base delay between requests (in seconds)
BASE_REQUEST_DELAY = 1.0


# ---------------------------------------------------------------------------
# 1. Parse the markdown archive
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def repair_url(url: str) -> str:
    """Best-effort fix for a couple of known malformed-URL patterns so a typo
    in the markdown doesn't silently drop an entire entry."""
    if "wikipedia.org/wiki/" in url:
        return url
    m = re.match(r"^https?://([a-z]{2,3})\.wikipediaorgwiki(.+)$", url)
    if m:
        lang, rest = m.groups()
        return f"https://{lang}.wikipedia.org/wiki/{rest}"
    return url


def parse_archive(md_path: Path):
    """Returns a list of category dicts, in file order:
    {name, slug, tags: [...], entries: [{title, url}, ...]}
    """
    categories = []
    current = None

    for raw_line in md_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        cat_match = CATEGORY_RE.match(line)
        if cat_match:
            name = cat_match.group(1).strip()
            tags = TAG_RE.findall(cat_match.group(2))
            slug = slugify(tags[0] if tags else name)
            current = {"name": name, "slug": slug, "tags": tags, "entries": []}
            categories.append(current)
            continue

        entry_match = ENTRY_RE.match(line)
        if entry_match and current is not None:
            title, url = entry_match.groups()
            current["entries"].append({"title": title.strip(), "url": repair_url(url.strip())})
            continue

    return [c for c in categories if c["entries"]]


# ---------------------------------------------------------------------------
# 2. Fetch + cache Wikipedia metadata (with robust retry logic)
# ---------------------------------------------------------------------------

def wiki_lang_and_title(url: str) -> Tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    lang = parsed.netloc.split(".")[0]
    page_title = parsed.path.rsplit("/wiki/", 1)[-1]
    return lang, page_title


def resolve_redirect(lang: str, page_title: str, session: requests.Session) -> Optional[str]:
    """Resolve Wikipedia redirects using the MediaWiki API."""
    query_url = (
        f"https://{lang}.wikipedia.org/w/api.php"
        f"?action=query&titles={urllib.parse.quote(page_title)}"
        "&redirects=1&format=json"
    )
    try:
        resp = session.get(query_url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        if pages:
            # Get the first page (the key is the page ID or -1 for missing)
            page = next(iter(pages.values()))
            if "missing" not in page:
                return page.get("title")
    except Exception:
        pass
    return None


def fetch_summary_with_retry(url: str, session: requests.Session) -> Dict[str, Any]:
    """
    Fetch Wikipedia summary with robust retry logic, redirect resolution,
    and exponential backoff for rate limiting.
    """
    lang, page_title = wiki_lang_and_title(url)
    
    # First, try to resolve any redirects
    resolved_title = resolve_redirect(lang, page_title, session)
    if resolved_title and resolved_title != page_title:
        page_title = resolved_title
    
    # Now fetch the summary
    api_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(page_title)}"
    
    retry_count = 0
    while retry_count <= MAX_RETRIES:
        try:
            resp = session.get(api_url, timeout=15)
            
            if resp.status_code == 404:
                # Try one more time with redirect resolution
                resolved_title = resolve_redirect(lang, page_title, session)
                if resolved_title and resolved_title != page_title:
                    api_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(resolved_title)}"
                    resp = session.get(api_url, timeout=15)
                    if resp.status_code == 200:
                        page_title = resolved_title
                        break
            
            if resp.status_code == 429:
                # Rate limited - wait and retry
                retry_after = int(resp.headers.get("Retry-After", 10))
                wait_time = retry_after + random.uniform(0.5, 2.0)
                print(f"  ⏳ Rate limited for {page_title}, waiting {wait_time:.1f}s...")
                time.sleep(wait_time)
                retry_count += 1
                continue
            
            if resp.status_code >= 500:
                # Server error - retry with backoff
                wait_time = (2 ** retry_count) + random.uniform(0.5, 1.0)
                print(f"  ⏳ Server error ({resp.status_code}) for {page_title}, retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                retry_count += 1
                continue
            
            resp.raise_for_status()
            break
            
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            wait_time = (2 ** retry_count) + random.uniform(0.5, 1.0)
            print(f"  ⏳ Connection error: {e}, retrying in {wait_time:.1f}s...")
            time.sleep(wait_time)
            retry_count += 1
            continue
    
    # If we exhausted retries, raise the last error
    if retry_count > MAX_RETRIES:
        raise requests.exceptions.RequestException(f"Max retries exceeded for {api_url}")
    
    data = resp.json()
    thumb = data.get("thumbnail") or data.get("originalimage") or {}
    return {
        "title": data.get("title", page_title.replace("_", " ")),
        "extract": data.get("extract", ""),
        "image": thumb.get("source"),
        "image_width": thumb.get("width"),
        "image_height": thumb.get("height"),
        "wiki_url": data.get("content_urls", {}).get("desktop", {}).get("page", url),
        "lang": lang,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def create_robust_session() -> requests.Session:
    """Create a requests session with retry logic and proper headers."""
    session = requests.Session()
    
    # Set user agent
    session.headers.update({"User-Agent": USER_AGENT})
    
    # Configure retry strategy
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=10,
        pool_maxsize=10,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    return session


def load_cache() -> Dict[str, Any]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("Warning: cache.json is corrupted, starting with empty cache")
            return {}
    return {}


def save_cache(cache: Dict[str, Any]):
    """Save cache atomically to avoid corruption."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = CACHE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8"
    )
    temp_file.replace(CACHE_FILE)


def enrich_categories(categories: List[Dict], refresh: bool = False, offline: bool = False) -> List[Dict]:
    """Fetch Wikipedia metadata for all entries with robust retry logic."""
    cache = load_cache()
    session = create_robust_session()
    
    stats = {
        "fetched": 0,
        "from_cache": 0,
        "failed": 0,
        "redirected": 0,
        "retried": 0,
    }
    
    # Count total entries
    total_entries = sum(len(c["entries"]) for c in categories)
    processed = 0
    
    for cat in categories:
        for entry in cat["entries"]:
            url = entry["url"]
            processed += 1
            
            # Check cache
            cached = cache.get(url)
            if cached and not refresh:
                entry.update(cached)
                stats["from_cache"] += 1
                continue
            
            if offline:
                # Offline mode: use minimal placeholder
                lang, _ = wiki_lang_and_title(url)
                entry.update({
                    "title": entry["title"],
                    "extract": "",
                    "image": None,
                    "wiki_url": url,
                    "lang": lang,
                    "fetched_at": None,
                })
                stats["from_cache"] += 1
                continue
            
            # Fetch from Wikipedia with retry
            try:
                meta = fetch_summary_with_retry(url, session)
                cache[url] = meta
                entry.update(meta)
                stats["fetched"] += 1
                
                # Check if URL was redirected
                if meta.get("wiki_url") != url and meta.get("wiki_url"):
                    stats["redirected"] += 1
                
                print(f"  ✓ {meta['title']} ({stats['fetched']} fetched, {stats['from_cache']} cached, {stats['failed']} failed)")
                
            except Exception as exc:
                print(f"  ! failed to fetch {url}: {exc}")
                stats["failed"] += 1
                # Use cached version if available, otherwise fallback
                fallback = cache.get(url) or {
                    "title": entry["title"],
                    "extract": "",
                    "image": None,
                    "wiki_url": url,
                    "lang": wiki_lang_and_title(url)[0],
                    "fetched_at": None,
                }
                entry.update(fallback)
            
            # Save cache periodically
            if (stats["fetched"] + stats["from_cache"]) % CACHE_SAVE_INTERVAL == 0:
                save_cache(cache)
                print(f"  💾 Cache saved ({len(cache)} entries)")
            
            # Polite delay between requests (with jitter)
            if stats["fetched"] > 0:
                delay = BASE_REQUEST_DELAY + random.uniform(0.1, 0.5)
                time.sleep(delay)
    
    # Final cache save
    if not offline:
        save_cache(cache)
    
    print(f"\nWikipedia metadata summary:")
    print(f"  ✓ {stats['fetched']} new articles fetched")
    print(f"  ↪ {stats['redirected']} redirects resolved")
    print(f"  📦 {stats['from_cache']} articles from cache")
    print(f"  ⚠ {stats['failed']} articles failed")
    print(f"  📊 {len(cache)} total entries in cache")
    
    return categories


# ---------------------------------------------------------------------------
# 3. Render the site
# ---------------------------------------------------------------------------

def truncate(text: str, length: int = 280) -> str:
    text = (text or "").strip()
    if len(text) <= length:
        return text
    cut = text[:length].rsplit(" ", 1)[0]
    return cut + "…"


def build_site(categories: List[Dict]):
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["truncate_words"] = truncate

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)
    (OUTPUT_DIR / "categories").mkdir()

    total_articles = sum(len(c["entries"]) for c in categories)

    # Assign a library-style call number to each category, in file order.
    for i, cat in enumerate(categories, start=1):
        cat["call_number"] = f"{i:03d}"

    # "Recently added" = entries with the most recent fetched_at date.
    all_entries = []
    for cat in categories:
        for e in cat["entries"]:
            all_entries.append({**e, "category": cat["name"], "category_slug": cat["slug"]})
    recent = sorted(
        [e for e in all_entries if e.get("fetched_at")],
        key=lambda e: e["fetched_at"], reverse=True,
    )[:8]

    common_ctx = {
        "site_title": SITE_TITLE,
        "categories": categories,
        "total_articles": total_articles,
        "build_date": date.today().isoformat(),
    }

    # Homepage
    index_tpl = env.get_template("index.html")
    (OUTPUT_DIR / "index.html").write_text(
        index_tpl.render(**common_ctx, recent=recent, page_type="home"),
        encoding="utf-8",
    )

    # Category pages, with simple prev/next links following file order
    cat_tpl = env.get_template("category.html")
    for i, cat in enumerate(categories):
        prev_cat = categories[i - 1] if i > 0 else None
        next_cat = categories[i + 1] if i < len(categories) - 1 else None
        out = OUTPUT_DIR / "categories" / f"{cat['slug']}.html"
        out.write_text(
            cat_tpl.render(
                **common_ctx,
                category=cat,
                prev_cat=prev_cat,
                next_cat=next_cat,
                page_type="category",
            ),
            encoding="utf-8",
        )

    # Search page (client-side, driven by search-index.json)
    search_tpl = env.get_template("search.html")
    (OUTPUT_DIR / "search.html").write_text(
        search_tpl.render(**common_ctx, page_type="search"),
        encoding="utf-8",
    )

    # 404 page
    if (TEMPLATES_DIR / "404.html").exists():
        tpl404 = env.get_template("404.html")
        (OUTPUT_DIR / "404.html").write_text(
            tpl404.render(**common_ctx, page_type="404"), encoding="utf-8"
        )

    # Search index for client-side JS search
    search_index = [
        {
            "title": e["title"],
            "extract": truncate(e.get("extract", ""), 160),
            "url": e["url"],
            "category": e["category"],
            "category_slug": e["category_slug"],
        }
        for e in all_entries
    ]
    (OUTPUT_DIR / "search-index.json").write_text(
        json.dumps(search_index, ensure_ascii=False), encoding="utf-8"
    )

    # Static assets
    static_out = OUTPUT_DIR / "static"
    shutil.copytree(STATIC_DIR, static_out)

    # .nojekyll so GitHub Pages serves the docs/ folder as-is
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    print(f"\nBuilt {len(categories)} category pages + homepage + search page "
          f"({total_articles} articles) into {OUTPUT_DIR}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Ignore cache, re-fetch every URL")
    parser.add_argument("--offline", action="store_true", help="Never touch the network; cache only")
    args = parser.parse_args()

    print("=" * 60)
    print("Knowledge Archive Build")
    print("=" * 60)
    
    categories = parse_archive(CONTENT_MD)
    total = sum(len(c["entries"]) for c in categories)
    print(f"Parsed {len(categories)} categories, {total} articles from {CONTENT_MD.name}")

    categories = enrich_categories(categories, refresh=args.refresh, offline=args.offline)
    build_site(categories)
    
    print("\n" + "=" * 60)
    print("Build complete!")


if __name__ == "__main__":
    main()