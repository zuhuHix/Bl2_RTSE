"""Downloads the Latin woff2 files for the UI fonts into rtse/web/fonts so the page works offline.

    python -I dev/fetch_fonts.py
"""

import re
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "rtse" / "web" / "fonts"
CSS_URL = (
    "https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Barlow:wght@400;500;600;700&display=swap"
)
# A current browser UA makes Google serve woff2
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as response:
        return response.read()


OUT.mkdir(parents=True, exist_ok=True)
css = get(CSS_URL).decode()
blocks = re.findall(r"/\* (\S+) \*/\s*(@font-face \{.*?\})", css, re.S)
for subset, block in blocks:
    if subset != "latin":
        continue
    family = re.search(r"font-family: '([^']+)'", block).group(1)
    weight = re.search(r"font-weight: (\d+)", block).group(1)
    url = re.search(r"url\((https://[^)]+\.woff2)\)", block).group(1)
    name = f"{family.lower().replace(' ', '-')}-{weight}.woff2"
    (OUT / name).write_bytes(get(url))
    print(f"{name}: {(OUT / name).stat().st_size} bytes")
