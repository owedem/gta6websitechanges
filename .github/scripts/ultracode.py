#!/usr/bin/env python3
"""
GTA Bot Ultracode — the single GTA VI monitor
=============================================

One tool that watches the official GTA VI site from every angle and explains any
meaningful change in plain English (free AI: Groq, then Gemini, then Claude).

It folds in everything the old monitor.yml / scan.yml / probe.yml did:

  Per page (home, only-in-leonida, media, editions), every run (~5 min):
    - text     : visible page copy                                  [alerts]
    - head     : <title>, meta description, OG/Twitter share tags    [alerts]
    - routes   : /VI/* nav links                                     [alerts]
    - media    : /_next/static/media image names (new screenshots)   [alerts]
    - chunks   : JS/CSS bundle filenames (rotate every deploy)       [context]
    - headers  : ETag etc. (deploy fingerprint)                      [context]
  Site-wide, every run:
    - media counts on /VI/media (new items)                          [alerts]
    - robots.txt / sitemap (GTA VI slice)                            [alerts]
  Heavier checks on their own cadence (gated by a timestamp in state):
    - code scan (~20 min): download JS chunk *contents*, grep for hidden
      routes, dates, pre-order keywords, API endpoints                [alerts]
    - URL probe (~2 hr): test candidate /VI/<path> URLs for pages going
      live before they are linked                                     [alerts]

Routine rebuilds (only chunk hashes / ETag rotate) update the snapshot silently.
A manual "Run workflow" forces the heavy checks too (full scan on demand).

Env:
  GROQ_API_KEY / GROQ_MODEL          - free AI (preferred)
  GEMINI_API_KEY / GEMINI_MODEL      - free AI (fallback)
  ANTHROPIC_API_KEY / ANALYSIS_MODEL - paid AI (last resort)
  DISCORD_WEBHOOK                    - where to post
  STATE_DIR / LOG_FILE               - snapshot dir / changelog
"""

import os
import re
import sys
import json
import time
import html as html_mod
import difflib
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

STATE_DIR = os.environ.get("STATE_DIR", "ultracode-state")
LOG_FILE = os.environ.get("LOG_FILE", "ultracode-log.md")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
# Dedicated channels for posting the actual new media assets (not just naming
# them in the main alert). One webhook per channel.
IMAGES_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_IMAGES", "").strip()
VIDEOS_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_VIDEOS", "").strip()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("ANALYSIS_MODEL", "claude-opus-4-8")

# An explicit "full scan" request (the workflow_dispatch `full_scan` input) forces
# the heavy checks regardless of their cadence timers. Automated triggers — the
# cron schedule, or an external pinger calling workflow_dispatch without inputs —
# leave this false and do a normal cadence-gated run, so frequent 5-min pings
# don't re-download every JS chunk each time.
FORCE_FULL = os.environ.get("FULL_SCAN", "").strip().lower() in ("1", "true", "yes")

# Mention prepended to MAJOR alerts (new page, new route, launch keyword, sitemap)
# so they can't be missed. e.g. "@everyone" or "<@&ROLE_ID>". Empty = no ping.
MAJOR_PING = os.environ.get("MAJOR_PING", "").strip()
# Periodic "still alive" heartbeat so silence never hides a dead bot.
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_HOURS", "24")) * 3600

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BASE = "https://www.rockstargames.com"

PAGES = [
    ("home", f"{BASE}/VI/"),
    ("only-in-leonida", f"{BASE}/VI/only-in-leonida"),
    ("media", f"{BASE}/VI/media"),
    ("editions", f"{BASE}/VI/editions"),
]
RAW = [
    ("robots", f"{BASE}/robots.txt", None),
    ("sitemap", f"{BASE}/sitemap.xml", "/vi"),
]

# Heavy-check cadences (seconds). Slightly under the nominal interval so the
# ~5-min ticks reliably cross the threshold despite scheduler jitter.
CODESCAN_INTERVAL = 19 * 60   # ~20 min: download JS chunks and grep for clues
PROBE_INTERVAL = 28 * 60      # ~30 min: probe candidate URLs for new live pages

