# Tour availability scanner

Runs daily via `.github/workflows/tour-scanner.yml` (GitHub Actions cron), checks
https://stahlhouse.com/tours/ for newly listed tour dates, and opens/updates a
GitHub issue (labeled `tour-scanner`) when it finds something. Repo watchers get
GitHub's normal email notification for that issue — no extra secrets needed.

## How it works

The tours page's calendar (`#espresso_calendar`) is an Event Espresso
"fullcalendar" widget — classic jQuery FullCalendar, populated client-side
via AJAX after page load. Every day cell in the raw HTML carries a
`data-date` attribute whether or not a tour runs that day, so scraping HTML
text (even rendered) can't tell availability from filler.

Instead:

1. Renders the page with a real browser (Playwright + headless Chromium).
2. Clicks the calendar's own "next" button forward `--months-ahead` times
   (default 6) so it fetches each of those months' events via its normal
   AJAX calls.
3. Reads the events straight out of FullCalendar's client-side event cache —
   `jQuery('#espresso_calendar').fullCalendar('clientEvents')` — the same
   data the widget itself renders from, not a DOM/position heuristic. Each
   event has a start date, title, and booking URL.
4. Diffs the set of `(date, title)` pairs against `known_events` in
   `state.json` (committed back to the repo each run). New ones → issue
   opened/commented with the list.
5. If the JS event cache can't be read at all (the widget's markup or
   library changed) or events go to zero while the page's visible text still
   changed, it opens/comments a lower-key "check manually" issue instead of
   staying silent — once per distinct change, not every run.

## If Event Espresso changes its calendar widget

`CLIENT_EVENTS_JS` in `scan.py` calls the widget's own jQuery FullCalendar
API, so it should keep working across ordinary content changes (new tours,
new dates). It would need updating only if the site changes the calendar
library/plugin itself — the "check manually" issue mentioned above is the
signal to come look.

## Manual run

From this directory:

```
pip install -r requirements.txt
playwright install --with-deps chromium
python scan.py --url https://stahlhouse.com/tours/ --state state.json
```

Or trigger the workflow on demand from the Actions tab ("Run workflow").

## Changing the schedule

Edit the `cron` line in `.github/workflows/tour-scanner.yml` (times are UTC).
