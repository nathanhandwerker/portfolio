# Tour availability scanner

Runs daily via `.github/workflows/tour-scanner.yml` (GitHub Actions cron), checks
https://stahlhouse.com/tours/ for newly listed tour dates, and opens/updates a
GitHub issue (labeled `tour-scanner`) when it finds something. Repo watchers get
GitHub's normal email notification for that issue — no extra secrets needed.

## How it works

1. Fetches the tours page.
2. Detects which booking widget (if any) the page embeds — FareHarbor, Bookeo,
   Checkfront, Peek Pro, Rezdy, Xola, Calendly, Acuity, TicketSpice, Eventbrite,
   Bokun, Regiondo — and records it in `state.json` for reference.
3. Pulls date-like tokens out of the visible text and common calendar
   attributes (`data-date`, `data-day`, `datetime`, `aria-label`, `title`),
   keeps the ones that parse as real future dates, and normalizes them to
   ISO `YYYY-MM-DD`.
4. Diffs that set against `known_dates` in `state.json` (committed back to the
   repo each run). New dates → issue opened/commented with the list.
5. If it can't parse any dates at all but the page's visible text changed
   since last run, it opens/comments a lower-key "check manually" issue
   instead of staying silent. This fires once per distinct change, not every
   day, so it won't spam.

## Known limitation

This was written without being able to load the live page — the sandbox it
was authored in has `stahlhouse.com` blocked by network policy — so the
provider list and date heuristics are best-effort, not verified against the
real markup.

**After the first couple of scheduled runs**, check the Action's logs
(`Providers detected: ...`, `Dates found on page: ...`) and `state.json`:

- If `providers_detected` names a widget (e.g. `fareharbor`) but availability
  actually lives behind that widget's own API/iframe (common — many of these
  render their calendar client-side via JS), the static scrape may see 0
  dates even though the page "has" a calendar. In that case the fallback
  full-page-hash diff still fires a "check manually" issue on any change,
  but for real per-date tracking you'd add a small provider-specific step to
  `scan.py` that calls that widget's public availability endpoint directly
  (open a PR — the diffing/issue-filing logic around `extract_dates()`
  doesn't need to change).
- If dates are being over- or under-matched, tighten/loosen the regexes and
  `DATE_ATTRS` list in `scan.py` based on what the real HTML looks like.

## Manual run

From this directory: `pip install -r requirements.txt && python scan.py --url https://stahlhouse.com/tours/ --state state.json`

Or trigger the workflow on demand from the Actions tab ("Run workflow").

## Changing the schedule

Edit the `cron` line in `.github/workflows/tour-scanner.yml` (times are UTC).
