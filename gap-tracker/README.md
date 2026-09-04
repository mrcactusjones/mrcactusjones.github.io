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

## Price history (paid tier)

The $9.99 API tier returns up to a year of daily price history per card, and
20,000 credits a day. That changes the shape of the project: history arrives in
one pass instead of accruing a day at a time, so it goes into SQLite rather
than daily JSON snapshots.

```bash
python3 run.py backfill --provider ppt --days 180   # sweep sets, store history
python3 run.py rank                                 # trends attach automatically
python3 run.py trends --top 20                      # rank by how long a gap held
```

Costs, at 3 credits a card (base + graded + history):

| | credits | notes |
|---|---|---|
| one card | 3 | |
| a 100-card page | 300 | one request against the 60/min ceiling, not 100 |
| a 443-card universe | ~1,329 | a single run, inside one day's budget |
| daily refresh of 6,000 cards | 18,000 | still fits |

Sweeping by set returns cards the seed list never chose. They are paid for
either way, so they are folded into the universe -- the set list, not the
rarity filter, becomes what bounds the project.

## Conviction score

One 0-100 number for "how much do I believe this trade", so the top of the
table can be read without weighing six columns by eye. It is the default sort.

| component | weight | scale |
|---|---|---|
| size | 20% | floor ROI, full marks at 50% |
| durability | 25% | worst 90-day floor ÷ current floor |
| depth | 15% | PSA 9 comps, log-scaled, full at 20 |
| freshness | 15% | days since the last PSA 9 sale, zero at 90 |
| liquidity | 15% | PSA 9 sales/month, full at 4 |
| direction | 10% | divergence, centred on no movement |

Three rules keep it from becoming a black box:

- **The floor is a gate, not an input.** A card whose PSA 9 doesn't clear costs
  scores zero no matter how good everything else looks. The score reorders
  within your framing; it can never overrule it.
- **Missing components are dropped, not zeroed**, and the remaining weights are
  renormalised. Otherwise the score would rank cards by how much history we
  happen to hold. `conviction_coverage` reports how much of the weight was
  actually measurable; the tooltip shows every component.
- **Thin comps multiply, they don't veto.** An unconfident card keeps 60% of
  its score, so something exceptional can still surface — it just has to clear
  a higher bar.

Displayed rounded to the nearest 5, because 85 against 81 is noise and a
precise-looking number invites reading a difference that isn't there.

Weights and scales live under `scoring` in `config.json`. Note that `size`
saturates for most cards that pass the floor test — that is deliberate. Past
about a 50% return, a bigger gap doesn't make a card more of a *no-brainer*,
and letting size run would just rediscover "sort by biggest gap", which is the
ranking this tool exists to improve on. The discrimination comes from the
reliability components.

### Worst case and liquidity

Two columns matter more than the headline floor:

- **Worst 90d** — the lowest the floor went in the last 90 days, costed under
  your current settings. A card showing a $175 floor today that touched −$97
  in May is not a floor, it is a snapshot of a good day. `floor_durability`
  expresses the same thing as a ratio (worst ÷ current), and `floor_p10_90d`
  gives the 10th percentile for cards where the outright minimum is one bad
  print.
- **$/month** — the floor divided by months of capital: PSA's turnaround plus
  the expected wait to sell, derived from PSA 9 sales per month. A $400 floor
  you collect after eight months is worse than a $120 floor you collect twice
  over in the same time.

Sales rate comes from the counts and the window they cover
(`ebay.dateRangeStart/End`), because a count without a window means nothing --
12 sales over three months and 12 over three weeks are different markets. The
wait is a lower bound: it assumes yours is the next copy to sell, when you
actually queue behind other listings.

The page recomputes both from the same per-day observations the model used
(`gap_points`), so the worst case, the sparkline and the table can never
disagree, and all three follow the cost sliders.

### What the history buys

`gapscan/trends.py` computes, per card:

- **held / observations** — how often the floor cleared the threshold in the
  last 90 days, with the denominator, so sparse data can't masquerade as
  persistence.
