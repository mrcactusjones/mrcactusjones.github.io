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

1. Get a free key at [pokemonpricetracker.com](https://www.pokemonpricetracker.com/pricing)
   (100 credits/day, no card required).
2. Copy `.env.example` to `.env` and paste the key in. `.env` is git-ignored.
3. Check the plumbing before spending anything:

```bash
python3 run.py reset --yes                      # clear demo data first, if you ran it
python3 run.py catalog                          # free; warns on bad or failed set ids
python3 run.py scan --provider ppt --dry-run    # shows what it would fetch, spends nothing
python3 run.py probe --card base1-4             # 1 card: confirms the field mapping
```

If `catalog` reports skipped sets (pokemontcg.io throws intermittent 500s),
retry just those — the rest of the universe is kept:

```bash
python3 run.py catalog --sets base5,gym1
```

If `probe` 404s, the endpoint has moved. Find the live one, no credits spent:

```bash
python3 run.py probe --discover
```

then put the working base in `.env` as `PPT_API_BASE`.

4. Then run it for real:

```bash
python3 run.py daily --provider ppt --log
```

## Tracking specific cards

`seeds/watchlist.json` holds hand-picked cards. They bypass the rarity and
price-band filters, sit at the top of the scan queue, and survive catalog
rebuilds.

A name and number alone can match the wrong printing, so an entry has to be
pinned to an exact provider record before it is tracked:

```bash
python3 run.py watchlist              # what's in the list, what's resolved
python3 run.py watchlist --resolve    # look each one up, 1 credit each
python3 run.py catalog                # fold the resolved ones into the universe
```

Resolution matches on card number and reports what it found. One candidate is
accepted automatically; several are listed for you to choose between; none
prints what it did see, so you can correct the number or set hint. Nothing is
guessed.

## Running it daily

Missing a day costs a day of price history, so schedule it.

**Windows** — one command, then it runs at 9am daily:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1
Start-ScheduledTask -TaskName GapTracker     # test it immediately
```

Change the time with `-At "7:00AM"`. Remove it with
`Unregister-ScheduledTask -TaskName GapTracker -Confirm:$false`. If PowerShell
policy blocks it, `scripts\run-daily.bat` does the same job and can be pointed
at by Task Scheduler's GUI.

**macOS / Linux** — `crontab -e`, then:

```
0 9 * * * /full/path/to/gap-tracker/scripts/run-daily.sh
```

Either way, output lands in `data/logs/<date>.log`. The scheduled task is set
to catch up on a missed run rather than skip it, so a sleeping laptop doesn't
put a hole in the history.

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

## Gem rates and EV

Alongside the floor there's a probability-weighted EV, when a grade mix is
known:

```
EV = P(10)×net(PSA 10) + P(9)×net(PSA 9) + P(<9)×net(downside) − all_in
```

The mix comes from one of two places, and the difference matters:

- **population** — PSA's own report via `/api/v2/population`. The real thing,
  but 2 credits/card and Business plan only. `run.py population` tests whether
  your plan can reach it, for 2 credits.
- **sales** — inferred from the counts in `ebay.salesByGrade`, which you get
  free with every scan. Shown with a `*` in the dashboard.

The sales mix is the weaker one, and biased in a specific direction: people
list their 9s and 10s and sit on their 8s, so a card with no low-grade sales
looks like it can never grade below a 9. `sales_mix_min_low` reserves a floor
of probability (15% by default) for that outcome, taken proportionally from the
grades that were observed. It is an assumption, not a measurement, which is why
a population report supersedes it.

Both sources overstate P(10) for a random raw copy, because submitters send
their best copies. Treat either as a ceiling.

**The floor-at-9 ranking never uses any of this.** Verdicts and ordering depend
only on observed prices; the EV is an extra column, so gem-rate guesswork can
never promote a card to `no_brainer`.

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
scripts/                run-daily wrappers + Windows Task Scheduler installer
seeds/community.json    curated bootstrap: which sets/cards get credits first
fixtures/catalog.json   offline stand-in for pokemontcg.io so demo needs no network
index.html              dashboard; recomputes client-side as you move the sliders
.env                    git-ignored: your API key (copy .env.example)
data/                   git-ignored: cache, rankings, daily history, logs
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
