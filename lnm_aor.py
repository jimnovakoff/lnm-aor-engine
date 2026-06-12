#!/usr/bin/env python3
"""
lnm-aor-engine -- Parameterized USCG District 1 LNM parser.

Fetches the weekly D1 Local Notice to Mariners PDF and extracts, for each
configured Area of Responsibility (AOR), a small JSON heads-up:

  - ATON discrepancies (Federal/Private Discrepancy tables, matched on aid NAME)
  - Hazard locations (alphabetical place-name sections, matched on the HEADER),
    summarized as place + hazard category only

The parsing approach is the line/section-aware parser from AuxRadio Fairhaven's
uscg_lnm_automation.py v3.4.8 (it replaced a blob-matcher that produced
district-wide false positives). The flattened multi-column PDF text scrambles
free-text narrative and coordinates beyond reliable reconstruction, so this
engine deliberately emits NO narrative and NO positions -- consumers must link
to the full LNM for detail. That is a safety posture, not a limitation to fix.

Consumers:
  - AuxRadio Fairhaven pre-watch briefing (aor: buzzards-bay)
  - SBC / Sally pre-sail briefing       (aor: narragansett-east-passage)

Usage:
  python lnm_aor.py --all                      # fetch live LNM, emit data/<aor>.json
  python lnm_aor.py --aor buzzards-bay         # one AOR only
  python lnm_aor.py --all --pdf-file x.pdf     # parse a local PDF (testing)
"""

import argparse
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import pdfplumber
import requests

NAVCEN_LNM_RSS = "https://public.govdelivery.com/topics/USDHSCG_65/feed.rss"
NAVCEN_D1_RE = re.compile(r"d0?1", re.IGNORECASE)
UA = {"User-Agent": "lnm-aor-engine (github.com/jimnovakoff/lnm-aor-engine)"}

# --- Structural regexes (from uscg_lnm_automation.py v3.4.8) -----------------
WINDFARM_RE = re.compile(r"\bWTG\b|Wind\s*Farm|Windfarm|Wind\s+\d|Wind Wave", re.I)
DISC_KIND_RE = re.compile(r"\b(Federal|Private) Discrepancies\b", re.I)
DISC_HEADER_RE = re.compile(r"^NAME\s+LLNR\s+STATUS\s+AID\s*TYPE", re.I)
MSI_HEADER_RE = re.compile(r"^TITLE\s+SUBCATEGORY\s+DESCRIPTION\s+LOCATION", re.I)
DISC_ROW_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9].*?)\s+(?P<llnr>\d{2,5}(?:\.\d+)?|null)\s+"
    r"(?P<status>[A-Za-z].*?)\s+(?P<type>[A-Z]{2})$")
DISC_ROW_GUARD = re.compile(r"\s(?:\d{2,5}(?:\.\d+)?|null)\s+[A-Za-z].*\s[A-Z]{2}$")
SECTION_MARK_RE = re.compile(r"^(Additional MSI Categories|(Federal|Private) Discrepancies)", re.I)
HAZARD_CAT_RE = re.compile(r"(Shoaling Reported|Obstructions?|Dredging|Wreck|Unexploded Ordnance)", re.I)


def clean_llnr(s):
    if s.lower() == "null":
        return "n/a"                      # source data carries literal "null"
    if "." in s:
        try:
            return f"{float(s):g}"        # 16302.300000000001 -> 16302.3
        except ValueError:
            return s
    return s


def norm_hazard_cat(c):
    c = c.lower()
    if c.startswith("shoal"):
        return "Shoaling"
    if c.startswith("obstruct"):
        return "Obstruction"
    if c.startswith("dredg"):
        return "Dredging"
    if c.startswith("wreck"):
        return "Wreck"
    if "ordnance" in c:
        return "Unexploded Ordnance"
    return ""


def split_place_sections(lines):
    """Split the LNM into (place_header, body_lines) sections. A place header is
    a short line immediately followed by a section marker, and is not itself a
    marker, a table header, or a discrepancy row."""
    sections, cur, body, n = [], None, [], len(lines)
    for i, s in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < n else ""
        if (s and SECTION_MARK_RE.match(nxt) and not SECTION_MARK_RE.match(s)
                and len(s) < 70 and not DISC_HEADER_RE.match(s)
                and not MSI_HEADER_RE.match(s) and not DISC_ROW_GUARD.search(s)):
            if cur is not None:
                sections.append((cur, body))
            cur, body = s, []
            continue
        if cur is not None:
            body.append(s)
    if cur is not None:
        sections.append((cur, body))
    return sections


class AorMatcher:
    """Word-boundary keyword matcher with an exclusion list. Excludes guard
    against substring traps like 'Little Narragansett Bay' (Stonington CT)
    matching a 'narragansett bay' keyword."""

    def __init__(self, keywords, excludes=()):
        self.kw_re = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b", re.I)
        self.ex_re = (re.compile(
            r"\b(" + "|".join(re.escape(e) for e in excludes) + r")\b", re.I)
            if excludes else None)

    def match(self, text):
        if self.ex_re and self.ex_re.search(text):
            return False
        return bool(self.kw_re.search(text))


