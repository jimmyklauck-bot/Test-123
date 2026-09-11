#!/usr/bin/env python3
"""
Pull Houston-metro advisor teams from independent broker-dealer directories.

Run this on your own machine. The BD directories are blocked from the Claude
Code sandbox, which is why this exists as a script instead of a finished list.

    python3 pull_houston_advisors.py                 # all BDs, writes CSV
    python3 pull_houston_advisors.py --bd ameriprise # one BD
    python3 pull_houston_advisors.py --debug         # also save raw HTML

Output: houston_advisors.csv  (bd, practice, advisors, street, city, state,
zip, county_ok, source_url)

No third-party packages required. Uses bs4 if it happens to be installed,
otherwise falls back to stdlib parsing.
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    from bs4 import BeautifulSoup  # optional, improves extraction
    HAVE_BS4 = True
except ImportError:
    HAVE_BS4 = False

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# ---------------------------------------------------------------- geography
# Houston-The Woodlands-Sugar Land MSA: Harris, Fort Bend, Montgomery,
# Brazoria, Galveston, Liberty, Waller, Chambers, Austin counties.
MSA_ZIP_PREFIXES = ("770", "771", "772", "773", "774", "775")

# ZIPs that share a prefix with the MSA but sit outside it. Extend as needed.
MSA_ZIP_EXCLUDE = {
    "77518", "77661", "77662", "77663",  # Jefferson/Hardin edges
}

# Cities searched. Covers the metro without pulling in Beaumont or Bryan.
HOUSTON_CITIES = [
    ("Houston", "77002"), ("Houston", "77024"), ("Houston", "77027"),
    ("Houston", "77042"), ("Houston", "77056"), ("Houston", "77057"),
    ("Houston", "77068"), ("Houston", "77077"), ("Houston", "77079"),
    ("Houston", "77081"), ("Houston", "77098"),
    ("Bellaire", "77401"), ("Sugar Land", "77479"), ("Missouri City", "77459"),
    ("Stafford", "77477"), ("Richmond", "77406"), ("Rosenberg", "77471"),
    ("Fulshear", "77441"), ("Katy", "77450"), ("Katy", "77494"),
    ("Cypress", "77429"), ("Cypress", "77433"), ("Spring", "77379"),
    ("The Woodlands", "77380"), ("The Woodlands", "77381"),
    ("Conroe", "77304"), ("Magnolia", "77354"), ("Tomball", "77375"),
    ("Humble", "77338"), ("Kingwood", "77339"), ("Atascocita", "77346"),
    ("Baytown", "77521"), ("Pasadena", "77505"), ("Deer Park", "77536"),
    ("Pearland", "77584"), ("Friendswood", "77546"), ("League City", "77573"),
    ("Webster", "77598"), ("Clear Lake", "77058"), ("Texas City", "77591"),
    ("Galveston", "77551"), ("Lake Jackson", "77566"), ("Angleton", "77515"),
    ("Alvin", "77511"), ("Waller", "77484"), ("Brookshire", "77423"),
]


def in_msa(zipcode: str) -> bool:
    z = (zipcode or "").strip()[:5]
    if len(z) != 5 or not z.isdigit():
        return False
    return z.startswith(MSA_ZIP_PREFIXES) and z not in MSA_ZIP_EXCLUDE


# ------------------------------------------------------------------ fetching
def fetch(url: str, debug_dir=None, tag="") -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  ! HTTP {e.code} for {url}", file=sys.stderr)
        return ""
    except Exception as e:  # noqa: BLE001 - network is the whole point here
        print(f"  ! {type(e).__name__} for {url}: {e}", file=sys.stderr)
        return ""
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{tag}_{url}")[-120:]
        with open(os.path.join(debug_dir, safe + ".html"), "w") as fh:
            fh.write(body)
    return body


def text_of(markup: str) -> str:
    if HAVE_BS4:
        return BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


# --------------------------------------------------------------- extraction
ADDR_RE = re.compile(
    r"(?P<street>\d{2,6}\s+[A-Z0-9][^,<>\n]{3,60}?)"
    r",?\s*(?P<city>[A-Z][A-Za-z .'-]{2,30}?),\s*TX\s*(?P<zip>\d{5})"
)


def extract_records(markup: str, bd: str, source_url: str):
    """Pull practice/advisor + address pairs out of a directory page.

    Directory markup changes without notice. Rather than binding to one set of
    CSS selectors, this scans for Texas addresses and takes the nearest
    preceding heading or link text as the practice name. Crude, but it
    survives redesigns. Use --debug and inspect the saved HTML if a BD
    returns nothing.
    """
    out = []
    if HAVE_BS4:
        soup = BeautifulSoup(markup, "html.parser")
        for tag in soup.find_all(["a", "h2", "h3", "h4", "li", "article", "div"]):
            blob = tag.get_text(" ", strip=True)
            if not blob or len(blob) > 600:
                continue
            m = ADDR_RE.search(blob)
            if not m:
                continue
            name = ""
            for prev in tag.find_all_previous(["h2", "h3", "h4", "a"], limit=4):
                cand = prev.get_text(" ", strip=True)
                if cand and 3 < len(cand) < 90 and not ADDR_RE.search(cand):
                    name = cand
                    break
            out.append(_rec(bd, name, blob, m, source_url))
    else:
        flat = html.unescape(re.sub(r"<[^>]+>", "\n", markup))
        lines = [l.strip() for l in flat.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            m = ADDR_RE.search(line)
            if not m:
                continue
            name = ""
            for back in range(i - 1, max(-1, i - 5), -1):
                cand = lines[back]
                if 3 < len(cand) < 90 and not ADDR_RE.search(cand):
                    name = cand
                    break
            out.append(_rec(bd, name, line, m, source_url))
    return out


def _rec(bd, name, blob, m, source_url):
    return {
        "bd": bd,
        "practice": name.strip(" -|·"),
        "advisors": "",
        "street": m.group("street").strip(),
        "city": m.group("city").strip(),
        "state": "TX",
        "zip": m.group("zip"),
        "county_ok": "yes" if in_msa(m.group("zip")) else "NO - outside MSA",
        "source_url": source_url,
        "_blob": blob[:300],
    }


# ------------------------------------------------------------------ adapters
# Each adapter yields URLs to fetch. Verify these against the live site the
# first time you run it; directory URL shapes do change.
ADAPTERS = {
    "ameriprise": lambda city, z: [
        "https://www.ameripriseadvisors.com/find-a-financial-advisor-by-state/"
        f"texas/{city.lower().replace(' ', '-')}/"
    ],
    "cetera": lambda city, z: [
        f"https://cetera.com/find-an-advisor?zip={z}&radius=10"
    ],
    "osaic": lambda city, z: [
        f"https://osaic.com/find-an-advisor?zip={z}"
    ],
    "kestra": lambda city, z: [
        f"https://www.kestrafinancial.com/find-an-advisor?zip={z}"
    ],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bd", choices=sorted(ADAPTERS), action="append",
                    help="limit to one or more broker-dealers")
    ap.add_argument("--out", default="houston_advisors.csv")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between requests (be polite)")
    ap.add_argument("--debug", action="store_true",
                    help="save raw HTML to ./debug_html for inspection")
    args = ap.parse_args()

    bds = args.bd or sorted(ADAPTERS)
    debug_dir = "debug_html" if args.debug else None
    seen, rows = set(), []

    for bd in bds:
        print(f"\n=== {bd} ===")
        for city, z in HOUSTON_CITIES:
            for url in ADAPTERS[bd](city, z):
                markup = fetch(url, debug_dir, bd)
                if not markup:
                    continue
                found = extract_records(markup, bd, url)
                new = 0
                for r in found:
                    key = (r["bd"], r["practice"].lower(), r["street"].lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(r)
                    new += 1
                print(f"  {city:<14} {z}  {len(found):>3} found, {new:>3} new")
                time.sleep(args.delay)

    in_metro = [r for r in rows if r["county_ok"] == "yes"]
    print(f"\n{len(rows)} records, {len(in_metro)} inside the Houston MSA")

    if not rows:
        print("\nNothing parsed. Re-run with --debug and check debug_html/ —"
              "\nthe directories may render results via JavaScript, in which"
              "\ncase use the browser devtools Network tab to find the JSON"
              "\nendpoint and point this script at that instead.",
              file=sys.stderr)
        return 1

    fields = ["bd", "practice", "advisors", "street", "city", "state", "zip",
              "county_ok", "source_url"]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["bd"], r["city"], r["practice"])))
    print(f"wrote {args.out}")

    with open("houston_advisors_raw.json", "w") as fh:
        json.dump(rows, fh, indent=1)
    print("wrote houston_advisors_raw.json (includes matched text for QA)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
