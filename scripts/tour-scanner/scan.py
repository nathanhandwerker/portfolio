#!/usr/bin/env python3
"""
Daily scanner for https://stahlhouse.com/tours/ that looks for newly listed
tour dates and reports results via GitHub Actions outputs.

How it decides "new dates":
1. Detects known booking-widget providers embedded in the page (FareHarbor,
   Bookeo, Checkfront, Peek Pro, Rezdy, Xola, Calendly, Acuity, TicketSpice,
   Eventbrite) so the state file documents what's actually powering the page.
2. Pulls every date-like token out of the visible text and common calendar
   attributes (data-date, data-day, datetime, aria-label, title), keeps only
   ones that parse as a real future date, and normalizes them to ISO
   (YYYY-MM-DD).
3. Compares that set against the dates recorded on the previous run.

If nothing date-like can be parsed at all (e.g. the widget renders its
calendar via an API call this static scrape can't see), it falls back to
hashing the page's visible text so at least a "the page changed, check
manually" signal still fires - once per distinct change, not every day.

This was written without being able to load the live page (the sandbox this
was authored in has stahlhouse.com blocked by network policy), so the
provider list and date-attribute heuristics are best-effort. See README.md
in this directory for how to verify/tune it against the real page.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

DEFAULT_STATE = {
    "known_dates": [],
    "content_hash": None,
    "last_checked": None,
    "providers_detected": [],
}

# Substrings that show up in <script>/<iframe> src or inline HTML when a page
# embeds one of these booking widgets.
PROVIDER_SIGNATURES = {
    "fareharbor": ["fareharbor"],
    "bookeo": ["bookeo"],
    "checkfront": ["checkfront"],
    "peek": ["peek.com", "peekpro"],
    "rezdy": ["rezdy"],
    "xola": ["xola.com", "xola-widget"],
    "calendly": ["calendly"],
    "acuity": ["acuityscheduling"],
    "ticketspice": ["ticketspice"],
    "eventbrite": ["eventbrite"],
    "bokun": ["bokun.io"],
    "regiondo": ["regiondo"],
}

DATE_ATTRS = ["data-date", "data-day", "datetime", "aria-label", "title"]

MONTH_TEXT_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}\b",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

MAX_YEARS_OUT = 3
USER_AGENT = (
    "Mozilla/5.0 (compatible; TourAvailabilityScanner/1.0; "
    "+https://github.com/nathanhandwerker/portfolio)"
)


def fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def detect_providers(html: str) -> list:
    lowered = html.lower()
    found = []
    for name, signatures in PROVIDER_SIGNATURES.items():
        if any(sig in lowered for sig in signatures):
            found.append(name)
    return sorted(found)


def _plausible_future_date(dt: date, today: date) -> bool:
    if dt < today:
        return False
    if dt.year > today.year + MAX_YEARS_OUT:
        return False
    return True


def _try_parse(token: str, today: date):
    try:
        parsed = dateparser.parse(token, fuzzy=False, default=None)
    except (ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    dt = parsed.date()
    if not _plausible_future_date(dt, today):
        return None
    return dt.isoformat()


def extract_dates(soup: BeautifulSoup, today: date) -> set:
    found = set()

    for tag in soup.find_all(True):
        for attr in DATE_ATTRS:
            value = tag.get(attr)
            if not value:
                continue
            for match in ISO_DATE_RE.finditer(value):
                iso = _try_parse(match.group(0), today)
                if iso:
                    found.add(iso)
            for match in MONTH_TEXT_RE.finditer(value):
                iso = _try_parse(match.group(0), today)
                if iso:
                    found.add(iso)

    page_text = soup.get_text(separator=" ")
    for match in MONTH_TEXT_RE.finditer(page_text):
        iso = _try_parse(match.group(0), today)
        if iso:
            found.add(iso)
    for match in ISO_DATE_RE.finditer(page_text):
        iso = _try_parse(match.group(0), today)
        if iso:
            found.add(iso)

    return found


def visible_text_hash(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_STATE)
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    merged = dict(DEFAULT_STATE)
    merged.update(state)
    return merged


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def write_github_output(outputs: dict) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        for key, value in outputs.items():
            print(f"{key}={value}")
        return
    with open(gh_output, "a", encoding="utf-8") as f:
        for key, value in outputs.items():
            if "\n" in str(value):
                delimiter = f"EOF_{key}_EOF"
                f.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                f.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()

    today = date.today()
    state = load_state(args.state)

    try:
        html = fetch(args.url)
    except requests.RequestException as exc:
        print(f"ERROR: failed to fetch {args.url}: {exc}", file=sys.stderr)
        write_github_output({"fetch_failed": "true"})
        return 1

    soup = BeautifulSoup(html, "html.parser")
    providers = detect_providers(html)
    current_dates = extract_dates(BeautifulSoup(html, "html.parser"), today)
    content_hash = visible_text_hash(soup)

    known_dates = set(state.get("known_dates") or [])
    content_changed = content_hash != state.get("content_hash")

    new_dates = sorted(current_dates - known_dates) if current_dates else []
    has_new_dates = bool(new_dates) and bool(state.get("content_hash"))
    unparsed_change = (not current_dates) and content_changed

    state["last_checked"] = today.isoformat()
    state["providers_detected"] = providers
    state["content_hash"] = content_hash
    if current_dates:
        state["known_dates"] = sorted(current_dates)
    save_state(args.state, state)

    print(f"Providers detected: {providers or 'none'}")
    print(f"Dates found on page: {len(current_dates)}")
    print(f"New dates since last run: {new_dates or 'none'}")
    print(f"Unparsed content change: {unparsed_change}")

    write_github_output(
        {
            "fetch_failed": "false",
            "has_new_dates": "true" if has_new_dates else "false",
            "new_dates": "\n".join(new_dates),
            "num_dates_found": str(len(current_dates)),
            "providers": ", ".join(providers) if providers else "none detected",
            "unparsed_change": "true" if unparsed_change else "false",
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
