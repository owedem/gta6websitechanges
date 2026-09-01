"""Noise filters, checked against real strings and asset names from the log.

Every NOISE entry and every JUNK_MEDIA name below actually reached Discord (see
ultracode-log.md); every SIGNAL and REAL_MEDIA entry is something the bot exists
to catch. Add to these lists whenever a new false positive or miss shows up.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import load, report  # noqa: E402

uc, _ = load()

# Minified RSC fragments that were re-reported on nearly every rebuild, because
# they happen to contain a rockstar url or a game word.
NOISE = [
    ',"rockstar",{"variant":"platform","label":"Open external link to Rockstar store"'
    ',"analytics":{"event":"cta_store_link","text":"rockstar","element_placement":'
    '"sku-selector - standard","link_url":"https://store.rockstargames.com/game/'
    'buy-gta-vi"},"asChild":true,"children":"$L125"}]]}],"$L126"]}]}]]',
    ':1,kalaga:1,ambrosia:1,',
    ',"content":"image/jpeg"}]',
    '1d8:["$","$L1d6",null,{}]',
    '1da:["$","meta","34",{"name":"twitter:image:width","content":"1200"}]',
    'target":"_blank","href":"https://store.rockstargames.com/game/buy-gta-vi",'
    '"children":[["$","$L1d2",null,{"children":[["$","$L1d3",null,{"platform":'
    '"rockstar"}]]}]]}]',
]

SIGNAL = [
    "/VI/an-extended-look",
    "https://www.rockstargames.com/VI/-/twitter-image.jpg",
    "November 19, 2026",                 # the form Rockstar writes dates in
    "An Extended Look, Coming August 27",
    "Pre-Order Grand Theft Auto VI on June 25",
    "Grand Theft Auto VI is Now Set to Launch November 19, 2026",
    "Jason and Lucia have always known the deck is stacked against them.",
    "Ultimate Edition",
    "PreorderDrawerContent",
    '{"launch":"2026-11-19","x":1}',     # a code blob is kept if it carries a date
]

checks = []
for s in NOISE:
    checks.append((f"rejects: {s[:52]}...", uc.is_meaningful(s) is False))
for s in SIGNAL:
    checks.append((f"keeps:   {s[:52]}", uc.is_meaningful(s) is True))

# Build ids and chrome that were posted to the images channel as "new images".
JUNK_MEDIA = ["3642a34ab778931f321cc65c7d870a090dcfd4a4", "5171972o3ak5oa",
              "ak3ak31a49a221", "9k2kaa1o3297k9", "featured", "featured-mobile",
              "skybox"]
REAL_MEDIA = ["Jason_and_Lucia_Robbery_landscape", "Jason_Duval_07",
              "Lucia_Caminos_09", "Vice_City_11", "GTAVI_Trailer2_poster",
              "an-extended-look", "GTAVI_An_Extended_Look_poster",
              "Ambrosia_01", "screenshot_01", "Vice_City_Postcard"]

for name in JUNK_MEDIA + REAL_MEDIA:
    kept = name in uc.media_assets(f'<img src="/_next/static/media/{name}.hash.jpg">')
    want = name in REAL_MEDIA
    checks.append((f"media {'keeps  ' if want else 'skips  '}: {name}", kept == want))

# Date shapes: every form the site uses, and no bare years.
for text, want in [("November 19, 2026", True), ("19 November 2026", True),
                   ("2026-11-19", True), ("2026/11/19", True),
                   ("November 2026", True), ("August 27th, 2026", True),
                   ("coming in 2026", False)]:
    checks.append((f"date {'matches' if want else 'ignores'}: {text}",
                   bool(uc.DATE_RE.findall(text)) is want))

sys.exit(report(checks))
