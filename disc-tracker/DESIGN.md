# Disc Tracker - design contract

A PriceCharting-style price tracker for disc golf discs. Retail stores that run
Shopify are scraped daily from the public `/products.json` endpoint, each
listing is identified (manufacturer / mold / plastic / edition), prices are
stored change-only in SQLite, and a static dashboard renders price history.

Python 3.12+, deps: `httpx`, `rapidfuzz` (dev: `pytest`). No other runtime deps.
Run everything as `python -m disctracker <command>` from `disc-tracker/`.

## 1. Layout

```
disc-tracker/
  stores.json                 store list (id, name, base_url, currency, enabled, collection?)
  data/discs.db               SQLite, committed to git
  disctracker/
    models.py                 RawProduct, RawVariant, ParsedListing, disc_key, key_slug   (DONE)
    db.py                     schema + record_products/save_parse                          (DONE)
    shopify.py                fetch + check store                                           (section 3)
    parser.py                 title -> ParsedListing                                        (section 4)
    data/molds.json           mold seed list, data/plastics.json, data/manufacturers.json   (section 4)
    export.py                 DB -> static JSON for the site                                (section 5)
    cli.py / __main__.py      commands                                                      (DONE)
  site/                       static dashboard (index.html, app.js, style.css) + site/data/ (generated)
  tests/                      pytest, offline only (fixtures under tests/fixtures/)
../.github/workflows/disc-tracker.yml   daily cron: scrape -> parse -> export -> commit
```

## 2. Data model

See `db.py` for the schema. Key facts:
* money is integer cents in the DB; the JSON exports use dollars (number, 2 dp).
* `observations` are change-only per variant. To know the state of a variant on
  date D: take its latest observation with `observed_on <= D`; if none, the
  variant did not exist yet; if that row has `gone=1`, the variant was delisted.
* A "scrape date" is any `runs.observed_on` with `status='ok'` for that store.
  Daily series must only use dates where the store's run was `ok`, and must
  only count a store on dates it has an ok run (do not forward-fill across a
  store outage).
* Disc identity = `disc_key` = `manufacturer|mold|plastic|edition|player`,
  lowercase, 5 fields always (empties kept). `key_slug` makes it file-safe.
* `listings.condition` is `new` or `used`; every price series is split by it.
* `status`: `matched` (in the disc index), `review` (low confidence - counted in
  stats, not shown as discs), `ignored` (bags, baskets, apparel, ...), `unparsed`.

## 3. shopify.py (public API)

```python
def fetch_store(store: dict, client: httpx.Client | None = None, delay: float = 1.0,
                max_pages: int = 100, respect_robots: bool = True) -> list[RawProduct]
def check_store(store: dict, client: httpx.Client | None = None) -> dict
    # {"ok": bool, "detail": str, "sample_count": int}
class StoreError(Exception)
```
* GET `{base_url}/products.json?limit=250&page=N` (or
  `{base_url}/collections/{collection}/products.json` when `store["collection"]`).
  Stop when a page returns fewer than 250 products or an empty list.
* Prices are decimal strings ("19.99") -> integer cents without float error.
  `compare_at_price` may be null/"0.00" -> None. `available` is a bool on each variant.
* Identify politely: User-Agent `disc-tracker/1.0 (+https://github.com/mrcactusjones/mrcactusjones.github.io)`,
  `delay` seconds between pages, honour `Retry-After` on 429, retry 5xx/timeouts
  with exponential backoff (max 3 retries), 30s timeout.
* Check `{base_url}/robots.txt` once per store; if `/products.json` is disallowed
  for our UA (or `*`), raise `StoreError`. A missing/unreadable robots.txt means allowed.
* Any failure that would make the result incomplete (a page fails after retries,
  invalid JSON, duplicate page loop, max_pages hit while still full) MUST raise
  `StoreError` - never return a partial list, because `record_products` treats the
  list as complete and marks everything else gone.
* Product `url` = `{base_url}/products/{handle}`. Tags may be a list or a
  comma-separated string - normalise to a list of stripped strings.

## 4. parser.py (public API)

```python
PARSER_VERSION: int          # bump whenever rules/data change; CLI re-parses stale rows
def parse_listing(title, vendor="", product_type="", tags=()) -> ParsedListing
def parse_weight(text: str) -> int | None   # "173g" -> 173; "170-175g" -> None
```
* `parse_weight`: single weight "173g"/"173 g" -> 173; a range "170-175g" -> None
  (ranges are not a weight); implausible values (outside 100-200) -> None.
