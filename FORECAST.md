# How LowTide forecasts the fee tide

This document describes what the plugin computes and how, in the order the numbers appear on screen. Everything below is implemented in pure Python without numpy, in `lowtide/core_history.py`, `core_forecast.py`, `core_eta.py`, `core_histogram.py` and `core_backtest.py`, with unit tests on recorded fixtures in `tests/`.

## 1. What is being forecast

The quantity LowTide forecasts is the **next-block rate** `r(t)`: the fee rate, in sat/vB, that a transaction needs to be inside the first 1 vMB of the mempool at time `t`, which is the part the next block takes.

- **Live:** from the mempool fee histogram (`/api/mempool`, a list of `[fee rate, vsize]` pairs, fine-grained below 1 sat/vB). Walk the histogram from the highest fee down, accumulating vsize; the fee at which the cumulative vsize reaches 1 vMB is the next-block rate, rounded *up* to Electrum's 0.1 sat/vB precision.
- **Historical:** from `/api/v1/statistics/{1w,1m,3m}`, which gives the mempool's vsize in 39 fee bands at each timestamp. The bands are `[0,1), [1,2), [2,3), … [1800,2000), [2000,∞)` sat/vB. Walk the bands from the top; when the cumulative vsize crosses 1 vMB inside a band, interpolate linearly inside it: needing only a sliver of the band means a rate near its top, needing the whole band means its bottom.

### Resolution below 1 sat/vB

Band 0 covers everything below 1 sat/vB, so history cannot say whether a quiet hour needed 0.2 or 0.9 sat/vB. Two things compensate:

1. When the historical cut-off falls into band 0, the rate is resolved with the **live sub-1 shape**: the amount of sub-1 vsize needed to fill the block is looked up in today's fine histogram, and the fee at which the live sub-1 cumulative reaches that amount is used. The justification is that the sub-1 pile is persistent (around 40 vMB for months, concentrated at 0.1–0.3 sat/vB), so its shape today is a fair prior for its shape last week.
2. The plugin records its **own snapshots** every 10 minutes (`snapshots.jsonl`: the histogram compacted to 0.1 sat/vB bins below 2 sat/vB, plus the newest blocks), building genuine sub-1 history over time.

## 2. Building the history series

The three statistics series have different resolution: 5 minutes for the last week, 30 minutes for the last month, 2 hours for the last 3 months. They are merged into one series with one point per 30-minute bucket, the finer series overriding the coarser where both exist. The series is then reduced to **one point per hour** (median rate of the points in that hour). On Oct 2, 2026 this gave 92 days and about 1,465 hourly points.

## 3. The tide model

Fees follow a weekly cycle (nights and weekends are cheap, weekday afternoons expensive), so the model is a **seasonal profile over the hour of the week**, adjusted for how the current days are running.

1. **Profile.** Every hourly point of the last 12 weeks is assigned to one of 168 slots (weekday × hour, UTC). Each slot keeps the sorted log rates of its samples. A slot with fewer than 3 samples borrows its neighbours (±1, ±2 hours).
2. **Point forecast and band.** For each of the next 168 hours, the forecast median is the slot's median, and the uncertainty band is the slot's 20th and 80th percentile, all in log space (fees are multiplicative).
3. **Level adjustment.** How does today compare with the profile? For each of the last 24 observed hours, take the ratio of the observed rate to the slot median; the level is the geometric mean of those ratios, clamped to [0.4, 2.5]. It multiplies the forecast with a decaying exponent, `level ^ exp(-h / 48)` for an hour `h` steps ahead, so today's level matters for the next day or two and the profile takes over later.
4. **Live blend.** For the first 6 hours, the forecast is interpolated in log space from the live next-block rate (weight 1 at the current hour) to the adjusted seasonal value (weight 1 at +6 h), so the chart starts where the mempool actually is.
5. All values are clamped to at least 0.1 sat/vB and quantized to 0.1.

## 4. Low-tide windows

A **threshold** is set per forecast: the larger of 1.5 × the week's minimum forecast median and the 25th percentile of the week's hourly medians. Hours at or below the threshold are low tide. Gaps of one hour are merged, and runs shorter than two hours are dropped. Each window reports the 25th percentile of its hourly medians as its expected rate. The first window ahead is what the status bar announces.

