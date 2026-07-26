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
OUTPUT_DIR = ROOT / "docs"

USER_AGENT = "KnowledgeArchive/1.0 (https://github.com/emachiamu/knowledge-archive)"
SITE_TITLE = "Knowledge Archive"

CATEGORY_RE = re.compile(r"^(.+?)\s*((?:\[\[[a-z0-9\- ]+\]\]\s*,?\s*)+)$")
TAG_RE = re.compile(r"\[\[([a-z0-9\- ]+)\]\]")
ENTRY_RE = re.compile(r"^(.+?):\s*(https?://\S+)\s*$")

CACHE_SAVE_INTERVAL = 25
MAX_RETRIES = 3
BASE_REQUEST_DELAY = 0.5


# ---------------------------------------------------------------------------
# 1. Parse the markdown archive
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def repair_url(url: str) -> str:
    if "wikipedia.org/wiki/" in url:
        return url
    m = re.match(r"^https?://([a-z]{2,3})\.wikipediaorgwiki(.+)$", url)
    if m:
        lang, rest = m.groups()
        return f"https://{lang}.wikipedia.org/wiki/{rest}"
    return url


def parse_archive(md_path: Path):
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
# 2. Fetch Wikipedia metadata (properly handling URL encoding)
# ---------------------------------------------------------------------------

def wiki_lang_and_title(url: str) -> Tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    lang = parsed.netloc.split(".")[0]
    page_title = parsed.path.rsplit("/wiki/", 1)[-1]
    # Decode the title once to get the actual page title
    page_title = urllib.parse.unquote(page_title)
    return lang, page_title


def get_wikipedia_summary(lang: str, title: str, session: requests.Session) -> Optional[Dict[str, Any]]:
    """
    Fetch Wikipedia summary for a given language and title.
    Uses the REST API with properly encoded URLs.
    """
    # The REST API expects the title in the URL path, properly URL-encoded
    # But we need to be careful not to double-encode
    encoded_title = urllib.parse.quote(title, safe="")
    api_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded_title}"
    
    try:
        resp = session.get(api_url, timeout=10)
        
        if resp.status_code == 404:
            # Try with spaces instead of underscores
            alt_title = title.replace("_", " ")
            if alt_title != title:
                encoded_alt = urllib.parse.quote(alt_title, safe="")
                alt_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded_alt}"
                resp_alt = session.get(alt_url, timeout=10)
                if resp_alt.status_code == 200:
                    return resp_alt.json()
            return None
        
        if resp.status_code == 403:
            # Try with a different encoding approach
            # Sometimes the API rejects certain characters, try without them
            alt_title = re.sub(r'[^\w\s\-\.]', '', title)
            if alt_title != title:
                encoded_alt = urllib.parse.quote(alt_title, safe="")
                alt_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded_alt}"
                resp_alt = session.get(alt_url, timeout=10)
                if resp_alt.status_code == 200:
                    return resp_alt.json()
            return None
        
        resp.raise_for_status()
        return resp.json()
        
    except requests.exceptions.RequestException:
        return None