* Titles are noisy: `"Innova Star Destroyer 175g Ricky Wysocki Tour Series 2015 OOP 9/10"`,
  `"Champion Roc3"`, `"[Used] DX Leopard3 - 168g"`, `"Latitude 64 Opto River"`.
* Resolve manufacturer from `vendor` first, then title text, via
  `data/manufacturers.json` (canonical name + aliases).
* Resolve mold with exact normalised match first, then `rapidfuzz` (WRatio or
  token ratio) against the manufacturer's molds, falling back to all molds when
  manufacturer is unknown. Remember short/ambiguous mold names (Fuse, Wraith,
  Roc, Aviar...) - require word-boundary matches, never substring-of-word.
  Handle spelling variants ("Roc 3" == "Roc3", "Buzzz" == "Buzzz").
* Plastic from `data/plastics.json` (per manufacturer, with aliases, e.g.
  Innova "Star", "G-Star"/"GStar", "Champion", "DX", "Pro", "KC Pro";
  Discraft "ESP", "Z", "Jawbreaker", "Titanium", "Big Z"...). Longest match wins.
* Edition from a controlled vocabulary: `tour series`, `team series`, `first run`,
  `signature series`, `limited edition`, `prototype`, `factory second`, `glow`
  (only when it is a distinct edition), `misprint`, `ghost`... author a sensible
  list in `data/` and document it. `player` = name following/preceding Tour/Team/Signature
  markers when present (title-case, strip weights/years). `year` = 4-digit 1990-2035.
* Condition: `used` if title/tags/product_type contain used/pre-owned/second-hand/
  "beat in"/ "sleepy"/ a "N/10" grade; grade parsed from "9/10" or "grade 8".
  Otherwise `new`.
* Flags (list of strings): `oop`, `ink`, `dyed`, `signed`, `prototype`, `stamped_error`.
* `ignored`: product types/tags/titles for bags, baskets, towels, apparel, hats,
  shirts, backpacks, stickers, gift cards, mini markers, accessories, `Basket`.
* Confidence 0..1. `matched` needs a mold match score >= 0.85 AND a resolved
  manufacturer. Otherwise `review` if a mold-ish candidate exists, else `unparsed`.
* Must be deterministic and pure (no network, no I/O after import-time data load).

## 5. export.py (public API)

```python
def export_site(conn, out_dir: Path, today: str) -> dict   # returns stats dict
```
Writes (UTF-8, `json.dump(..., separators=(",", ":"), sort_keys=True)`):

`out_dir/index.json`
```json
{"generated_at": "2026-10-08", "currency": "USD",
 "stores": [{"id": "...", "name": "...", "base_url": "...", "last_ok": "2026-10-08"|null}],
 "stats": {"listings": 0, "matched": 0, "review": 0, "ignored": 0, "unparsed": 0, "discs": 0, "stores": 0},
 "discs": [{"key": "innova|destroyer|star||", "slug": "innova-destroyer-star",
            "manufacturer": "Innova", "mold": "Destroyer", "plastic": "Star", "edition": "", "player": "",
            "disc_type": "Distance Driver",
            "new":  {"min": 17.99, "median": 18.49, "stores_in_stock": 3, "stores_listing": 4,
                     "change_7d": -0.02, "change_30d": null} | null,
            "used": {...same shape...} | null,
            "last_seen": "2026-10-08"}]}
```
Display names (manufacturer/mold/plastic) come from the most common casing in
`listings`. Only `status='matched'`, not-gone listings with at least one
in-stock variant count toward `min`/`median`/`stores_in_stock`;
`stores_listing` counts stores with a non-gone listing. A disc with no live
listings but with history is still exported (`new`/`used` may be null; prices
use the last known data in `history`). `change_Nd` = fractional change of the
daily `min` price vs the nearest series point at or before `today - N days`
(null if the series does not reach back that far). Discs sorted by
`manufacturer, mold, plastic`.

`out_dir/history/<slug>.json`
```json
{"key": "...", "slug": "...", "manufacturer": "...", "mold": "...", "plastic": "...",
 "edition": "", "player": "", "disc_type": "",
 "series": {"new": [{"date": "2026-10-01", "min": 17.99, "median": 18.49, "stores_in_stock": 3}],
            "used": []},
 "listings": [{"store": "Dynamic Discs", "store_id": "dynamic-discs", "title": "...", "url": "...",
               "condition": "new", "weight_g": 175, "price": 17.99, "compare_at": null,
               "available": true, "last_seen": "2026-10-08"}]}
```
`series` is one point per scrape date (dates where any store had an ok run),
computed per date from forward-filled variant state, counting only in-stock,
non-gone variants of matched listings; a date with no in-stock variant is
omitted (no zero points). `listings` = live (not gone) variants, cheapest first.
Slug collisions between distinct keys are impossible by construction
(`key_slug`) but guard anyway by appending `-2`.