# Secondary pages (characters/locations) watched EVERY run for text/head/route
# changes and new media, same cadence as the main pages (cheap fetches).
# Non-existent ones are skipped automatically.
EXTRA_PAGE_PATHS = [
    "jason", "lucia", "ambrosia", "boobie", "brian", "cal", "drequan", "raul",
    "dimez", "vice-city", "port-gellhorn", "leonida-keys", "grassrivers",
    "kalaga",
]
# A new code finding mentioning one of these is a MAJOR signal (pre-order is
# excluded — it is already live and would be noise).
MAJOR_KEYWORDS = ("countdown", "releasedate", "outnow", "launchday", "comingsoon",
                  "worldpremiere", "gameplayreveal", "availablenow", "nowavailable",
                  "wishlist")

# Candidate paths the URL probe brute-forces (ported from probe.yml).
PROBE_PATHS = [
    "story", "gameplay", "features", "world", "map", "characters", "factions",
    "weapons", "vehicles", "radio", "music", "soundtrack",
    "online", "gta-online", "multiplayer",
    "buy", "pre-order", "collector", "special-edition", "day-one",
    "ultimate-edition", "standard-edition",
    "pc", "requirements", "accessibility",
    "news", "newswire", "support", "faq",
    "heist", "score", "prologue", "epilogue", "credits",
    "gta-plus", "interactive",
    "trailer", "trailers", "videos", "screenshots", "artwork", "wallpapers",
    "gameplay-trailer", "making-of", "behind-the-scenes",
    "leonida", "kelly-county", "launch", "release",
]
PROBE_SEED = [
    "only-in-leonida", "media", "editions", "jason", "lucia", "ambrosia",
    "boobie", "brian", "cal", "drequan", "raul", "dimez", "vice-city",
    "port-gellhorn", "leonida-keys", "grassrivers", "kalaga", "downloads",
]

KEEP_HEADERS = {
    "etag", "last-modified", "content-length", "content-type",
    "cache-control", "x-nextjs-cache", "x-nextjs-prerender",
}

ASSET_RE = re.compile(
    r"/_next/static/[A-Za-z0-9_.~/\-]+"
    r"\.(?:js|css|woff2?|json|png|jpe?g|webp|svg|avif|gif|ico|mp4|webm)"
)
# Full media asset filename (name + content-hash + ext) for images and videos.
# SVG/logos are excluded on purpose; the content hash is kept so we can fetch and
# post the actual file. (Next.js content-hashes these, so the hash is stable for
# unchanged content — a new filename means genuinely new/changed media.)
MEDIA_ASSET_RE = re.compile(
    r"/_next/static/media/([A-Za-z0-9_.~\-]+\.(?:jpg|jpeg|png|webp|avif|gif|mp4|webm|mov))"
)
VIDEO_EXT = (".mp4", ".webm", ".mov")
MEDIA_SKIP_NAMES = {"esrb", "vi", "t1", "t2"}  # tiny UI/logo assets, not content
CHUNK_RE = re.compile(r"/_next/static/chunks/(?!turbopack)[^\"?\s]+\.(?:js|css)")
CHUNK_PATH_RE = re.compile(r"_next/static/chunks/[^\"?\s]+\.js")

