# Intrinsic Value Portfolio Tracker

A small static site that displays the **Intrinsic Value podcast** portfolio and watchlist
(from The Investor's Podcast) in a clean, clickable way instead of the busy master spreadsheet.

## How to view it

Just open **`index.html`** in any browser — double-click it. No server, no build step, no internet
required (fonts load online but fall back to system fonts offline).

- **index.html** — Portfolio home: totals + interactive donut chart. Click any slice or holding
  to see Fair Value, Current Price, Margin of Safety, Target Last Updated, Avg Price, Shares,
  Gain, Weighting, Position Value, plus Model / Podcast / Newsletter links.
- **watchlist.html** — Shawn & Daniel's ranked Top 10, plus a searchable, sortable table of every
  company covered on the show.

## How to update the numbers each week

Everything the site shows lives in one file: **`data.js`**. Open it in any text editor and edit the
values — the site re-reads it automatically on refresh. No other files need to change.

- Numbers like `marginOfSafety`, `gain`, and `weight` are **decimals** (`0.15` = 15%).
- Prices are plain numbers (`342.09`).
- To add/remove a holding or a covered company, copy an existing `{ ... }` block and edit it.
- `holdings` = the owned portfolio (shows on the pie chart).
- `coverage` = every company valued on the pod (shows in the watchlist table).
- `watchRank` = the ranked Top 10 (matched to `coverage`/`holdings` by name automatically).

> Note: a couple of rows are mirrored exactly as they appear in the source spreadsheet and carry its
> quirks — e.g. **Booking Holdings** and **Nintendo** have a stale/units-mismatched price in the
> master file, so their margin of safety looks extreme. Edit those values in `data.js` to correct them.

## Files
```
index.html      Portfolio home page
watchlist.html  Watchlist + full coverage table
styles.css      Styling (dark editorial / podcast brand)
app.js          Chart, modal, table logic
data.js         >>> the only file you edit to update data <<<
```
