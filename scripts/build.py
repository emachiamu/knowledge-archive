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


def resolve_redirect(lang: str, page_title: str, session: requests.Session) -> Optional[Dict[str, Any]]:
    """Resolve Wikipedia redirects and get the actual page info using MediaWiki API."""
    # Try different encodings for the page title
    titles_to_try = [
        page_title,
        urllib.parse.unquote(page_title),  # Decode URL encoding
        urllib.parse.quote(page_title),    # URL encode
        page_title.replace("_", " "),       # Replace underscores with spaces
        page_title.replace(" ", "_"),       # Replace spaces with underscores
    ]
    
    # Remove duplicates while preserving order
    seen = set()
    titles_to_try = [t for t in titles_to_try if not (t in seen or seen.add(t))]
    
    for title in titles_to_try:
        query_url = (
            f"https://{lang}.wikipedia.org/w/api.php"
            f"?action=query"
            f"&titles={urllib.parse.quote(title)}"
            "&redirects=1"
            "&converttitles=1"  # Convert to canonical title
            "&format=json"
        )
        try:
            resp = session.get(query_url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            pages = data.get("query", {}).get("pages", {})
            
            # Find the first valid page
            for page_id, page in pages.items():
                if page_id != "-1" and "missing" not in page:
                    # Check if this is a redirect
                    if "redirects" in data.get("query", {}):
                        for redirect in data["query"]["redirects"]:
                            if redirect.get("to"):
                                # This is a redirect, get the target
                                page = pages.get(str(redirect.get("to")), page)
                                break
                    return {
                        "title": page.get("title"),
                        "pageid": page.get("pageid"),
                        "redirected": title != page.get("title")
                    }
        except Exception:
            continue
    
    return None


def fetch_summary_with_retry(url: str, session: requests.Session) -> Dict[str, Any]:
    """
    Fetch Wikipedia summary with robust retry logic, redirect resolution,
    and exponential backoff for rate limiting.
    """
    lang, page_title = wiki_lang_and_title(url)
    
    # First, try to resolve redirects
    page_info = resolve_redirect(lang, page_title, session)
    
    if page_info:
        # Use the resolved title
        resolved_title = page_info["title"]
        if resolved_title and resolved_title != page_title:
            print(f"  ↪ Redirected from '{page_title}' to '{resolved_title}'")
            page_title = resolved_title
    else:
        # Try to find the page by searching for it
        search_url = (
            f"https://{lang}.wikipedia.org/w/api.php"
            f"?action=query"
            f"&list=search"
            f"&srsearch={urllib.parse.quote(page_title.replace('_', ' '))}"
            f"&srlimit=1"
            f"&format=json"
        )
        try:
            resp = session.get(search_url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            search_results = data.get("query", {}).get("search", [])
            if search_results:
                # Use the first search result
                search_title = search_results[0].get("title")
                if search_title:
                    print(f"  🔍 Found '{search_title}' via search for '{page_title}'")
                    page_title = search_title
        except Exception:
            pass
    
    # Now fetch the summary with the (possibly resolved) title
    api_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(page_title)}"
    
    retry_count = 0
    last_error = None
    
    while retry_count <= MAX_RETRIES:
        try:
            resp = session.get(api_url, timeout=15)
            
            if resp.status_code == 404:
                # Page not found - this might be a genuinely missing page
                # Try one more time with a different encoding
                alt_title = urllib.parse.unquote(page_title).replace("_", " ")
                if alt_title != page_title:
                    api_url_alt = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(alt_title)}"
                    resp_alt = session.get(api_url_alt, timeout=15)
                    if resp_alt.status_code == 200:
                        resp = resp_alt
                        break
                raise requests.exceptions.HTTPError(f"404 Not Found: {page_title}")
            
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
            last_error = e
            continue
        except requests.exceptions.HTTPError as e:
            # Don't retry 404s
            if e.response and e.response.status_code == 404:
                raise
            retry_count += 1
            last_error = e
            continue
    
    # If we exhausted retries, raise the last error
    if retry_count > MAX_RETRIES:
        raise last_error or requests.exceptions.RequestException(f"Max retries exceeded for {api_url}")
    
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
        "found_via_search": 0,
    }
    
    # Count total entries
    total_entries = sum(len(c["entries"]) for c in categories)
    processed = 0
    
    # Manual override for known problematic URLs
    manual_fixes = {
        "https://en.wikipedia.org/wiki/King%27s_College_Chapel,_Cambridge": "King's College Chapel, Cambridge",
        "https://en.wikipedia.org/wiki/Lion_Gate,_Mycenae": "Lion Gate",
        "https://en.wikipedia.org/wiki/Massimiliano_Locatelli": "Massimiliano Locatelli (architect)",
        "https://en.wikipedia.org/wiki/Fogong_Temple_Wooden_Pagoda": "Fogong Temple Pagoda",
        "https://en.wikipedia.org/wiki/Doge%27s_Palace": "Doge's Palace",
        "https://en.wikipedia.org/wiki/Rurik%C5%8D-ji": "Rurikō-ji",
        "https://en.wikipedia.org/wiki/Sir_John_Soane%27s_Museum": "Sir John Soane's Museum",
        "https://en.wikipedia.org/wiki/Speakers%27_Corner": "Speakers' Corner",
        "https://en.wikipedia.org/wiki/St._Mary%27s_Cathedral,_Tokyo": "St. Mary's Cathedral, Tokyo",
        "https://en.wikipedia.org/wiki/Trajan%27s_Kiosk": "Trajan's Kiosk",
        "https://en.wikipedia.org/wiki/Arnold_B%C3%B6cklin": "Arnold Böcklin",
        "https://en.wikipedia.org/wiki/Jupiter_and_Semele_(Moreau)": "Jupiter and Semele",
        "https://en.wikipedia.org/wiki/Artist%27s_Shit": "Artist's Shit",
        "https://en.wikipedia.org/wiki/Banditaccia_necropolis": "Banditaccia Necropolis",
        "https://en.wikipedia.org/wiki/Trompe-l%27%C5%93il": "Trompe-l'œil",
        "https://en.wikipedia.org/wiki/Faberg%C3%A9_egg": "Fabergé egg",
        "https://en.wikipedia.org/wiki/Aberration-corrected_electron_microscope": "Aberration-corrected transmission electron microscopy",
        "https://en.wikipedia.org/wiki/Lempel%E2%80%93Ziv%E2%80%93Markov_chain_algorithm": "Lempel–Ziv–Markov chain algorithm",
        "https://it.wikipedia.org/wiki/Curva_di_B%C3%A9zier": "Curva di Bézier",
        "https://en.wikipedia.org/wiki/Pascal%27s_calculator": "Pascal's calculator",
        "https://it.wikipedia.org/wiki/Carl_Friedrich_von_Weizs%C3%A4cker": "Carl Friedrich von Weizsäcker",
        "https://en.wikipedia.org/wiki/Maxwell%27s_demon": "Maxwell's demon",
        "https://en.wikipedia.org/wiki/Elitzur%E2%80%93Vaidman_bomb_tester": "Elitzur–Vaidman bomb tester",
        "https://it.wikipedia.org/wiki/Gruppo_di_Poincar%C3%A9": "Gruppo di Poincaré",
        "https://en.wikipedia.org/wiki/Hilbert%27s_problems": "Hilbert's problems",
        "https://en.wikipedia.org/wiki/Mach%E2%80%93Zehnder_interferometer": "Mach–Zehnder interferometer",
        "https://en.wikipedia.org/wiki/Moir%C3%A9_pattern": "Moiré pattern",
        "https://en.wikipedia.org/wiki/Landauer%27s_principle": "Landauer's principle",
        "https://en.wikipedia.org/wiki/Sol%C3%A8r%27s_theorem": "Solèr's theorem",
        "https://en.wikipedia.org/wiki/Bell%27s_theorem": "Bell's theorem",
        "https://it.wikipedia.org/wiki/Teorema_di_Rouch%C3%A9-Capelli": "Teorema di Rouché-Capelli",
        "https://en.wikipedia.org/wiki/Assassination_attempt_on_Pope_John_Paul_II": "Attempted assassination of Pope John Paul II",
        "https://en.wikipedia.org/wiki/Government_of_the_Nine": "The Nine (Siena)",
        "https://en.wikipedia.org/wiki/Hermann_G%C3%B6ring": "Hermann Göring",
        "https://en.wikipedia.org/wiki/Lorenzo_de%27_Medici": "Lorenzo de' Medici",
        "https://en.wikipedia.org/wiki/Men%C3%A9ndez_brothers": "Menéndez brothers",
        "https://en.wikipedia.org/wiki/Capitoline_Geese": "Capitoline Geese",
        "https://en.wikipedia.org/wiki/Rudolf_H%C3%B6%C3%9F": "Rudolf Höss",
        "https://en.wikipedia.org/wiki/2010_Smolensk_air_disaster": "Smolensk air disaster",
        "https://en.wikipedia.org/wiki/2011_T%C5%8Dhoku_earthquake_and_tsunami": "2011 Tōhoku earthquake and tsunami",
        "https://en.wikipedia.org/wiki/A_Lover%27s_Discourse:_Fragments": "A Lover's Discourse",
        "https://en.wikipedia.org/wiki/Barlaam_and_Iosaphat": "Barlaam and Josaphat",
        "https://en.wikipedia.org/wiki/Accabadora": "Accabadora (novel)",
        "https://en.wikipedia.org/wiki/Georg_G%C3%A4nswein": "Georg Gänswein",
        "https://en.wikipedia.org/wiki/J%C5%ABrat%C4%97_and_Kastytis": "Jūratė and Kastytis",
        "https://en.wikipedia.org/wiki/Alcestis_(Euripides)": "Alcestis (play)",
        "https://en.wikipedia.org/wiki/Gian_Antonio_Cibotto": "Gian Antonio Cibotto",
        "https://en.wikipedia.org/wiki/Myricae": "Myricae",
        "https://en.wikipedia.org/wiki/Stanis%C5%82aw_Lem": "Stanisław Lem",
        "https://en.wikipedia.org/wiki/On_Literature_(Eco)": "On Literature (essay collection)",
        "https://it.wikipedia.org/wiki/L%27armata_Brancaleone": "L'armata Brancaleone",
        "https://it.wikipedia.org/wiki/L%27eclisse": "L'eclisse",
        "https://en.wikipedia.org/wiki/Roman_Pola%C5%84ski": "Roman Polański",
        "https://it.wikipedia.org/wiki/Belzeb%C3%B9": "Belzebù",
        "https://en.wikipedia.org/wiki/Colombe_d%27Or": "Colombe d'Or",
        "https://en.wikipedia.org/wiki/Ne_sutor_ultra_crepidam": "Sutor, ne ultra crepidam",
        "https://en.wikipedia.org/wiki/Testa_di_moro": "Testa di moro",
        "https://en.wikipedia.org/wiki/Christie%27s": "Christie's",
        "https://en.wikipedia.org/wiki/Sotheby%27s": "Sotheby's",
    }
    
    for cat in categories:
        for entry in cat["entries"]:
            url = entry["url"]
            processed += 1
            
            # Check if we have a manual fix for this URL
            if url in manual_fixes:
                # Create a corrected URL
                lang, _ = wiki_lang_and_title(url)
                corrected_title = manual_fixes[url]
                corrected_url = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(corrected_title)}"
                print(f"  🔧 Manual fix for {url} -> {corrected_url}")
                entry["url"] = corrected_url
                url = corrected_url
            
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
                
            except requests.exceptions.HTTPError as e:
                # 404 errors - log but don't count as fatal
                if "404" in str(e):
                    print(f"  ⚠ Page not found: {url}")
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
                else:
                    raise
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
    print(f"  🔍 {stats['found_via_search']} found via search")
    print(f"  📦 {stats['from_cache']} articles from cache")
    print(f"  ⚠ {stats['failed']} articles failed (404/403)")
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