- **streak** — calendar days the gap has held unbroken. Days, not
  observations: an irregularly sampled series must not claim more.
- **PSA 9 30d** — momentum of the graded price.
- **divergence** — PSA 9 momentum minus raw momentum. Positive means the
  graded price is pulling away on its own; negative means raw is catching up
  and the trade is closing.
- **volatility** — standard deviation of daily moves, to separate a real gap
  from a noisy one.

Trend ends are compared by median of the first and last few points, so a single
odd sale cannot create a trend.

### Schema

`data/gaps.sqlite3`: `cards`, `price_points` (card, date, grade -- so
re-ingesting overlapping windows is idempotent), `daily_metrics`, `runs`.

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

### When a card is not judged at all

Three checks sit ahead of the verdict, because the failure they catch looks
like an opportunity rather than an error.

**No graded market.** A card with fewer than `min_graded_sales` sales across
every PSA grade returns no verdict at all, the same as one with a missing
price. `Mega Dragonite ex` sat at −$94 with a PSA 10 at $0.60; that is not a
bad trade, it is the absence of a market, and ranking it as a terrible trade
implies a precision that isn't there.

**A 9 priced above a 10.** Impossible in a market that grades honestly, so it
means sales from different cards landed in one bucket. Costs the card its
confident status.

**A graded/raw multiple far out of step with its own set.** Computed per set
from the cards already priced (median, needing `min_set_sample` cards), because
sets differ enormously — 3× is normal in one and 20× in another. Aquapolis
holds several cards of the same name, and grades are parsed from eBay listing
titles, so this is where pooled comps surface.

**Printings worth very different amounts.** A card's `variants` can differ
several-fold — your Psyduck is $202 as a Normal and $700 as a Reverse Holofoil
— while its graded sales come back as a single pool. There is one `ebay` block
per card and no printing dimension anywhere inside it, so a graded price paired
with any one printing's raw price is comparing two different goods.

This one cannot be fixed at the source. The API's `printing` parameter changes
which *raw* price is returned; it has no effect on the graded sales, because
those carry no printing to filter on. So the spread is measured and flagged,
and the floor is left alone: we genuinely do not know which printing those
sales were, and adjusting the number would be inventing an answer rather than
reporting the uncertainty.

All four come from the same root cause: the provider matches graded sales by
reading listing titles. The checks don't fix that; they mark where it shows.

### Grading fees

PSA paused all four Value tiers on 2026-06-02 under a 14-million-card backlog,
so the cheapest service is Regular at $79.99 rather than the ~$20 bulk rate
most ROI calculators still assume. `Economics.fee_tiers` prices a submission
off the card's **slabbed** value (its PSA 9 price where known), because that is
what PSA charges the tier and the 2% insurance surcharge on. Set
`use_fee_tiers: false` to go back to a flat `grading_fee` if the Value tiers
reopen.

The difference is not cosmetic: a card with a $35 raw and a $149 PSA 9 cleared
$61 at the old bulk rate and clears $0.02 at the real one.

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

## Tests

```bash
python3 -m unittest discover -s tests -t .        # 131 tests, no dependencies
node tests/js/check_page_matches_model.js         # page vs model agreement
```

The second one matters more than its size suggests. The dashboard reimplements
the entire cost model in JavaScript so the sliders respond instantly, and that
duplication is the project's biggest correctness risk: the two can drift apart
and nothing would say so. The check runs the page's own script against the
current rankings and asserts they agree on all-in, floor, upside, break-even,
EV, worst case and conviction, row by row.

It has caught five real bugs so far: probabilities rounded in the payload but
not in the maths; the historical floor priced at a cheaper PSA tier than the
present (Python disagreeing with itself); a downsampled series that hid the dip
the model saw; capital months stored rounded but divided at full precision; and
a fee slider that silently did nothing. Run it after any change to `econ.py`,
`scoring.py` or `index.html`.

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