def resolve_wikipedia_title(lang: str, title: str, session: requests.Session) -> Optional[str]:
    """
    Use the MediaWiki API to resolve a title to its canonical form.
    This handles redirects and finds the correct page.
    """
    # Try the title as-is first (with underscores)
    titles_to_try = [
        title,
        title.replace("_", " "),
        re.sub(r'\s*\([^)]*\)\s*$', '', title),  # Remove parenthetical suffix
        re.sub(r'\s*\([^)]*\)\s*$', '', title).replace("_", " "),
    ]
    
    # Remove duplicates
    seen = set()
    titles_to_try = [t for t in titles_to_try if t and not (t in seen or seen.add(t))]
    
    for t in titles_to_try:
        query_url = (
            f"https://{lang}.wikipedia.org/w/api.php"
            f"?action=query"
            f"&titles={urllib.parse.quote(t)}"
            "&redirects=1"
            "&format=json"
        )
        try:
            resp = session.get(query_url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            pages = data.get("query", {}).get("pages", {})
            
            for page_id, page in pages.items():
                if page_id != "-1" and "missing" not in page:
                    return page.get("title")
        except Exception:
            continue
    
    return None


def fetch_with_retry(url: str, session: requests.Session) -> Dict[str, Any]:
    """
    Fetch Wikipedia metadata with retry logic and proper error handling.
    """
    lang, page_title = wiki_lang_and_title(url)
    
    # First, resolve any redirects
    resolved_title = resolve_wikipedia_title(lang, page_title, session)
    if resolved_title:
        page_title = resolved_title
    
    # Try to fetch the summary
    for attempt in range(MAX_RETRIES + 1):
        data = get_wikipedia_summary(lang, page_title, session)
        
        if data:
            thumb = data.get("thumbnail") or data.get("originalimage") or {}
            return {
                "title": data.get("title", page_title),
                "extract": data.get("extract", ""),
                "image": thumb.get("source"),
                "image_width": thumb.get("width"),
                "image_height": thumb.get("height"),
                "wiki_url": data.get("content_urls", {}).get("desktop", {}).get("page", url),
                "lang": lang,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        
        # If we got a 404, the page doesn't exist
        if attempt < MAX_RETRIES:
            wait_time = (2 ** attempt) + random.uniform(0.1, 0.5)
            time.sleep(wait_time)
    
    # If all attempts failed, raise an exception
    raise requests.exceptions.HTTPError(f"Failed to fetch {page_title} after {MAX_RETRIES} attempts")


def create_robust_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
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
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = CACHE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8"
    )
    temp_file.replace(CACHE_FILE)


def enrich_categories(categories: List[Dict], refresh: bool = False, offline: bool = False) -> List[Dict]:
    cache = load_cache()
    session = create_robust_session()
    
    stats = {
        "fetched": 0,
        "from_cache": 0,
        "failed": 0,
        "redirected": 0,
    }
    
    # Manual fixes for known problematic titles
    manual_fixes = {
        "King%27s_College_Chapel,_Cambridge": "King's College Chapel, Cambridge",
        "Lion_Gate,_Mycenae": "Lion Gate",
        "Massimiliano_Locatelli": "Massimiliano Locatelli",
        "Fogong_Temple_Wooden_Pagoda": "Fogong Temple Pagoda",
        "Doge%27s_Palace": "Doge's Palace",
        "Rurik%C5%8D-ji": "Rurikō-ji",
        "Sir_John_Soane%27s_Museum": "Sir John Soane's Museum",
        "Speakers%27_Corner": "Speakers' Corner",
        "St._Mary%27s_Cathedral,_Tokyo": "St. Mary's Cathedral, Tokyo",
        "Trajan%27s_Kiosk": "Trajan's Kiosk",
        "Arnold_B%C3%B6cklin": "Arnold Böcklin",
        "Jupiter_and_Semele_(Moreau)": "Jupiter and Semele",
        "Artist%27s_Shit": "Artist's Shit",
        "Banditaccia_necropolis": "Banditaccia Necropolis",
        "Trompe-l%27%C5%93il": "Trompe-l'œil",
        "Faberg%C3%A9_egg": "Fabergé egg",
        "Aberration-corrected_electron_microscope": "Aberration-corrected transmission electron microscopy",
        "Lempel%E2%80%93Ziv%E2%80%93Markov_chain_algorithm": "Lempel–Ziv–Markov chain algorithm",
        "Curva_di_B%C3%A9zier": "Curva di Bézier",
        "Pascal%27s_calculator": "Pascal's calculator",
        "Carl_Friedrich_von_Weizs%C3%A4cker": "Carl Friedrich von Weizsäcker",
        "Maxwell%27s_demon": "Maxwell's demon",
        "Elitzur%E2%80%93Vaidman_bomb_tester": "Elitzur–Vaidman bomb tester",
        "Gruppo_di_Poincar%C3%A9": "Gruppo di Poincaré",
        "Hilbert%27s_problems": "Hilbert's problems",
        "Mach%E2%80%93Zehnder_interferometer": "Mach–Zehnder interferometer",
        "Moir%C3%A9_pattern": "Moiré pattern",
        "Landauer%27s_principle": "Landauer's principle",
        "Sol%C3%A8r%27s_theorem": "Solèr's theorem",
        "Bell%27s_theorem": "Bell's theorem",
        "Teorema_di_Rouch%C3%A9-Capelli": "Teorema di Rouché-Capelli",
        "Assassination_attempt_on_Pope_John_Paul_II": "Attempted assassination of Pope John Paul II",
        "Government_of_the_Nine": "The Nine",
        "Hermann_G%C3%B6ring": "Hermann Göring",
        "Lorenzo_de%27_Medici": "Lorenzo de' Medici",
        "Men%C3%A9ndez_brothers": "Menéndez brothers",
        "Capitoline_Geese": "Capitoline Geese",
        "Rudolf_H%C3%B6%C3%9F": "Rudolf Höss",
        "2010_Smolensk_air_disaster": "Smolensk air disaster",
        "2011_T%C5%8Dhoku_earthquake_and_tsunami": "2011 Tōhoku earthquake and tsunami",
        "A_Lover%27s_Discourse:_Fragments": "A Lover's Discourse",
        "Barlaam_and_Iosaphat": "Barlaam and Josaphat",
        "Accabadora": "Accabadora",
        "Georg_G%C3%A4nswein": "Georg Gänswein",
        "J%C5%ABrat%C4%97_and_Kastytis": "Jūratė and Kastytis",
        "Alcestis_(Euripides)": "Alcestis",
        "Gian_Antonio_Cibotto": "Gian Antonio Cibotto",
        "Myricae": "Myricae",
        "Stanis%C5%82aw_Lem": "Stanisław Lem",
        "On_Literature_(Eco)": "On Literature",
        "L%27armata_Brancaleone": "L'armata Brancaleone",
        "L%27eclisse": "L'eclisse",
        "Roman_Pola%C5%84ski": "Roman Polanski",
        "Belzeb%C3%B9": "Belzebù",
        "Colombe_d%27Or": "Colombe d'Or",
        "Ne_sutor_ultra_crepidam": "Sutor, ne ultra crepidam",
        "Testa_di_moro": "Testa di moro",
        "Christie%27s": "Christie's",
        "Sotheby%27s": "Sotheby's",
    }
    
    for cat in categories:
        for entry in cat["entries"]:
            url = entry["url"]
            
            # Extract the page title from the URL and decode it
            _, page_title = wiki_lang_and_title(url)
            
            # Check if we have a manual fix for this title
            if page_title in manual_fixes:
                corrected = manual_fixes[page_title]
                if corrected != page_title:
                    print(f"  🔧 Manual fix: {page_title} -> {corrected}")
                    # Reconstruct the URL with the corrected title
                    lang, _ = wiki_lang_and_title(url)
                    encoded_corrected = urllib.parse.quote(corrected)
                    url = f"https://{lang}.wikipedia.org/wiki/{encoded_corrected}"
                    entry["url"] = url
                    page_title = corrected
            
            # Check cache
            cached = cache.get(url)
            if cached and not refresh:
                entry.update(cached)
                stats["from_cache"] += 1
                continue
            
            if offline:
                lang, _ = wiki_lang_and_title(url)
                entry.update({
                    "title": page_title,
                    "extract": "",
                    "image": None,
                    "wiki_url": url,
                    "lang": lang,
                    "fetched_at": None,
                })
                stats["from_cache"] += 1
                continue
            
            # Fetch from Wikipedia
            try:
                meta = fetch_with_retry(url, session)
                
                # Check if the actual title is different from what we expected
                if meta.get("title") and meta["title"] != page_title:
                    stats["redirected"] += 1
                    print(f"  ↪ '{page_title}' -> '{meta['title']}'")
                
                cache[url] = meta
                entry.update(meta)
                stats["fetched"] += 1
                print(f"  ✓ {meta['title']} (fetched: {stats['fetched']}, cached: {stats['from_cache']}, failed: {stats['failed']})")
                
            except Exception as exc:
                print(f"  ! Failed: {page_title} ({str(exc)[:50]})")
                stats["failed"] += 1
                # Use cache if available, otherwise create placeholder
                fallback = cache.get(url) or {
                    "title": page_title,
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
            
            # Polite delay
            if stats["fetched"] > 0:
                time.sleep(BASE_REQUEST_DELAY + random.uniform(0.1, 0.3))
    
    # Final cache save
    if not offline:
        save_cache(cache)
    
    print(f"\n📊 Wikipedia metadata summary:")
    print(f"  ✅ {stats['fetched']} new articles fetched")
    print(f"  🔄 {stats['redirected']} redirects resolved")
    print(f"  💾 {stats['from_cache']} articles from cache")
    print(f"  ❌ {stats['failed']} articles failed")
    print(f"  📚 {len(cache)} total entries in cache")
    
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

    for i, cat in enumerate(categories, start=1):
        cat["call_number"] = f"{i:03d}"

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

    # Category pages
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

    # Search page
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

    # Search index
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

    # .nojekyll
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    print(f"\n🏗️  Built {len(categories)} category pages + homepage + search page "
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
    print("📚 Knowledge Archive Build")
    print("=" * 60)
    
    categories = parse_archive(CONTENT_MD)
    total = sum(len(c["entries"]) for c in categories)
    print(f"📄 Parsed {len(categories)} categories, {total} articles from {CONTENT_MD.name}")

    categories = enrich_categories(categories, refresh=args.refresh, offline=args.offline)
    build_site(categories)
    
    print("\n" + "=" * 60)
    print("✅ Build complete!")


if __name__ == "__main__":
    main()