The exporter first deletes stale `history/*.json` files that no longer
correspond to an exported disc.

## 6. site/ (static dashboard)

Plain HTML/CSS/JS, no build step, served by GitHub Pages at
`/disc-tracker/site/` (relative URLs only; `fetch("data/index.json")`).
Chart.js loaded from a pinned CDN URL (cdnjs, `chart.js@4.4.x` is fine).
* Home: search box (fuzzy-ish: all tokens must appear), filters (manufacturer,
  disc type, condition new/used, in-stock only), sortable table/grid of discs
  with min price, store count, 7d/30d change badges (green down / red up).
* Disc page: `#/disc/<slug>` hash route; line chart of min and median over time
  (new vs used toggle), table of current listings with links to the store, stats.
* Header shows `generated_at` and the matched/review counts; an honest
  "no data yet" empty state when `index.json` has zero discs.
* Responsive down to 360px, dark/light via `prefers-color-scheme`, keyboard
  accessible, no inline event handlers, all dynamic text inserted with
  `textContent` (never `innerHTML` with scraped strings - titles are untrusted).

## 7. CLI (cli.py, done)

`check-stores`, `scrape [--store ID]`, `parse [--all]`, `export`, `run` (scrape+parse+export),
global `--db`, `--stores`, `--out`. A store failure never aborts the other stores; the
process exits 0 if at least one store succeeded, 1 if every enabled store failed.

## 8. Ground rules

* Offline tests only: no test may touch the network. Fixtures are hand-written
  Shopify-shaped JSON in `tests/fixtures/`.
* Be a good citizen: delay between requests, robots.txt, honest User-Agent.
* Untrusted input: never `eval`, never HTML-inject scraped strings.

## 9. Implementation notes (where the code is stricter than, or adds to, the contract)

**Scraper (`shopify.py`)** - robots.txt is evaluated with an own RFC 9309 matcher (longest
match wins, `*` and `$`, BOM-safe) against both the bare path and the query-bearing URL for
every page, because `urllib.robotparser` behaves differently across Python versions. A
`Crawl-delay` can only raise our delay. `base_url` must be a plain http(s) origin. A redirect
that drops `limit`/`page` raises `StoreError` (it would otherwise return a short default page
as "complete"). Malformed products (missing id/handle/price) raise instead of being skipped.
Product URLs use the percent-encoded handle. 408/429/5xx are retried; a `Retry-After` over
120 s fails the store.

**Safety nets in `cli.py`** - a scrape is refused (run recorded as `error`, nothing written)
if it returns zero products or fewer than half of the previous successful run's count
(previous >= 20). Stores that are no longer enabled in `stores.json` have their listings marked
gone on the next full scrape. `parse` and `export` refuse to run against a missing database.

**Parser (`parser.py`)** - fuzzy fallback uses `rapidfuzz.fuzz.ratio`, not WRatio, so a
mold can never match inside a longer word. `matched` is stricter than the contract: ambiguous
cases (brand mismatch, two molds in a title, mold + unknown variant token such as `GT`/`SS`)
become `review`. Multi-disc/non-regulation words (`set`, `pack`, `combo`, `bundle`, `mini`,
`mystery`) make a listing `ignored`. Edition vocabulary is in `data/editions.json` (lowercase,
the site title-cases it). The seed mold list is ~220 entries written from memory and is not
verified against any reference; ten manufacturers have plastics but no molds yet.

**Weights** - `variants.weight_g` comes from the variant title, then the product title.
Shopify's `grams` field is shipping weight and is not used.

**Exporter (`export.py`)** - a per-condition block is `null` only when no live priced listing of
that condition exists; if listings exist but none are in stock the block is present with
`stores_in_stock: 0` and the last listed prices. Median is over in-stock variants. Series are
dense (one point per scrape date), roughly 35 MB per 1,250 discs per year, so revisit before
the history gets large. `index.json` is written before stale history files are deleted. Series
reflect the *current* parse, so re-parsing rewrites past chart points.