The **"now" verdict** in the panel is the percentile of the live rate among the past 7 days of hourly rates: cheaper than 75% of them is "a cheap moment", above 35% "average", otherwise "an expensive moment".

## 5. Confirmation time (ETA)

For a transaction paying rate `x`:

- **Queue ahead** `Q(x)`: the vsize in the live histogram at or above `x` (ties count against you).
- **Arrivals above `x`** `λ(x)` in vB/s: the vsize mined at or above `x` over the newest 15 blocks, divided by their time span, using each block's 7-point `feeRange` (min, p10, p25, p50, p75, p90, max) interpolated as a CDF. When two of the plugin's own snapshots exist, the observed growth of the queue above `x` between them is added. In steady state what gets mined above a rate is what arrives above it.
- **Blocks needed** `k = ceil(Q / (1 vMB − 600 s · λ))`. Blocks arrive as a Poisson process with a 10-minute mean, so the time for `k` blocks is Erlang-distributed; the median and 90% quantile are found by inverting the Poisson sum.
- **Deferring to the tide.** If the queue does not drain (`600 · λ ≥ 1 vMB`) or the median exceeds 2 hours, the steady-state model is the wrong tool: the transaction confirms when the tide drops below `x`. The ETA then becomes the first forecast hour whose median is at or below `x`, plus one block, and the 90% bound the first hour whose *upper band* is at or below `x`, plus three blocks. This is marked "when the tide turns" in the UI.

**Deadline planner (inverse):** test rates in 0.1 steps from the floor up to 2 sat/vB, then each histogram level above; the cheapest whose 90% ETA fits inside the deadline wins. The "or wait until" suggestion is the cheapest forecast hour that still leaves three hours before the deadline.

**Pile-jump rate:** the smallest 0.1-step rate above the mempool minimum at which the vsize between that rate and 1 sat/vB is at most 0.5 vMB, meaning half a block from the queue position that 1 sat/vB buys. With about 41 vMB below 0.3 sat/vB and almost nothing between 0.3 and 1, this has been 0.3–0.4 sat/vB, which is why "nearly the same place in line for 60–70% less".

## 6. Backtest

A rolling-origin backtest runs daily on the merged series: for each of the last 4 weeks, train on the 8 weeks before it, forecast the week from its first hour (using the last training observation as the live rate), and compare with what happened.

| Metric | Result (Oct 2, 2026) |
|---|---|
| Predicted low-tide hours that were cheaper than that week's actual median | **70%** (317 hours) |
| Baseline: calling every hour low tide | 51% |
| Predicted low-tide hours inside the cheapest quarter of the week | 54% (baseline 25%) |
| Hours whose actual rate fell inside the 20–80% band | 53% (nominal 60%) |
| Mean absolute error of log₂(rate) | 0.73, a factor of about 1.7 |

The headline number appears on the panel's Method tab. The band is somewhat too narrow; it is shown as "likely" rather than as a guarantee.

## 7. Limitations

- History below 1 sat/vB comes from the live sub-1 shape until the plugin's own snapshots have accumulated weeks of data.
- The 3-month series is 2-hourly, so the oldest training weeks contribute every other hour.
- A seasonal model cannot foresee demand shocks (inscription waves, ETF flows, exchange outages); the level adjustment catches up within a day.
- `feeRange` of blocks older than the newest 15 is integer-rounded, which is why arrivals are estimated from the newest blocks only.
- All times are computed in UTC and displayed in local time.

## 8. Data sources and privacy

Everything above uses aggregate network data: `/api/mempool`, `/api/v1/fees/mempool-blocks`, `/api/v1/blocks`, `/api/v1/statistics/*` and `/api/v1/mining/blocks/fee-rates/*` on the configured mempool instance (mempool.space by default, its onion address under Tor, or your own node). Nothing about the wallet is sent. Requests go through Electrum's proxy-aware HTTP helper. In privacy mode the live histogram comes from the connected Electrum server instead.
