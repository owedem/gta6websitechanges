"""The 15 Oct 2026 false alarm and the AI failure that came with it.

Rockstar moved the og:image / twitter:image URL on every page at once (same
picture, same content hash), which posted "<page> head changed" for 17 pages.
The write-up then failed: the 28k-char bundle was too big for Groq's free tier
(HTTP 413, 8000 tokens per minute including max_tokens).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import load, report  # noqa: E402

uc, _ = load()

DESC = "description: Vice City, USA."
BEFORE = "\n".join([
    DESC,
    "og:image: https://www.rockstargames.com/VI/-/opengraph-image.jpg"
    "?opengraph-image.0t8ty~nlmxq2s.jpg",
    "og:image:width: 1200",
    "twitter:image: https://www.rockstargames.com/VI/-/twitter-image.jpg"
    "?twitter-image.0t8ty~nlmxq2s.jpg",
])
AFTER = "\n".join([
    DESC,
    "og:image: https://www.rockstargames.com/VI/_next/static/media/"
    "meta.0t8ty~nlmxq2s.jpg?qXOoWo7_YJjVLK9jY_b4F",
    "og:image:width: 1200",
    "twitter:image: https://www.rockstargames.com/VI/_next/static/media/"
    "meta.0t8ty~nlmxq2s.jpg?qXOoWo7_YJjVLK9jY_b4F",
])
NEW_IMAGE = AFTER.replace("0t8ty~nlmxq2s", "9zz1abcde77q")
NEW_TITLE = AFTER + "\ntitle: Grand Theft Auto VI - Pre-Order Now"

checks = [
    ("same image, moved URL: no alert", uc.head_key(BEFORE) == uc.head_key(AFTER)),
    ("new image hash: alerts", uc.head_key(AFTER) != uc.head_key(NEW_IMAGE)),
    ("other head change: alerts", uc.head_key(AFTER) != uc.head_key(NEW_TITLE)),
    ("unhashed image path change: alerts",
     uc.head_key("og:image: https://x/a.jpg") != uc.head_key("og:image: https://x/b.jpg")),
]

# Groq: a request must fit 8000 TPM even with a maximal bundle.
big = "x" * 40000
fitted = uc.groq_fit(big)
est_tokens = (len(uc.SYSTEM_PROMPT) + len(fitted)) / 3.5 + uc.GROQ_MAX_TOKENS
checks += [
    ("groq bundle trimmed to cap", len(fitted) <= uc.GROQ_MAX_USER_CHARS),
    (f"groq request fits 8000 TPM (est {est_tokens:.0f})", est_tokens < 8000),
    ("small bundle untouched", uc.groq_fit("short") == "short"),
]

sys.exit(report(checks))
