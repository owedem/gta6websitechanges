#!/usr/bin/env python3
"""
GTA Bot Ultracode
=================

A separate, deep-dive monitor for the official GTA VI website. Where the other
workflows (monitor.yml / scan.yml / probe.yml) watch for *specific* known
patterns, this one takes a broad forensic snapshot of every GTA VI surface,
diffs it byte-for-byte against the previous run, and — when something
*meaningful* changes — asks Claude to explain the before/after in plain English
and cautiously forecast what it might mean.

For each page it captures and normalises:
  - text     : visible page copy (tags stripped)          [meaningful → alerts]
  - head     : <title>, meta description, Open Graph /     [meaningful → alerts]
               Twitter tags (og:image, og:title, ...), canonical
  - routes   : /VI/* navigation links (excluding _next)    [meaningful → alerts]
  - assets   : referenced /_next/static/* filenames        [supporting context]
               (content-hashed → rotate on every rebuild)
  - headers  : stable subset of response headers (ETag...) [supporting context]
Plus site-wide robots.txt and the GTA VI slice of sitemap.xml  [meaningful].

The site is a Next.js App Router build (no __NEXT_DATA__/buildId); the ETag in
`headers` is the per-deploy fingerprint.

Routine rebuilds (only asset hashes / ETag rotate) are snapshotted silently — no
Discord ping — because monitor.yml already reports those. A Discord post + AI
analysis fires only when a *meaningful* surface changes. Claude is only called
when an alert fires, so cost stays near zero.

Snapshots live under STATE_DIR (committed to the repo) so each run diffs against
the last.

The AI write-up can be powered by a FREE provider (Google Gemini) or a paid one
(Anthropic Claude). Gemini wins if both keys are set. With no key at all, the
workflow still detects/diffs/commits and posts a non-AI summary.

Env:
  GEMINI_API_KEY      - free AI layer via Google Gemini (preferred if set)
  GEMINI_MODEL        - Gemini model id (default: gemini-2.0-flash)
  ANTHROPIC_API_KEY   - paid AI layer via Claude (used only if no GEMINI_API_KEY)
  ANALYSIS_MODEL      - Claude model id (default: claude-opus-4-8)
  DISCORD_WEBHOOK     - where to post (degrades gracefully if absent)
  STATE_DIR           - snapshot directory (default: ultracode-state)
  LOG_FILE            - changelog path (default: ultracode-log.md)
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
MODEL = os.environ.get("ANALYSIS_MODEL", "claude-opus-4-8")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Free alternative: Google Gemini. If GEMINI_API_KEY is set it is preferred over
# Anthropic, so the AI write-ups cost nothing.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
# When set (via the workflow's "selftest" dispatch input), post a synthetic
# alert through the real pipeline to verify the webhook + AI key.
SELFTEST = os.environ.get("SELFTEST", "").strip().lower() in ("1", "true", "yes")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Pages we fetch and decompose into artifacts.
PAGES = [
    ("home", "https://www.rockstargames.com/VI/"),
    ("only-in-leonida", "https://www.rockstargames.com/VI/only-in-leonida"),
    ("media", "https://www.rockstargames.com/VI/media"),
    ("editions", "https://www.rockstargames.com/VI/editions"),
]

# Raw text endpoints. `grep` (optional) keeps only lines containing that
# substring (case-insensitive) so we stay scoped to GTA VI. sitemap currently
# 404s — kept because a sitemap *appearing* would itself be a signal.
RAW = [
    ("robots", "https://www.rockstargames.com/robots.txt", None),
    ("sitemap", "https://www.rockstargames.com/sitemap.xml", "/vi"),
]

# A change in these triggers a Discord alert + AI analysis. They are
# human-meaningful and do NOT rotate on a routine rebuild.
TRIGGER_ARTIFACTS = {"text", "head", "routes", "robots", "sitemap"}

# Captured and shown to Claude as supporting context, but a change here alone
# does not raise an alert (these churn on every deploy).
CONTEXT_ARTIFACTS = {"assets", "headers"}

# Order in which diffs are bundled for Claude (most meaningful first).
ARTIFACT_PRIORITY = ["head", "text", "routes", "robots", "sitemap",
                     "headers", "assets"]
DIFF_BUDGET = 30000

KEEP_HEADERS = {
    "etag", "last-modified", "content-length", "content-type",
    "cache-control", "x-nextjs-cache", "x-nextjs-prerender",
}

ASSET_RE = re.compile(
    r"/_next/static/[A-Za-z0-9_.~/\-]+"
    r"\.(?:js|css|woff2?|json|png|jpe?g|webp|svg|avif|gif|ico|mp4|webm)"
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def safe_print(s):
    """Print Unicode safely regardless of console encoding."""
    try:
        sys.stdout.buffer.write((s + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()
    except Exception:
        sys.stdout.write(s.encode("ascii", "replace").decode("ascii") + "\n")


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def fetch(url, retries=2):
    """Return (status, headers_dict, body_text) or None on failure."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                body = raw.decode("utf-8", "replace")
                return resp.status, headers, body
        except Exception as e:  # network blip, 5xx, 404, timeout, etc.
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    sys.stderr.write(f"[warn] fetch failed for {url}: {last_err}\n")
    return None


