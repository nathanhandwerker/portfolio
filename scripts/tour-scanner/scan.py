#!/usr/bin/env python3
"""
Daily scanner for https://stahlhouse.com/tours/ that looks for newly listed
tour dates and reports results via GitHub Actions outputs.

The page's calendar (#espresso_calendar) is an Event Espresso "fullcalendar"
widget: classic jQuery FullCalendar, populated client-side via AJAX after
page load. Every day cell carries a data-date attribute whether or not a
tour runs that day, so scraping raw HTML (even after rendering) can't tell
availability from filler. Instead this renders the page with a real browser
(Playwright/Chromium), pages the calendar forward a few months using its own
"next" button so it fetches those months' events, then reads the events
straight out of FullCalendar's client-side event cache via
`jQuery('#espresso_calendar').fullCalendar('clientEvents')` - the same data
the widget itself uses, not a DOM heuristic.

Each event has a start date, title, and booking URL. The set of
(date, title) pairs is diffed against the previous run's state
(scripts/tour-scanner/state.json, committed back to the repo each run) to
find newly listed tour dates.

As a secondary signal, the page's full visible text is hashed; if it changes
while zero events are found (e.g. the widget markup changes and this needs
updating), a lower-key "check manually" issue fires instead of staying
silent - once per distinct change, not every run.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_STATE = {
    "known_events": [],
    "content_hash": None,
    "last_checked": None,
}

CALENDAR_ID = "espresso_calendar"
USER_AGENT = (
    "Mozilla/5.0 (compatible; TourAvailabilityScanner/1.0; "
    "+https://github.com/nathanhandwerker/portfolio)"
)

CLIENT_EVENTS_JS = """
(calendarId) => {
    try {
        const cal = jQuery('#' + calendarId);
        const evts = cal.fullCalendar('clientEvents');
        return evts.map(e => ({
            title: e.title || null,
            start: e.start ? e.start.format('YYYY-MM-DD') : null,
            end: e.end ? e.end.format('YYYY-MM-DD') : null,
            url: e.url || null,
        }));
    } catch (err) {
        return null;
    }
}
"""


def visible_text_hash(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def scrape(url: str, months_ahead: int):
    """Returns (events_or_None, content_hash). events is None if the
    calendar's JS event cache couldn't be read (page structure changed)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_selector(f"#{CALENDAR_ID} .fc-header-title h2", timeout=20000)

            for _ in range(months_ahead):
                page.click(f"#{CALENDAR_ID} .fc-button-next")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(600)

            events = page.evaluate(CLIENT_EVENTS_JS, CALENDAR_ID)
            content_hash = visible_text_hash(page.content())
        finally:
            browser.close()
    return events, content_hash


def event_key(event: dict) -> str:
    return f"{event.get('start')}::{event.get('title')}"


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
    parser.add_argument("--months-ahead", type=int, default=6)
    args = parser.parse_args()

    today = date.today()
    state = load_state(args.state)

    try:
        events, content_hash = scrape(args.url, args.months_ahead)
    except Exception as exc:  # noqa: BLE001 - surface any scrape failure to the workflow
        print(f"ERROR: failed to scrape {args.url}: {exc}", file=sys.stderr)
        write_github_output({"fetch_failed": "true"})
        return 1

    had_previous_run = bool(state.get("content_hash"))
    content_changed = content_hash != state.get("content_hash")
    scrape_broken = events is None

    known_events = {e["key"]: e for e in state.get("known_events") or []}
    current_events = []
    if not scrape_broken:
        for e in events:
            if not e.get("start"):
                continue
            entry = {
                "key": event_key(e),
                "date": e["start"],
                "title": e.get("title"),
                "url": e.get("url"),
            }
            current_events.append(entry)

    current_by_key = {e["key"]: e for e in current_events}
    new_events = [
        e for key, e in current_by_key.items() if key not in known_events
    ]
    new_events.sort(key=lambda e: (e["date"], e["title"] or ""))

    has_new_dates = bool(new_events) and had_previous_run
    # Zero events is a legitimate, common state (e.g. no tours currently open
    # for booking) - only flag "check manually" when the JS event cache
    # itself couldn't be read, not just because it came back empty.
    unparsed_change = scrape_broken and content_changed

    state["last_checked"] = today.isoformat()
    state["content_hash"] = content_hash
    if not scrape_broken:
        state["known_events"] = sorted(current_events, key=lambda e: (e["date"], e["title"] or ""))
    save_state(args.state, state)

    new_dates_lines = [
        f"{e['date']} — {e['title'] or 'Untitled tour'}" + (f" ({e['url']})" if e.get("url") else "")
        for e in new_events
    ]

    print(f"Scrape broken (JS event cache unreadable): {scrape_broken}")
    print(f"Events found on calendar: {len(current_events)}")
    print(f"New events since last run: {new_dates_lines or 'none'}")
    print(f"Unparsed content change: {unparsed_change}")

    write_github_output(
        {
            "fetch_failed": "false",
            "has_new_dates": "true" if has_new_dates else "false",
            "new_dates": "\n".join(new_dates_lines),
            "num_events_found": str(len(current_events)),
            "unparsed_change": "true" if unparsed_change else "false",
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
