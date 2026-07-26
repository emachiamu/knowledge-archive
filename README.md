# Knowledge Archive

A static "digital library" website generated from `content/archive.md` — your
plain list of Wikipedia links, grouped by subject. The build script fetches
each article's title, image, and intro paragraph from Wikipedia, caches the
result, and renders a minimal, fast, accessible site you can host for free on
GitHub Pages.

## Why a plain Python build script, not Jekyll/Eleventy/Hugo/Astro

You asked me to recommend the best fit and explain why. I chose a small,
dependency-light **Python script** (`scripts/build.py`) over a general-purpose
static site generator for one reason: the actual hard part of this project
isn't templating, it's **talking to the Wikipedia API and caching the
results** so a rebuild doesn't refetch 500 pages every time. That logic has
to be written in *some* language regardless of which SSG you pick — Jekyll
would need a Ruby plugin, Eleventy a JS fetch script, Hugo a Go module or an
external data step. Python's standard tooling (`requests`, `json`) makes that
part about 100 lines of very readable code, and Jinja2 (the templating engine
FYI already used by Jekyll's spiritual cousins and very close to Liquid/Nunjucks)
covers the templating half for free.

The result: **one script, one command, one output folder.** No Node/Ruby/Go
toolchain to install or keep updated, no plugin ecosystem to learn — just
Python + two libraries. For a personal archive that one person maintains by
editing a text file a few times a month, that's the lowest-maintenance option
and the easiest to still understand in five years.

If you already live in the Node ecosystem and want a "real" SSG with live
reload, etc., Eleventy is the next-best fit (plain JS/Nunjucks, minimal
opinions) — the `scripts/build.py` fetch/cache logic would port over almost
unchanged as an Eleventy "global data" file.

## How it works

```
content/archive.md   → the only file you normally edit
        │
        ▼
scripts/build.py      1. parses categories + links from the markdown
        │              2. fetches {title, image, intro} per link from the
        │                 Wikipedia REST API — only for links not already
        │                 in data/cache.json
        │              3. renders templates/*.html with Jinja2
        ▼
docs/                  the finished static site (this is what GitHub Pages serves)
```

`data/cache.json` is committed to the repo. It means:

- A rebuild is fast and doesn't hammer Wikipedia's API — only *new* links get
  fetched.
- The site still builds even if Wikipedia is briefly unreachable.
- You get a natural "date added" per article for the homepage's *Recently
  added* section, for free.

## Project structure

```
content/archive.md         your source of truth — one link per line, grouped
                            under CATEGORY NAME [[tag]] headers
scripts/build.py           the entire build pipeline
scripts/requirements.txt   pip dependencies (requests, Jinja2)
templates/                 Jinja2 HTML templates
static/style.css           all styling — one file, CSS custom properties for
                            light/dark theming, no framework
static/app.js               ~100 lines of vanilla JS: theme toggle, search,
                            random-article button
data/cache.json             cached Wikipedia metadata (committed)
docs/                       generated output — what GitHub Pages serves
.github/workflows/deploy.yml   rebuilds + deploys automatically
```

## Editing the archive

Keep using the same format you already have:

```
CATEGORY NAME [[some-tag]]
Article display name: https://en.wikipedia.org/wiki/Article_Title
Another article: https://it.wikipedia.org/wiki/Un_Altro_Articolo
```

- A new category = a new `NAME [[tag]]` line (Italian or English Wikipedia
  both work; the language is detected from the URL's subdomain).
- A new article = one `Title: URL` line under the relevant category.
- Order in the file = display order on the site (and drives the previous
  category / next category links and the catalog numbers).

Commit the change and push. GitHub Actions rebuilds the site and redeploys
automatically — you never touch `docs/` by hand.

## Running it yourself

```bash
cd knowledge-archive
pip install -r scripts/requirements.txt
python scripts/build.py            # fetches only new links, then builds docs/
```

Useful flags:

- `python scripts/build.py --refresh` — ignore the cache and re-fetch every
  article (e.g. to pick up a Wikipedia edit or a newer photo).
- `python scripts/build.py --offline` — never touch the network; anything not
  already cached is rendered with a placeholder card instead of failing the
  build. Handy for quick local template tweaks.

Preview locally:

```bash
python -m http.server --directory docs 8000
# open http://localhost:8000
```

## Deploying on GitHub Pages

1. Push this repo to GitHub.
2. In **Settings → Pages**, set the source to **GitHub Actions**.
3. The included workflow (`.github/workflows/deploy.yml`) builds the site and
   deploys it on every push to `main` that touches `content/archive.md`,
   `scripts/`, `templates/`, or `static/`. It also runs weekly, so if an
   article's Wikipedia thumbnail changes, it's picked up automatically. You
   can also trigger it manually from the Actions tab.
4. The workflow commits the refreshed `data/cache.json` back to the repo
   after each run, so your local copy and GitHub stay in sync — just `git
   pull` before your next local build.

No other setup, no secrets, no backend.

## Design notes

The visual language is a quiet "reading room / card catalog": a paper
background, a single pine-green accent, and a brass-toned monospace label
used only for the catalog numbers assigned to each category (in the order
they appear in your markdown file) — a nod to library classification codes,
and a genuine way to always know "where you are" in the archive. No
animation beyond instant hover states; motion is skipped for anyone with
`prefers-reduced-motion`, and full keyboard focus outlines are kept visible
throughout.

## Features included

- Homepage catalog of all categories with counts and a 3-title preview
- One page per category, article cards (image · title · intro · source link)
- Client-side search across all article titles/intros, with a category filter
- Previous/next category navigation, breadcrumbs on every page
- Random-article button
- "Recently added" list on the homepage (based on when a link was first
  fetched, not when the underlying Wikipedia article was written)
- Dark/light theme toggle (respects system preference, remembered locally)
- Fully static, no backend, ~30 KB of CSS+JS total, no build framework runtime

## Not included (deliberately)

Reading-progress bars, tag clouds, and other extras from the optional list
were left out — this is a list of link *cards*, not long-form articles, so a
progress indicator has nothing meaningful to track. Adding more than the
above risked the "overengineered" trap the brief warned against; everything
here earns its place for browsing efficiency.