# --------------------------------------------------------------------------- #
# Extraction / normalisation — every function returns a deterministic string
# so that git diffs are clean and stable.
# --------------------------------------------------------------------------- #


def x_assets(html):
    return "\n".join(sorted(set(ASSET_RE.findall(html))))


def x_routes(html):
    # /VI/* navigation links, excluding hashed /_next/static asset references.
    found = set(re.findall(r'href="(/VI/(?!_next/)[^"#?]*)"', html))
    return "\n".join(sorted(found))


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
    return "\n".join(
        f"{k}: {headers[k]}" for k in sorted(headers) if k in KEEP_HEADERS
    )


def build_artifacts(html, headers):
    return {
        "text": x_text(html),
        "head": x_head(html),
        "routes": x_routes(html),
        "assets": x_assets(html),
        "headers": x_headers(headers),
    }


# --------------------------------------------------------------------------- #
# Snapshot storage + diffing
# --------------------------------------------------------------------------- #


def artifact_path(surface, name):
    return os.path.join(STATE_DIR, surface, f"{name}.txt")


def read_old(surface, name):
    p = artifact_path(surface, name)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return None


def write_new(surface, name, content):
    p = artifact_path(surface, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def make_diff(old, new, label):
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"{label} (before)", tofile=f"{label} (after)",
        lineterm="", n=2,
    )
    return "\n".join(diff)


def assets_summary(old, new, label):
    """A one-line summary instead of a 2000-line hash diff."""
    o, n = set(old.splitlines()), set(new.splitlines())
    return (f"{label}: +{len(n - o)} new / -{len(o - n)} removed asset file(s) "
            f"(content-hashed JS/CSS/media — these rotate on every rebuild).")


# --------------------------------------------------------------------------- #
# Claude analysis
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are a forensic analyst for a community that tracks the official Grand Theft \
Auto VI website (rockstargames.com/VI) for the earliest possible signs of news: \
a release date, pre-orders going live, new trailers/screenshots, or new pages \
appearing.

You are given a DIFF between two automatically-captured technical snapshots of \
the site, taken about 30 minutes apart. The snapshot artifacts are:
  - text    : the visible copy on the page, with HTML tags stripped.
  - head    : <title>, meta description, and Open Graph / Twitter share tags \