def parse_lnm(pdf_bytes, matcher):
    """Parse the D1 LNM for one AOR. Returns (aton, hazards).

    aton:    [{"llnr","name","status","kind"}] -- discrepancy rows whose aid
             NAME matches the AOR (name-matching is required: e.g. "Buzzards
             Bay Entrance Light" sits under the non-AOR "EEZ" section header)
    hazards: [{"place","categories"}] -- AOR place-sections carrying
             hazard-category MSI notices
    """
    lines = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pg in pdf.pages:
            for ln in (pg.extract_text() or "").splitlines():
                lines.append(ln.strip())

    aton = []
    in_disc, kind = False, ""
    for s in lines:
        if not s:
            in_disc = False
            continue
        mk = DISC_KIND_RE.search(s)
        if mk and len(s) < 60:
            kind = mk.group(1).title()
            in_disc = False
            continue
        if DISC_HEADER_RE.match(s):
            in_disc = True
            continue
        if in_disc:
            m = DISC_ROW_RE.match(s)
            if m:
                name = m.group("name").strip()
                if matcher.match(name) and not WINDFARM_RE.search(name):
                    aton.append({"llnr": clean_llnr(m.group("llnr")), "name": name,
                                 "status": m.group("status").strip(),
                                 "kind": kind or "Federal"})
                continue
            in_disc = False  # first non-row line ends the table

    hazards, seen = [], set()
    for place, body in split_place_sections(lines):
        if not matcher.match(place):
            continue
        text = " ".join(body)
        cats = sorted({norm_hazard_cat(c) for c in HAZARD_CAT_RE.findall(text)} - {""})
        if not cats:
            continue
        key = place.lower()
        if key in seen:
            continue
        seen.add(key)
        hazards.append({"place": place, "categories": cats})

    return aton, hazards


def fetch_lnm():
    """Locate + download the current D1 LNM PDF. Returns (pdf_bytes, url)."""
    pdf_url = None
    # Strategy 1: GovDelivery RSS (carries the navcen PDF link; bypasses WAF)
    try:
        feed = feedparser.parse(NAVCEN_LNM_RSS)
        for entry in feed.entries:
            candidates = [entry.get("link", "")]
            if entry.get("summary"):
                candidates.append(entry["summary"])
            for c in entry.get("content", []):
                if c.get("value"):
                    candidates.append(c["value"])
            for text in candidates:
                for m in re.finditer(r'https?://[^\s"<>]+\.pdf', text, re.IGNORECASE):
                    url = m.group(0)
                    if "navcen" in url.lower() and ("lnm" in url.lower() or NAVCEN_D1_RE.search(url)):
                        pdf_url = url
                        break
                if pdf_url:
                    break
            if pdf_url:
                break
    except Exception as e:
        print(f"WARN: LNM RSS failed: {e}", file=sys.stderr)

    # Strategy 2: direct URL construction (current + previous week, both cases)
    if not pdf_url:
        from datetime import date
        today = date.today()
        week, year = today.isocalendar()[1], today.year
        for w in (week, week - 1):
            if w < 1:
                continue
            for stem in (f"LNM01{w:02d}{year}", f"lnm01{w:02d}{year}"):
                url = f"https://www.navcen.uscg.gov/sites/default/files/pdf/lnms/{stem}.pdf"
                try:
                    if requests.head(url, headers=UA, timeout=10, allow_redirects=True).status_code == 200:
                        pdf_url = url
                        break
                except Exception:
                    continue
            if pdf_url:
                break

    if not pdf_url:
        raise RuntimeError("could not locate the current D1 LNM PDF")
    r = requests.get(pdf_url, headers=UA, timeout=60)
    r.raise_for_status()
    return r.content, pdf_url


def main():
    ap = argparse.ArgumentParser(description="D1 LNM -> per-AOR JSON heads-up")
    ap.add_argument("--all", action="store_true", help="run every AOR in aors.json")
    ap.add_argument("--aor", help="run a single AOR id")
    ap.add_argument("--pdf-file", help="parse a local LNM PDF instead of fetching (testing)")
    ap.add_argument("--out", default="data", help="output directory (default: data/)")
    args = ap.parse_args()

    root = Path(__file__).parent
    configs = json.loads((root / "aors.json").read_text(encoding="utf-8"))
    if args.aor:
        configs = [c for c in configs if c["id"] == args.aor]
        if not configs:
            sys.exit(f"unknown AOR id: {args.aor}")
    elif not args.all:
        sys.exit("pass --all or --aor <id>")

    if args.pdf_file:
        pdf_bytes, lnm_url = Path(args.pdf_file).read_bytes(), f"file:{args.pdf_file}"
    else:
        pdf_bytes, lnm_url = fetch_lnm()
    print(f"LNM: {lnm_url} ({len(pdf_bytes)//1024} KB)")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index = {"generated_utc": generated, "lnm_source": lnm_url, "aors": []}

    for cfg in configs:
        matcher = AorMatcher(cfg["keywords"], cfg.get("excludes", []))
        aton, hazards = parse_lnm(pdf_bytes, matcher)
        doc = {
            "aor": cfg["id"],
            "label": cfg["label"],
            "generated_utc": generated,
            "lnm_source": lnm_url,
            "aton": aton,
            "hazards": hazards,
        }
        path = out_dir / f"{cfg['id']}.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        index["aors"].append({"id": cfg["id"], "label": cfg["label"],
                              "aton": len(aton), "hazards": len(hazards)})
        print(f"  {cfg['id']}: {len(aton)} ATON, {len(hazards)} hazard locations -> {path}")

    (out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
