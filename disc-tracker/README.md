# Disc Tracker

A PriceCharting-style price tracker for disc golf discs. It scrapes retail stores
that run Shopify, works out which disc each listing is (manufacturer, mold,
plastic, edition), stores prices over time, and publishes a static dashboard.

* Dashboard (GitHub Pages): `/disc-tracker/site/`
* Design contract and data shapes: [DESIGN.md](DESIGN.md)

## How it works

1. **scrape** - `GET {store}/products.json` (paginated, rate limited, honours
   robots.txt) for every store in `stores.json`.
2. **store** - SQLite (`data/discs.db`). Observations are change-only, so the
   file grows slowly and is committed to git after every run.
3. **parse** - rule-based title parser with fuzzy mold matching
   (`disctracker/parser.py`, seed data in `disctracker/data/`). Low-confidence
   listings are kept but marked `review` and stay out of the disc index.
4. **export** - the database is turned into static JSON in `site/data/`.
5. **dashboard** - plain HTML/JS with Chart.js reads that JSON. No server.

A GitHub Actions workflow (`.github/workflows/disc-tracker.yml`) runs the tests
on every change and does the daily scrape-parse-export-commit on the default branch.

## Quick start

```bash
cd disc-tracker
pip install -r requirements-dev.txt
python -m pytest -q

python -m disctracker check-stores   # which stores in stores.json actually work?
python -m disctracker run            # scrape + parse + export
python -m http.server -d . 8000      # open http://localhost:8000/site/
```

Individual steps: `scrape [--store ID]`, `parse [--all]`, `export`.

## eBay (optional)

eBay is collected through the official Browse API: active fixed-price listings, one search per
mold and plastic (about 3,300 queries; a full rotation takes a day or more because of the free
5,000-calls-per-day limit, and each run resumes with the stalest queries).

1. Create a free developer account at developer.ebay.com and make a **Production** keyset.
2. Add the App ID and Cert ID as **Actions** repository secrets named `EBAY_CLIENT_ID` and
   `EBAY_CLIENT_SECRET` (Settings > Secrets and variables > Actions - not the Codespaces tab).
   Locally, export the same two environment variables.
3. `python -m disctracker scrape-ebay` (or `run`, which includes eBay whenever the keys are set).

These are **asking prices**, shown as "marketplace" in the dashboard. eBay's API does not return
sold prices; when a fixed-price listing disappears before its end date the tracker records a
low-confidence "likely sold" entry (the seller may simply have delisted it). Real sold data would
need eBay's Marketplace Insights API, which requires approval.

## Adding a store

Add an entry to `stores.json` (`id`, `name`, `base_url`, optional `collection`
to restrict to a single collection, e.g. only discs). Then run
`python -m disctracker check-stores`. A store that fails never stops the others.
The seed list is **unverified**: run `check-stores` and remove what does not work.

## Limits worth knowing

* Asking prices only. Sold prices need eBay's restricted Marketplace Insights API;
  until then "likely sold" entries are inferred and low confidence.
* Shopify stores plus eBay (US marketplace). Prices are assumed to be in the store's
  `currency` (default USD).
* The mold list is a hand-written seed, not complete. New releases will show up as
  `review`/`unparsed` until added to `disctracker/data/molds.json`; bump
  `PARSER_VERSION` after editing so listings are re-parsed.
* The SQLite file is committed to git daily. If the repo gets heavy, squash history
  or move the database to a release asset / separate data branch.