# Which per-page artifacts raise an alert vs. are recorded as context only.
TRIGGER_ARTIFACTS = {"text", "head", "routes"}
CONTEXT_ARTIFACTS = {"chunks", "headers"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def safe_print(s):
    try:
        sys.stdout.buffer.write((s + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()
    except Exception:
        sys.stdout.write(s.encode("ascii", "replace").decode("ascii") + "\n")


def fetch(url, retries=2):
    """Return (status, headers_dict, body_text) or None on failure."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read().decode("utf-8", "replace")
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, headers, body
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    sys.stderr.write(f"[warn] fetch failed for {url}: {last}\n")
    return None


def probe_url(url):
    """Return (status_code, size_bytes); status None on connection error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status, len(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception:
        return None, 0


# --------------------------------------------------------------------------- #
# State storage
# --------------------------------------------------------------------------- #


def state_path(*parts):
    return os.path.join(STATE_DIR, *parts)


def read_state(*parts):
    p = state_path(*parts)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return None


def write_state(content, *parts):
    p = state_path(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def last_run(name):
    v = read_state("_meta", name)
    try:
        return float(v.strip())
    except Exception:
        return 0.0


def mark_run(name):
    write_state(str(time.time()), "_meta", name)


def added_lines(old, new):
    o = set(old.splitlines()) if old else set()
    return sorted(set(new.splitlines()) - o)


def unified(old, new, label):
    return "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"{label} (before)", tofile=f"{label} (after)",
        lineterm="", n=2,
    ))


# --------------------------------------------------------------------------- #
# Per-page extraction
# --------------------------------------------------------------------------- #


def media_assets(html):
    """Map human name -> full asset path (with hash) for image/video media,
    skipping tiny UI/logo files. The name (part before the first dot) is the
    hash-independent identity; a new name = genuinely new media."""
    out = {}
    for fname in MEDIA_ASSET_RE.findall(html):
        name = fname.split(".")[0]
        if name.lower() in MEDIA_SKIP_NAMES:
            continue
        out[name] = f"/_next/static/media/{fname}"
    return out


def x_chunks(html):
    return "\n".join(sorted(set(CHUNK_RE.findall(html))))


def x_routes(html):
    return "\n".join(sorted(set(re.findall(r'href="(/VI/(?!_next/)[^"#?]*)"', html))))


def x_head(html):
    m = re.search(r"<head\b[^>]*>(.*?)</head>", html, re.S | re.I)
    head = m.group(1) if m else html
    lines = []
    t = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
    if t:
        lines.append("title: " + re.sub(r"\s+", " ", html_mod.unescape(t.group(1))).strip())
    for tag in re.findall(r"<meta\b[^>]*>", head, re.I):
        name = re.search(r'(?:name|property)\s*=\s*["\']([^"\']+)', tag, re.I)
        content = re.search(r'content\s*=\s*["\']([^"\']*)', tag, re.I)
        if name and content:
            val = re.sub(r"\s+", " ", html_mod.unescape(content.group(1))).strip()
            lines.append(f"{name.group(1)}: {val}")
    for link in re.findall(r"<link\b[^>]*>", head, re.I):
        if re.search(r'rel=["\']canonical["\']', link, re.I):
            href = re.search(r'href=["\']([^"\']+)', link, re.I)
            if href:
                lines.append("canonical: " + href.group(1))
    return "\n".join(sorted(set(lines)))


def x_text(html):
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    out = []
    for part in re.split(r"<[^>]+>", html):
        frag = re.sub(r"\s+", " ", html_mod.unescape(part)).strip()
        if frag:
            out.append(frag)
    return "\n".join(out)


def x_headers(headers):
    return "\n".join(f"{k}: {headers[k]}" for k in sorted(headers) if k in KEEP_HEADERS)


def page_artifacts(html, headers):
    return {
        "text": x_text(html),
        "head": x_head(html),
        "routes": x_routes(html),
        "chunks": x_chunks(html),
        "headers": x_headers(headers),
    }


def media_counts(html):
    """Extract '<Label> <count>' pairs from the media page (ported from monitor.yml)."""
    pairs, seen = [], set()
    for text, count in re.findall(
        r'>([A-Z][^<>"]{1,60}?)\s*<(?:div|span)[^>]+aria-hidden=["\']true["\']>\s*(\d+)\s*</',
        html,
    ):
        text = html_mod.unescape(text.strip())
        if 2 < len(text) < 60:
            key = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
            if key and key not in seen:
                seen.add(key)
                pairs.append(f"{key}={count}")
    return "\n".join(pairs)


# --------------------------------------------------------------------------- #
# Heavy checks
# --------------------------------------------------------------------------- #

KEYWORD_EXCLUDE = re.compile(
    r"pointerCapture|releaseProxy|releaseLock|onRelease|releasePointer|"
    r"unlockAudioContext|launchpad|launcher", re.I)

# Distinctive launch-signal terms captured as full, stable identifiers (e.g.
# "PreorderDrawerContent", "CountdownTimer"). Deliberately specific — generic
# words like "release"/"launch"/"available" match too much minified noise.
KEYWORD_RE = re.compile(
    r"[A-Za-z0-9_$]*"
    r"(?:preorder|wishlist|countdown|comingsoon|releasedate|launchday|outnow"
    r"|nowavailable|availablenow|gameplayreveal|worldpremiere)"
    r"[A-Za-z0-9_$]*", re.I)


def code_scan():
    """Download JS chunk contents and grep for clues (ported from scan.yml).
    Returns a sorted list of 'category: finding' strings."""
    all_html = ""
    for _, url in PAGES:
        r = fetch(url)
        if r:
            all_html += r[2]
    chunk_paths = sorted(set(CHUNK_PATH_RE.findall(all_html)))[:50]
    blobs = []
    for cp in chunk_paths:
        r = fetch(f"{BASE}/VI/{cp}")
        if r:
            blobs.append(r[2])
    code = "\n".join(blobs)

    findings = set()
    for route in re.findall(r'"(/VI/[a-zA-Z0-9_\-/]+)"', code):
        if "_next" not in route:
            findings.add(f"route: {route}")
    for pat in (r"202[456789]-\d{2}-\d{2}", r"202[456789]/\d{2}/\d{2}",
                r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* 202[456789]",
                r"\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* 202[456789]"):
        for d in re.findall(pat, code):
            findings.add(f"date: {d}")
    # Capture stable identifiers containing "preorder" (e.g. "PreorderDrawerContent"),
    # NOT 50-char minified code windows. Windows shift on every rebuild and caused
    # false "new pre-order feature" alerts even when nothing actually changed.
    kw = set()
    for tok in KEYWORD_RE.findall(code):
        if not KEYWORD_EXCLUDE.search(tok):
            kw.add(tok)
    for tok in re.findall(r"pre[-_]order", code, re.I):
        kw.add(tok.lower())
    for tok in sorted(kw):
        findings.add(f"keyword: {tok}")
    for api in re.findall(r"https?://[^\"'\s]+rockstar[^\"'\s]+/VI/[^\"'\s]*", code):
        findings.add(f"api: {api}")
    return sorted(findings)


def url_probe(known):
    """Probe candidate paths for new live pages (ported from probe.yml).
    Returns (new_paths, updated_known_set)."""
    _, home_size = probe_url(f"{BASE}/VI/")
    _, oil_size = probe_url(f"{BASE}/VI/only-in-leonida")
    new = []
    for path in PROBE_PATHS:
        if path in known:
            continue
        status, size = probe_url(f"{BASE}/VI/{path}")
        if status == 200 and abs(home_size - size) > 10000 and abs(oil_size - size) > 10000:
            new.append(path)
            known.add(path)
    return new, known


# --------------------------------------------------------------------------- #
# AI analysis
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are a forensic analyst for a community that tracks the official Grand Theft \
Auto VI website (rockstargames.com/VI) for the earliest possible signs of news: \
a release date, pre-orders going live, new trailers/screenshots, or new pages.

You are given the CHANGES found in one scan of the site, grouped by source. \
Possible groups:
  - PAGE TEXT / HEAD (share tags) / NAV ROUTES: what changed on the visible \
pages. The og:image / twitter:image and title are often updated just before \
content goes live.
  - NEW MEDIA IMAGES: new image asset names (likely new screenshots/art).
  - MEDIA COUNTS: item counts on the media page went up.
  - CLUES IN SITE CODE: strings found inside the JS bundles that are not visible \
on any page yet — hidden routes, hardcoded dates, pre-order keywords, API URLs.
  - NEW PAGES LIVE: a /VI/<path> URL that used to 404 now returns a real page, \
before it is linked anywhere.
  - ROBOTS / SITEMAP: changes to robots.txt or the sitemap.

Write a Discord post for a NON-TECHNICAL audience of GTA fans. Structure:

**<emoji> One-line headline**

**What changed:**
- 2-8 short bullets in plain English. Translate each finding into what a fan \
would care about. Quote specific new text, dates, image names, routes, or pages. \
Group related findings. Skip pure noise.

**What it likely means:**
- 1-3 sentences.

**Could signal:** _(confidence: low | medium | high)_
- Cautious, clearly-hedged speculation about what might be coming and a rough \
timeframe if inferable.

Rules: be precise and grounded strictly in the findings — never invent dates, \
names, or details not present. A hidden date or a new "/VI/buy" route is a strong \
signal; asset-hash churn is noise. If nothing here is actually meaningful, say so \
briefly. Never present speculation as fact.\
"""

LAST_AI_ERROR = None


def analyse(bundle):
    if GROQ_API_KEY:
        return _groq(bundle)
    if GEMINI_API_KEY:
        return _gemini(bundle)
    if ANTHROPIC_API_KEY:
        return _claude(bundle)
    sys.stderr.write("[info] No LLM key set — skipping AI analysis.\n")
    return None


def _http_error_reason(e):
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        pass
    try:
        return json.loads(body).get("error", {}).get("message", "") or body[:240]
    except Exception:
        return body[:240]


def _groq(bundle):
    global LAST_AI_ERROR
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": bundle}],
        "temperature": 0.7, "max_tokens": 1500,
    }
    try:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                     "Authorization": f"Bearer {GROQ_API_KEY}"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        LAST_AI_ERROR = f"HTTP {e.code} — {_http_error_reason(e)}"
        sys.stderr.write(f"[warn] Groq: {LAST_AI_ERROR}\n")
        return None
    except Exception as e:
        LAST_AI_ERROR = f"request failed — {e}"
        sys.stderr.write(f"[warn] Groq: {LAST_AI_ERROR}\n")
        return None
    try:
        return (data["choices"][0]["message"]["content"].strip() or None)
    except Exception:
        LAST_AI_ERROR = f"unexpected response: {json.dumps(data)[:200]}"
        return None


def _gemini(bundle):
    global LAST_AI_ERROR
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": bundle}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1500},
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        LAST_AI_ERROR = f"HTTP {e.code} — {_http_error_reason(e)}"
        sys.stderr.write(f"[warn] Gemini: {LAST_AI_ERROR}\n")
        return None
    except Exception as e:
        LAST_AI_ERROR = f"request failed — {e}"
        sys.stderr.write(f"[warn] Gemini: {LAST_AI_ERROR}\n")
        return None
    cands = data.get("candidates") or []
    if not cands:
        LAST_AI_ERROR = f"no candidates ({json.dumps(data.get('promptFeedback', {}))[:160]})"
        return None
    parts = cands[0].get("content", {}).get("parts", [])
    return ("".join(p.get("text", "") for p in parts).strip() or None)


def _claude(bundle):
    global LAST_AI_ERROR
    try:
        import anthropic
    except Exception as e:
        LAST_AI_ERROR = f"anthropic SDK unavailable: {e}"
        return None
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=4000,
            thinking={"type": "adaptive"}, output_config={"effort": "medium"},
            system=SYSTEM_PROMPT, messages=[{"role": "user", "content": bundle}])
        if resp.stop_reason == "refusal":
            LAST_AI_ERROR = "Claude refused"
            return None
        return ("".join(b.text for b in resp.content if b.type == "text").strip() or None)
    except Exception as e:
        LAST_AI_ERROR = f"Claude failed — {e}"
        return None


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #


# Map a media filename to a friendly category tag (first substring match wins).
CATEGORY_RULES = [
    ("ultimate_edition", "Ultimate Edition"),
    ("ultimateedition", "Ultimate Edition"),
    ("standard_edition", "Standard Edition"),
    ("standardedition", "Standard Edition"),
    ("vintage_vice_city", "Vintage Vice City Pack"),
    ("postcard", "Postcard"),
    ("video_clip", "Video Clip"),
    ("gtavi_fob", "Box Art"),
]


def category_match(name):
    low = name.lower()
    for key, label in CATEGORY_RULES:
        if key in low:
            return label
    return None


# Boilerplate tokens to drop from the displayed name (build/locale noise).
_DROP_TOKENS = re.compile(
    r"\b(gtavi|fob|desktop|mobile|en us|fr fr|ja jp|ko kr|zh hans|zh tw|"
    r"de de|es es|pt br|it it)\b", re.I)


def pretty_name(name):
    s = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    s = re.sub(r"\s+", " ", _DROP_TOKENS.sub("", s)).strip()
    return s.title() or name


def media_label(name):
    """A '[Category] Pretty Name' label; the category words are removed from the
    name to avoid repetition (e.g. '[Postcard] Vice City Landscape')."""
    cat = category_match(name)
    disp = pretty_name(name)
    if cat:
        disp = re.sub(r"\s+", " ", re.sub(re.escape(cat), "", disp, flags=re.I)).strip()
        return f"**[{cat}]** {disp}" if disp else f"**[{cat}]**"
    return disp


def post_media(name, full_url, is_video):
    """Post a new media asset to its dedicated channel. Images go as an image
    embed (Discord fetches the URL); videos go as a link (Discord renders a
    player, and large videos exceed upload limits anyway)."""
    webhook = VIDEOS_WEBHOOK if is_video else IMAGES_WEBHOOK
    if not webhook:
        return
    label = media_label(name)
    if is_video:
        payload = {"content": f"🎬 **New GTA VI video** — {label}\n{full_url}"}
    else:
        payload = {"content": f"🖼️ **New GTA VI image** — {label}",
                   "embeds": [{"image": {"url": full_url}}]}
    req = urllib.request.Request(
        webhook, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        sys.stderr.write(f"[warn] media post failed ({name}): {e}\n")
    time.sleep(1)  # stay under Discord's webhook rate limit


def post_discord(message, allow_mentions=False):
    if not DISCORD_WEBHOOK:
        sys.stderr.write("[info] DISCORD_WEBHOOK not set — printing instead:\n")
        safe_print(message)
        return
    # By default suppress all mentions (so stray text can't ping); only major
    # alerts opt in to actually pinging.
    am = {"parse": ["everyone", "roles", "users"]} if allow_mentions else {"parse": []}
    for chunk in chunk_text(message, 1900):
        payload = json.dumps({"content": chunk, "allowed_mentions": am}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            sys.stderr.write(f"[warn] Discord post failed: {e}\n")
        time.sleep(1)


def post_heartbeat(stamp):
    """Periodic 'still alive' message. Its absence is the dead-man's switch:
    if these stop arriving, something broke (token, pinger, workflow)."""
    etag = ""
    for line in (read_state("home", "headers.txt") or "").splitlines():
        if line.lower().startswith("etag:"):
            etag = line.split(":", 1)[1].strip()
    post_discord(
        f"✅ **Ultracode heartbeat — still watching** | {stamp}\n"
        f"No meaningful changes to report. Current site build (ETag): "
        f"`{etag or 'n/a'}`.\n"
        f"_(Periodic alive-check. If these stop arriving, the bot has stopped — "
        f"check the cron-job.org pinger and the GitHub token.)_")


def chunk_text(text, size):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size:
            if cur:
                chunks.append(cur)
            while len(line) > size:
                chunks.append(line[:size])
                line = line[size:]
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def append_log(stamp, sections, analysis):
    titles = ", ".join(t for t, _ in sections)
    body = f"\n## {stamp}\n\n**Found in:** {titles}\n\n"
    body += (analysis + "\n") if analysis else "_(AI analysis unavailable.)_\n"
    with open(LOG_FILE, "a", encoding="utf-8", newline="\n") as f:
        f.write(body)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def is_baseline():
    if not os.path.isdir(STATE_DIR):
        return True
    for _r, _d, files in os.walk(STATE_DIR):
        if any(f.endswith(".txt") for f in files):
            return False
    return True


def main():
    baseline = is_baseline()
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y at %H:%M UTC")

    # Sections that raise an alert: (priority, title, body, major). Lower prio first.
    sections = []
    context_changed = False

    def add(prio, title, body, major=False):
        if body and body.strip():
            sections.append((prio, title, body, major))

    # --- Per-page surface (every run) ---
    # Main pages get full artifacts (incl. chunks/headers deploy fingerprint);
    # the secondary character/location pages get the lighter text/head/routes set
    # (their chunks/headers would just duplicate the home page's deploy noise).
    media_html = None
    all_media = {}  # name -> full asset path, unioned across ALL pages
    all_pages = ([(s, u, (s,), True) for s, u in PAGES]
                 + [(p, f"{BASE}/VI/{p}", ("pages", p), False) for p in EXTRA_PAGE_PATHS])
    for surface, url, parts, full in all_pages:
        r = fetch(url)
        if r is None:
            continue
        status, headers, body = r
        if status != 200 or "<html" not in body.lower():
            continue
        if surface == "media":
            media_html = body
        all_media.update(media_assets(body))  # incl. character-page art
        arts = (page_artifacts(body, headers) if full else
                {"text": x_text(body), "head": x_head(body), "routes": x_routes(body)})
        for name, new in arts.items():
            old = read_state(*parts, f"{name}.txt")
            write_state(new, *parts, f"{name}.txt")
            if old is None or old == new:
                continue
            if name in TRIGGER_ARTIFACTS:
                is_major = name == "routes" and bool(added_lines(old, new))
                add(0 if name in ("head", "routes") else 1,
                    f"{surface} {name} changed", unified(old, new, f"{surface}/{name}"),
                    major=is_major)
            elif name in CONTEXT_ARTIFACTS:
                context_changed = True

    # --- Media counts (increase only) ---
    if media_html is not None:
        new_counts = media_counts(media_html)
        old_counts = read_state("media-counts.txt")
        write_state(new_counts, "media-counts.txt")
        if old_counts is not None and new_counts != old_counts:
            ov = dict(l.split("=", 1) for l in old_counts.splitlines() if "=" in l)
            ups = []
            for line in new_counts.splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                try:
                    if int(v) > int(ov.get(k, 0)):
                        ups.append(f"- {k.replace('_', ' ')}: {ov.get(k, 0)} -> {v}")
                except ValueError:
                    pass
            if ups:
                add(1, "media counts increased", "\n".join(ups))

    # --- New media assets: post the actual image/video to its channel ---
    # Fully isolated in try/except: this is an optional add-on, and a failure
    # here must NEVER take down the core site monitor / AI alert below.
    try:
        if all_media:
            cur = "\n".join(f"{n}|{p}" for n, p in sorted(all_media.items()))
            old_raw = read_state("media-assets.txt")
            write_state(cur, "media-assets.txt")
            if old_raw is not None:  # None = first run -> baseline silently
                old_names = {l.split("|", 1)[0] for l in old_raw.splitlines() if "|" in l}
                new_names = [n for n in sorted(all_media) if n not in old_names]
                imgs, vids = [], []
                for n in new_names[:40]:  # cap a runaway drop (rate limits / latency)
                    path = all_media[n]
                    is_video = path.lower().endswith(VIDEO_EXT)
                    post_media(n, f"{BASE}/VI{path}", is_video)
                    (vids if is_video else imgs).append(n)
                bits = []
                if imgs:
                    bits.append(f"{len(imgs)} new image(s) → images channel: "
                                + ", ".join(imgs[:12]))
                if vids:
                    bits.append(f"{len(vids)} new video(s) → videos channel: "
                                + ", ".join(vids[:12]))
                if len(new_names) > 40:
                    bits.append(f"(+{len(new_names) - 40} more new media not shown)")
                if bits:
                    add(2, "new media posted", "\n".join(f"- {b}" for b in bits))
    except Exception as e:
        sys.stderr.write(f"[warn] media posting failed (core monitor unaffected): {e}\n")

    # --- robots / sitemap ---
    for name, url, grep in RAW:
        r = fetch(url)
        if r is None:
            continue
        body = r[2]
        if grep:
            body = "\n".join(l for l in body.splitlines() if grep.lower() in l.lower())
        new = body.strip()
        old = read_state(name, "raw.txt")
        write_state(new, name, "raw.txt")
        if old is not None and old != new:
            add(0, f"{name} changed", unified(old, new, name), major=(name == "sitemap"))

    # --- Heavy: code scan (cadence-gated) ---
    if FORCE_FULL or time.time() - last_run("codescan.txt") >= CODESCAN_INTERVAL:
        try:
            findings = "\n".join(code_scan())
            old = read_state("code-findings.txt")
            write_state(findings, "code-findings.txt")
            mark_run("codescan.txt")
            if old is not None:
                new_f = added_lines(old, findings)
                if new_f:
                    blob = " ".join(new_f).lower()
                    is_major = (any(k in blob for k in MAJOR_KEYWORDS)
                                or "date:" in blob or "route:" in blob)
                    add(0, "new clues in site code",
                        "\n".join(f"- {x}" for x in new_f), major=is_major)
        except Exception as e:
            sys.stderr.write(f"[warn] code scan failed: {e}\n")

    # --- Heavy: URL probe (cadence-gated) ---
    if FORCE_FULL or time.time() - last_run("probe.txt") >= PROBE_INTERVAL:
        try:
            old = read_state("probe-known.txt")
            known = set(old.splitlines()) if old else set(PROBE_SEED)
            new_pages, known = url_probe(known)
            write_state("\n".join(sorted(known)), "probe-known.txt")
            mark_run("probe.txt")
            if old is not None and new_pages:
                add(0, "new pages live",
                    "\n".join(f"- {BASE}/VI/{p}" for p in new_pages), major=True)
        except Exception as e:
            sys.stderr.write(f"[warn] url probe failed: {e}\n")

    # --- Baseline: record everything, announce, exit ---
    if baseline:
        n = sum(1 for _r, _d, files in os.walk(STATE_DIR) for f in files
                if f.endswith(".txt"))
        post_discord(
            f"🛰️ **GTA Bot Ultracode initialised** | {stamp}\n\n"
            f"Captured a baseline across pages, media counts, site code, and "
            f"candidate URLs ({n} artifacts). One tool now watches everything the "
            f"old monitors did — and explains any meaningful change in plain English.")
        safe_print("Baseline established.")
        return

    # --- Heartbeat (runs whether or not there's an alert) ---
    try:
        if time.time() - last_run("heartbeat.txt") >= HEARTBEAT_INTERVAL:
            post_heartbeat(stamp)
            mark_run("heartbeat.txt")
    except Exception as e:
        sys.stderr.write(f"[warn] heartbeat failed: {e}\n")

    if not sections:
        safe_print("Routine rebuild only." if context_changed else "No changes.")
        return

    # --- Build the bundle, analyse, post ---
    sections.sort(key=lambda s: s[0])
    is_major = any(m for _p, _t, _b, m in sections)
    bundle, used = [], 0
    for _p, title, body, _m in sections:
        block = f"=== {title.upper()} ===\n{body}"
        if used + len(block) > 28000:
            block = block[: max(0, 28000 - used)] + "\n[... truncated ...]"
            bundle.append(block)
            break
        bundle.append(block)
        used += len(block) + 2

    # Ground the AI in established facts (user-editable known-facts.txt) so it
    # never re-hypes settled things (e.g. that pre-orders are already live).
    facts = ""
    try:
        with open("known-facts.txt", encoding="utf-8") as f:
            facts = f.read().strip()
    except Exception:
        pass
    user_content = "\n\n".join(bundle)
    if facts:
        user_content = ("ESTABLISHED FACTS — already known and true; do NOT report "
                        "these as new or upcoming, use them only to interpret the "
                        "changes below:\n" + facts
                        + "\n\n--- CHANGES DETECTED THIS SCAN ---\n\n" + user_content)

    analysis = analyse(user_content)
    titles = sorted({t for _p, t, _b, _m in sections})
    header = (f"{'🔴' if is_major else '🚨'} **GTA Bot Ultracode — "
              f"change detected** | {stamp}\n")
    if analysis:
        message = header + "\n" + analysis
    else:
        reason = f" ({LAST_AI_ERROR})" if LAST_AI_ERROR else ""
        message = (header + f"\nChanges found in: {', '.join(titles)}\n"
                   f"AI write-up unavailable{reason} — see the snapshot diff.\n"
                   f"\n{BASE}/VI/")
    if is_major and MAJOR_PING:
        message = f"{MAJOR_PING} **🔴 MAJOR signal**\n" + message
    post_discord(message, allow_mentions=bool(is_major and MAJOR_PING))
    append_log(stamp, [(t, b) for _p, t, b, _m in sections], analysis)
    safe_print(f"Alert posted{' [MAJOR]' if is_major else ''}. "
               f"Sections: {', '.join(titles)}")


if __name__ == "__main__":
    main()