(og:image, og:title, ...). The share image and title are often updated shortly \
before content goes live, so changes here are a strong early signal.
  - routes  : /VI/* navigation links present on the page (a new one means a new \
page is being linked).
  - robots / sitemap : robots.txt and the GTA VI slice of the sitemap; new URLs \
here can reveal pages before they are linked anywhere.
  - assets  : a SUMMARY of how many content-hashed /_next/static files changed. \
These rotate on EVERY deploy even when nothing visible changed, so treat asset \
churn as background noise, not news — it only tells you a deploy happened.
  - headers : selected HTTP response headers. The ETag changes on every deploy.

Write a Discord post for a NON-TECHNICAL audience of GTA fans. Use this exact \
structure:

**<emoji> One-line headline**

**What changed:**
- 2-6 short bullets in plain English describing before -> after. Translate the \
technical artifacts into what a fan would actually care about. Quote the specific \
new/changed text, dates, image names, or routes when present. Skip pure noise.

**What it likely means:**
- 1-3 sentences.

**Could signal:** _(confidence: low | medium | high)_
- Cautious, clearly-hedged speculation about what might be coming and a rough \
timeframe if one is inferable from the diff.

Rules: Be precise and grounded strictly in the diff — never invent dates, names, \
or details that are not in it. If the change turns out to be cosmetic or just a \
deploy with no real content change, say so plainly and keep the post short. Never \
present speculation as fact.\
"""


def _user_content(changed_surfaces, diff_bundle):
    return (
        f"Surfaces with changes: {', '.join(changed_surfaces)}\n\n"
        f"Here is the diff:\n\n{diff_bundle}"
    )


# Records the most recent AI-provider failure reason; surfaced by the self-test.
LAST_AI_ERROR = None


def analyse(changed_surfaces, diff_bundle):
    """Dispatch to whichever LLM provider is configured. Free Gemini is preferred
    over (paid) Anthropic. Returns plain-English analysis or None."""
    if GEMINI_API_KEY:
        return analyse_with_gemini(changed_surfaces, diff_bundle)
    if ANTHROPIC_API_KEY:
        return analyse_with_claude(changed_surfaces, diff_bundle)
    sys.stderr.write("[info] No LLM key set (GEMINI_API_KEY / ANTHROPIC_API_KEY) "
                     "— skipping AI analysis.\n")
    return None


def analyse_with_gemini(changed_surfaces, diff_bundle):
    """Free option: Google Gemini API over raw HTTP (no SDK dependency)."""
    global LAST_AI_ERROR
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": _user_content(changed_surfaces, diff_bundle)}],
        }],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1500},
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        reason = body
        try:
            reason = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            pass
        LAST_AI_ERROR = f"HTTP {e.code} — {reason[:240]}"
        sys.stderr.write(f"[warn] Gemini: {LAST_AI_ERROR}\n")
        return None
    except Exception as e:
        LAST_AI_ERROR = f"request failed — {e}"
        sys.stderr.write(f"[warn] Gemini: {LAST_AI_ERROR}\n")
        return None

    cands = data.get("candidates") or []
    if not cands:
        fb = data.get("promptFeedback", {})
        LAST_AI_ERROR = f"no candidates returned (promptFeedback: {json.dumps(fb)[:160]})"
        sys.stderr.write(f"[warn] Gemini: {LAST_AI_ERROR}\n")
        return None
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        LAST_AI_ERROR = f"empty text (finishReason: {cands[0].get('finishReason')})"
        sys.stderr.write(f"[warn] Gemini: {LAST_AI_ERROR}\n")
        return None
    return text


def analyse_with_claude(changed_surfaces, diff_bundle):
    """Paid option: Claude via the Anthropic SDK. Returns analysis or None."""
    try:
        import anthropic
    except Exception as e:
        sys.stderr.write(f"[warn] anthropic SDK unavailable: {e}\n")
        return None

    user_content = _user_content(changed_surfaces, diff_bundle)

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        if resp.stop_reason == "refusal":
            sys.stderr.write("[warn] Claude refused to analyse this diff.\n")
            return None
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None
    except Exception as e:
        sys.stderr.write(f"[warn] Claude analysis failed: {e}\n")
        return None


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #


def post_discord(message):
    if not DISCORD_WEBHOOK:
        sys.stderr.write("[info] DISCORD_WEBHOOK not set — printing instead:\n")
        safe_print(message)
        return
    for chunk in chunk_text(message, 1900):
        payload = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            sys.stderr.write(f"[warn] Discord post failed: {e}\n")
        time.sleep(1)


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


# --------------------------------------------------------------------------- #
# Changelog
# --------------------------------------------------------------------------- #


def append_log(stamp, changed, analysis):
    header = f"\n## {stamp}\n\n"
    body = f"**Surfaces:** {', '.join(changed)}\n\n"
    body += (analysis + "\n") if analysis else (
        "_(AI analysis unavailable — diff committed to snapshot state.)_\n")
    with open(LOG_FILE, "a", encoding="utf-8", newline="\n") as f:
        f.write(header + body)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def is_baseline():
    """True if we have no prior snapshots at all (first ever run)."""
    if not os.path.isdir(STATE_DIR):
        return True
    for _root, _dirs, files in os.walk(STATE_DIR):
        if any(f.endswith(".txt") for f in files):
            return False
    return True


def run_selftest(stamp):
    """Post a synthetic alert through the real analyse()+Discord pipeline so the
    user can verify the webhook and AI key work, without waiting for a real
    site change. Touches no snapshot state."""
    changed = ["home"]
    bundle = (
        "--- home/head (before)\n+++ home/head (after)\n@@ -1,2 +1,2 @@\n"
        "-og:image: https://www.rockstargames.com/VI/-/opengraph-image.OLD.jpg\n"
        "-title: Grand Theft Auto VI - Rockstar Games\n"
        "+og:image: https://www.rockstargames.com/VI/-/opengraph-image.NEW.jpg\n"
        "+title: Grand Theft Auto VI - Pre-Order Now\n\n"
        "--- home/routes (before)\n+++ home/routes (after)\n@@ -1,2 +1,3 @@\n"
        " /VI/\n+/VI/buy\n /VI/only-in-leonida"
    )
    provider = "Gemini" if GEMINI_API_KEY else ("Claude" if ANTHROPIC_API_KEY else "none")
    analysis = analyse(changed, bundle)
    note = (f"🧪 **GTA Bot Ultracode — SELF-TEST** | {stamp}\n"
            f"_(synthetic change, NOT real — just verifying the pipeline. "
            f"AI provider: {provider})_\n")
    if analysis:
        post_discord(note + "\n" + analysis)
    elif provider == "none":
        post_discord(note + "\n(No AI key set — add GEMINI_API_KEY for free "
                     "English explanations.)")
    else:
        post_discord(note + f"\n⚠️ The **{provider}** key is set but the call "
                     f"failed:\n```{LAST_AI_ERROR}```\nFix that and re-run the "
                     f"self-test.")
    safe_print(f"Self-test posted (provider: {provider}, error: {LAST_AI_ERROR}).")


def main():
    baseline = is_baseline()
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y at %H:%M UTC")

    if SELFTEST:
        run_selftest(stamp)
        return

    # Each entry: (priority, surface, artifact, old, new)
    changes = []
    changed_surfaces = set()
    triggered = False

    # --- Pages ---
    for surface, url in PAGES:
        result = fetch(url)
        if result is None:
            continue  # keep the old snapshot; don't treat a blip as a change
        status, headers, body = result
        if status != 200 or "<html" not in body.lower():
            sys.stderr.write(f"[warn] {surface}: unexpected response (status {status})\n")
            continue
        for name, new in build_artifacts(body, headers).items():
            old = read_old(surface, name)
            write_new(surface, name, new)
            if old is not None and old != new:
                changes.append((ARTIFACT_PRIORITY.index(name), surface, name, old, new))
                changed_surfaces.add(surface)
                if name in TRIGGER_ARTIFACTS:
                    triggered = True

    # --- Raw endpoints (robots, sitemap) ---
    for name, url, grep in RAW:
        result = fetch(url)
        if result is None:
            continue
        _status, _headers, body = result
        if grep:
            body = "\n".join(ln for ln in body.splitlines() if grep.lower() in ln.lower())
        new = body.strip()
        old = read_old(name, "raw")
        write_new(name, "raw", new)
        if old is not None and old != new:
            changes.append((ARTIFACT_PRIORITY.index(name), name, name, old, new))
            changed_surfaces.add(name)
            triggered = True

    # --- Baseline run: store everything, announce, exit ---
    if baseline:
        n = sum(1 for _r, _d, files in os.walk(STATE_DIR) for f in files
                if f.endswith(".txt"))
        post_discord(
            f"🛰️ **GTA Bot Ultracode initialised** | {stamp}\n\n"
            f"Captured a baseline forensic snapshot ({n} artifacts across "
            f"{len(PAGES)} pages + robots/sitemap). From now on, any meaningful "
            f"change — even the smallest — will be diffed and explained in plain "
            f"English.")
        safe_print("Baseline established.")
        return

    if not changes:
        safe_print("No changes detected.")
        return

    if not triggered:
        # Only asset hashes / ETag rotated — a routine rebuild. Snapshot is
        # already updated on disk; stay quiet (monitor.yml covers rebuilds).
        safe_print(f"Routine rebuild only ({', '.join(sorted(changed_surfaces))}) "
                   f"— snapshot updated silently, no alert.")
        return

    # --- Build the (size-capped) diff bundle for Claude ---
    changes.sort(key=lambda c: c[0])
    parts, used = [], 0
    for _prio, surface, name, old, new in changes:
        label = f"{surface}/{name}"
        block = (assets_summary(old, new, label) if name == "assets"
                 else make_diff(old, new, label))
        block = block.strip()
        if not block:
            continue
        if used + len(block) > DIFF_BUDGET:
            block = block[: max(0, DIFF_BUDGET - used)] + "\n[... truncated ...]"
            parts.append(block)
            break
        parts.append(block)
        used += len(block) + 2
    diff_bundle = "\n\n".join(parts)

    changed_sorted = sorted(changed_surfaces)
    analysis = analyse(changed_sorted, diff_bundle)

    header = f"🚨 **GTA Bot Ultracode — change detected** | {stamp}\n"
    if analysis:
        message = header + "\n" + analysis
    else:
        message = (
            header
            + f"\nChanged surfaces: {', '.join(changed_sorted)}\n"
            + "Meaningful change detected (text / metadata / routes). "
            + "AI analysis was unavailable — see the committed snapshot diff.\n"
            + "\nhttps://www.rockstargames.com/VI/"
        )
    post_discord(message)
    append_log(stamp, changed_sorted, analysis)
    safe_print(f"Alert posted. Changed: {', '.join(changed_sorted)}")


if __name__ == "__main__":
    main()
