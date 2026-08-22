# Grade Gap Tracker

Finds Pokémon cards where the graded price clears its costs **at a PSA 9** — the
downside case — rather than ranking by the PSA 10 multiple like every other
grading calculator. A card only earns "no brainer" if the 9 alone pays for the
raw copy, the grading fee, and the selling fees, on enough comps to believe.

Runs entirely on free tiers. No dependencies beyond the Python standard library.

## Quick start

```bash
cd gap-tracker
python3 run.py demo      # fake but plausible data + 21 days of history
python3 run.py serve     # http://127.0.0.1:8765/index.html
```

Everything works before you have an API key. When you're ready for real prices:

```bash
export PPT_API_KEY=...           # pokemonpricetracker.com
python3 run.py catalog           # free: builds the candidate universe
python3 run.py scan --provider ppt --dry-run   # see what it would spend
python3 run.py daily --provider ppt            # what cron should call
```

## The money math

`gapscan/econ.py` is the whole point; the rest is plumbing to feed it.

```
all_in      = raw × (1 + buy premium) + grading fee + submission shipping
net(price)  = price × (1 − marketplace fee) − shipping to buyer
floor       = net(PSA 9)  − all_in      ← the ranking column
upside      = net(PSA 10) − all_in
breakeven   = share of 10s needed for EV = 0, when the 9 loses money
```

Verdicts:

| verdict | meaning |
|---|---|
| `no_brainer` | the 9 clears the thresholds **and** the comps are deep enough to trust |
| `floor_positive` | the 9 makes money, but thin comps or below threshold |
| `ten_or_bust` | the 9 loses; only a 10 pays. `breakeven_p10` says what gem rate you'd need |
| `dead` | even a 10 doesn't cover costs |

The break-even figure assumes every non-10 comes back a 9, ignoring 8s and
below. It's a *lower bound* on the gem rate you need, not a forecast.

## Why it scans slowly

PokemonPriceTracker's free tier is 100 credits/day and a card with PSA data
costs 2 credits, so the ceiling is **~50 cards/day**. Discovery and watchlist
refresh compete for that pool, so:

- The watchlist refreshes **weekly**, not daily — graded comps are built from
  eBay completed listings and barely move day to day. Daily polling would burn
  the entire budget re-reading the same numbers.
- The watchlist takes at most half the budget (`watchlist_share`); whatever it
  doesn't claim rolls into discovery.
- Every quote is cached with a timestamp and never re-fetched inside its TTL.
  Credits are scarce; disk is free.

Expect roughly two weeks before the ranking is worth trusting. The dashboard
shows coverage (`612 / 2,000 scanned · oldest 34 days`) so you can see how
provisional it is.

## Layout

```
run.py                  CLI
gapscan/config.py       cost model, thresholds, budget (override in config.json)
gapscan/econ.py         the money math + verdicts
gapscan/catalog.py      free universe build from pokemontcg.io
gapscan/scan.py         budget-aware rolling scanner
gapscan/rank.py         ranking, snapshots, watchlist promotion
gapscan/providers/      mock (offline) and ppt (real) price sources
seeds/community.json    curated bootstrap: which sets/cards get credits first
fixtures/catalog.json   offline stand-in for pokemontcg.io so demo needs no network
index.html              dashboard; recomputes client-side as you move the sliders
data/                   git-ignored: cache, rankings, daily history
```

## Configuration

Drop a `config.json` next to `run.py` to override any default:

```json
{
  "econ": { "grading_fee": 25.0, "sale_fee_pct": 0.15 },
  "thresholds": { "min_floor_profit": 40.0, "min_sales_9": 8 },
  "budget": { "daily_credits": 100, "watchlist_size": 50 }
}
```

The dashboard sliders do the same thing live, without touching the data.

## Known gaps

- **The PPT adapter's field mapping is unverified.** Their API docs were
  unreachable when this was written, so `providers/ppt.py` pattern-matches keys
  instead of hard-coding paths — it should survive most response shapes. If it
  returns nothing, run `python3 run.py probe --card base1-4` to dump the raw
  response, then either fix the endpoint or pin exact paths in `EXPLICIT_PATHS`.
- **Set ids in `seeds/community.json` are unverified** against the live
  pokemontcg.io catalog. `run.py catalog` prints a warning naming any set id
  that returns zero cards.
- **PSA only.** PokemonPriceTracker carries PSA 8/9/10; there's no CGC or TAG
  data at any tier. TAG in particular has too little volume for a meaningful
  gap. Adding CGC means a second provider (PriceCharting, paid).
- **The seed list is a hypothesis.** It encodes the prior that PSA 9
  profitability lives almost entirely in condition-sensitive vintage — WOTC
  centring, Neo print lines, e-Series Crystal foil. It decides only what gets
  scanned first; data replaces the guesswork as coverage grows.

## Licensing note

PokemonPriceTracker's free and $9.99 tiers are personal/development use;
commercial use is the $99/mo Business plan. Publishing their prices on a public
site is a redistribution question — that's why `data/` is git-ignored and the
dashboard is served locally. Check with them before making it public.
