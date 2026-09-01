"""An incomplete view of the site must never manufacture "new" findings.

Regression for the duplicate-media bug: fetch() returned None both for "this
page does not exist" and "the site was unreachable", so a network blip silently
shrank the site-wide aggregates. The failed page's assets dropped out of
media-assets.txt, and the next healthy run re-posted every one of them to the
images channel as new.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import load, report  # noqa: E402

uc, STATE = load()

POSTED = []
uc.post_media = lambda name, url, is_video: POSTED.append(name)
uc.post_discord = lambda msg, allow_mentions=False: None
uc.append_log = lambda *a: None
uc.code_scan = lambda: []
uc.url_probe = lambda known: ([], known)


def page_html(assets):
    imgs = "".join(f'<img src="/_next/static/media/{a}.abc123.jpg">' for a in assets)
    return ("<html><head><title>GTA VI</title></head><body>"
            f"{imgs}<p>Jason and Lucia in Vice City</p>"
            '<script>self.__next_f.push([1,"{\\"copy\\":\\"Lucia returns to Leonida'
            '\\"}"])</script></body></html>')


def make_fetch(dead=()):
    """`dead` names surfaces that are UNREACHABLE this run (transport failure),
    as distinct from the speculative pages that simply 404."""
    def fake(url, retries=2):
        for surface in dead:
            if url.rstrip("/").endswith(surface):
                return None
        if url.endswith("robots.txt") or url.endswith("sitemap.xml"):
            return 200, {}, "Sitemap: /VI/"
        if "/VI/jason" in url:
            return 200, {"etag": "x"}, page_html(["jason_hero", "jason_car"])
        if url.rstrip("/").endswith("/VI"):
            return 200, {"etag": "x"}, page_html(["home_key_art"])
        if "/VI/media" in url:
            return 200, {"etag": "x"}, page_html(["screenshot_01"])
        if any(p in url for p in ("/VI/only-in-leonida", "/VI/editions")):
            return 200, {"etag": "x"}, page_html(["edition_art"])
        return 404, {}, "<html>not found</html>"   # a page that is not there yet
    return fake


def run(dead=()):
    POSTED.clear()
    uc.fetch = make_fetch(dead)
    uc.main()
    return list(POSTED)


run()                       # baseline
steady = run()              # nothing has changed
blip = run(("jason",))      # the jason page is unreachable
recovered = run()           # ...and comes back

checks = [
    ("no media posted in steady state", steady == []),
    ("no media posted while a page is unreachable", blip == []),
    ("recovered page does not re-post known art", recovered == []),
    ("flight-strings snapshot survived the blip",
     os.path.exists(os.path.join(STATE, "flight-strings.txt"))),
]

ledger = open(os.path.join(STATE, "media-assets.txt"), encoding="utf-8").read()
checks.append(("unreachable page's assets kept in the ledger",
               "jason_hero" in ledger and "jason_car" in ledger))


def fetch_with_new(url, retries=2):
    if "/VI/media" in url:
        return 200, {"etag": "x"}, page_html(["screenshot_01", "screenshot_02_NEW"])
    return make_fetch(())(url)


POSTED.clear()
uc.fetch = fetch_with_new
uc.main()
checks.append(("a genuinely new asset is still detected",
               POSTED == ["screenshot_02_NEW"]))

sys.exit(report(checks))