**Site** - Chart.js is injected lazily from a pinned cdnjs URL (no SRI hash yet); if it fails to
load, the page falls back to the data table. Browser checks in `tests/test_site.py` skip when
node/playwright-core/Chromium are missing (as in CI).

## 10. eBay (marketplace source) - binding contract for the eBay work

eBay is a second kind of source: `stores.kind = 'marketplace'` (retail Shopify stores are
`'retail'`). It is collected with the official **Browse API** (active, fixed-price listings
= asking prices). Browse API cannot return sold prices; the `sales` table is the hook for that.

### 10.1 Data model (DONE in `db.py`, do not change)
* One eBay item = one `listings` row (`store_id='ebay'`, `product_id` = legacy item id) with
  one `variants` row (`variant_id` = same id, `available=1`). Price = item price only (shipping
  excluded). Listings gain `ends_at` (ISO string or "") and `last_query` (query key that last returned it).
* `db.record_products(..., complete=False)` for eBay: a search only sees a slice, so absence is
  NOT disappearance. After a query ran to completion call
  `db.record_query_run(...)` then `db.expire_missing(conn, 'ebay', query_key, observed_on)`.
* `expire_missing` marks listings last returned by that query and unseen today as gone, and, if
  they vanished before `ends_at`, records a `sales` row (`source='inferred_disappeared'`,
  `confidence='low'`, price = last listed price, `sold_on` = last_seen). Sellers also delist items
  without a sale, so this is an upper-bound signal and must always be labelled "inferred".
  Relisting cancels the inferred sale. `db.record_sale(..., source='marketplace_insights',
  confidence='confirmed')` is the hook for real sold data if eBay ever approves Marketplace
  Insights; no client for it exists yet.
* `RawProduct` has `ends_at` and `query_key` for this.

### 10.2 `ebay.py` (public API)
```python
EBAY_STORE = {"id": "ebay", "name": "eBay", "base_url": "https://www.ebay.com",
              "currency": "USD", "kind": "marketplace"}
class EbayError(Exception)
class EbayAuthError(EbayError)    # missing/rejected credentials -> abort, record nothing
class EbayQuotaError(EbayError)   # rate/daily limit hit -> stop the run gracefully, keep what we have

def load_credentials(env=None) -> tuple[str, str] | None
    # EBAY_CLIENT_ID / EBAY_CLIENT_SECRET from env (default os.environ); empty/whitespace = missing -> None.
def build_queries(molds=None, plastics=None) -> list[tuple[str, str]]
    # [(query_key, query_text)] one per (mold, plastic of that mold's manufacturer) from
    # disctracker/data/molds.json + plastics.json (a mold of a manufacturer with no plastics gets
    # only the generic plastics list; never an empty plastic). query_text = "<plastic> <mold> <manufacturer>"
    # (eBay ANDs the words). query_key = "manufacturer|mold|plastic" lowercased, unique, stable.
    # Deterministic order. Plastic strings that are a single character or purely numeric still
    # go through (e.g. "Z", "400").
class EbayClient:
    def __init__(self, client_id, client_secret, http: httpx.Client | None = None, *,
                 marketplace_id="EBAY_US", category_ids=("184356",), base_url="https://api.ebay.com",
                 sleeper=time.sleep)
    def search(self, query: str, query_key: str, max_calls: int) -> SearchResult
@dataclass
class SearchResult: items: list[RawProduct]; total: int; complete: bool; calls: int
def collect(conn, client: EbayClient, observed_on: str, call_budget: int = 3500,
            queries=None, weight_parser=None, log=print) -> dict
    # orchestrates one run: upsert EBAY_STORE, start/finish a `runs` row, order queries with
    # db.order_queries, for each: search -> db.record_products(complete=False) ->
    # db.record_query_run -> (if complete) db.expire_missing. Stops when the call budget is
    # spent ("budget"), on EbayQuotaError ("quota"), or when queries are exhausted ("done").
    # EbayAuthError propagates BEFORE anything is recorded. A single failing query is logged
    # and skipped. Returns {"queries_run","queries_complete","calls","items_seen","new_listings",
    # "gone","inferred_sales","skipped_currency","stopped"}.
```
Browse API facts to implement (verify against these, do not invent others):
* Token: `POST {base}/identity/v1/oauth2/token`, header `Authorization: Basic base64(id:secret)`,
  form `grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope`; response
  `access_token`, `expires_in` (seconds). Cache until ~60 s before expiry; on a 401 from search
  refresh once, then raise `EbayAuthError`. 400/401 from the token endpoint -> `EbayAuthError`.
* Search: `GET {base}/buy/browse/v1/item_summary/search` with headers
  `Authorization: Bearer <token>`, `X-EBAY-C-MARKETPLACE-ID: EBAY_US`; params `q`, `category_ids`,
  `limit=200`, `offset`, `filter=buyingOptions:{FIXED_PRICE}` (no auctions: their price is a bid,
  not an asking price), `fieldgroups=MATCHING_ITEMS`? (omit unless needed). Response:
  `total`, `limit`, `offset`, `next`, `itemSummaries[]` (absent when 0 results). Each summary has
  `itemId` ("v1|<legacyId>|<variationId or 0>"), `title`, `price{value,currency}`, `condition`,
  `conditionId`, `itemWebUrl`, `itemEndDate`, `buyingOptions[]`, `seller{username}`.
  The API will not page past offset+limit > 10,000.
* `complete` = every one of `total` items was fetched (total <= 10,000, no call budget cutoff, no
  page error). Only complete queries may expire listings.
* Mapping: `product_id` = int legacy id (variation ids: keep one item per legacy id, use the
  variation id as `variant_id` only if you can do so without duplicating the listing; otherwise skip
  with a counter). `url` = `itemWebUrl` stripped to scheme+host+path (drop tracking query params, only
  http/https). `title` as is. `vendor` = "". `product_type` = condition text. `tags` includes exactly
  one of `condition:new` (conditionId 1000 or 1500) / `condition:used` (any other known condition;
  omit the tag if condition is unknown). `price_cents` via Decimal (no float). Items whose currency
  is not the store currency are skipped and counted.
* Politeness/safety: honour `Retry-After`; 429 (after one retry) -> `EbayQuotaError`; 5xx/timeouts
  retried with backoff (max 3) then that query fails; never log or put credentials/tokens in
  exception text; credentials come only from the environment, never from files or the DB.
* Daily limit: Browse API's default is 5,000 calls/day; `call_budget` default 3,500 leaves headroom.
  ~2,900 queries exist so a full rotation takes about a day; `db.order_queries` makes the next
  run resume with the stalest queries first.

### 10.3 CLI (cli.py, DONE)
`scrape-ebay [--budget N]`; `run` also runs eBay when credentials are set (skips it with a
message otherwise).

### 10.4 Export / site additions (additive to sections 5 and 6)
* `index.json` `stores[]` and history `listings[]` gain `"kind"` (`"retail"|"marketplace"`; listings
  call it `store_kind`).
* `index.json` `discs[]` gain `"sales_30d": {"confirmed": {"count": n, "median": 19.5} | null,
  "inferred": {"count": n, "median": 19.5} | null}` (sale date within 30 days of `today`).
  `stats` gains `"sales_confirmed"` and `"sales_inferred"` totals.
* history file gains `"sales": [{"date": "2026-10-01", "price": 19.5, "condition": "used",
  "source": "inferred_disappeared"|"marketplace_insights", "confidence": "low"|"confirmed",
  "store": "eBay", "url": "..."}]` (newest first, at most 100, for listings whose current parse
  has this disc_key) and, ONLY when the disc has marketplace listings with data,
  `"series_by_kind": {"retail": {"new": [...], "used": [...]}, "marketplace": {"new": [...], "used": [...]}}`
  using the same point shape as `series` (the existing `series` stays the all-stores aggregate).
* Site: a "marketplace" badge on marketplace listings; an All / Retail / Marketplace switch on the
  chart when `series_by_kind` exists; a "Recent sales" table whose inferred rows are labelled
  "Likely sold (inferred, low confidence - the seller may have delisted it)", never presented as
  confirmed sales; a home-page filter to include/exclude marketplace prices.
* A disc whose only live price is from eBay is still a disc; asking prices from a marketplace
  are not comparable to retail MSRP, so the UI must make the source obvious.

### 10.5 Parser additions
eBay titles are seller-written keyword soup ("NEW Innova Star Destroyer 175g Disc Golf Driver
Max Distance!!"). Lots, pairs and bundles ("lot of 3", "3x", "(2) discs", "set of", "bundle",
"pick your disc", "mystery") must never become a single-disc price (-> `ignored`, or `review`
when unsure). `condition:new` / `condition:used` tags are honoured (used wins when either the
tag or the title says used; "unthrown"/"never thrown"/"NIB" mean new). Bump `PARSER_VERSION`.
