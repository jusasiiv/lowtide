# LowTide — technical plan

Electrum 4.8.2 Qt plugin. *Send, consolidate and rescue at low tide.*
This plan covers how the product in `LOWTIDE_PLAN.md` gets built. Verified against the 4.8.2 source and live mempool.space on Oct 1, 2026 (17:30 Berlin). Deviations from the product plan are listed in §9.

## 1. Architecture

```
 mempool.space (or own instance)      Electrum server (fee histogram, relay fee)
          │ HTTP via Network.send_http_on_proxy (Tor/proxy aware)        │ events
          ▼                                                             ▼
 ┌──────────────── LowTideService (one daemon thread, non-Qt) ────────────────┐
 │ fetch → parse → snapshot recorder → forecast/ETA/pile (pure python core)   │
 │ cache on disk (<datadir>/lowtide/) → publish ForecastState to listeners    │
 └─────────────────────────────┬──────────────────────────────────────────────┘
                               │ callback → pyqtSignal (Qt bridge)
 ┌─────────────────────────────▼──────────────────────────────────────────────┐
 │ qt.py Plugin: status bar button · Tools ▸ LowTide menu · forecast panel     │
 │ stress test / consolidation · rescue (tx dialog button) · send-later queue  │
 │ All tx building through wallet.make_unsigned_transaction / bump_fee / cpfp │
 │ All signing through window.show_transaction (Electrum's own preview)       │
 └────────────────────────────────────────────────────────────────────────────┘
```

Rules: the core (`core/`) imports nothing from Qt and nothing from Electrum except pure helpers, so it runs under `unittest` on recorded fixtures. The Qt layer never does I/O on the GUI thread. Hooks never return values (`run_hook` asserts at most one result).

## 2. Module layout (plugin package `lowtide/`)

| File | Purpose |
|---|---|
| `manifest.json`, `__init__.py`, `lowtide.png` | manifest (`name: lowtide`, `available_for: ["qt"]`), ConfigVars (`plugins.lowtide.*`), icon via `read_file` |
| `lowtide.py` | `LowTidePlugin(BasePlugin)`: owns the service, settings, per-wallet storage helpers, network selection (mainnet/testnet4/signet URLs, onion when `network.is_proxy_tor`) |
| `core/mempool_api.py` | URL building + parsing for `/api/mempool`, `fees/mempool-blocks`, `fees/recommended`, `v1/blocks`, `statistics/{1w,1m,3m}`, `mining/blocks/fee-rates/{1m,3m,3y}`; transport injected (callable) |
| `core/histogram.py` | fine histogram math: depth above rate, 0.1-step bins, floor pile, pile-jump rate |
| `core/history.py` | merge statistics series into one 30-min series; derive `next_block_rate(t)` from the 39 bands |
| `core/forecast.py` | hour-of-week seasonal model, level adjustment, uncertainty band, low-tide windows, "is now good?" |
| `core/eta.py` | rate → ETA (median/90%) using queue + arrivals + Poisson blocks; deadline → cheapest rate and best send time |
| `core/stress.py` | input/output vsizes per script type, cost tables, uneconomical coins, spike levels |
| `core/consolidate.py` | privacy grouping (labels/sources), linking warnings, size/cost contrast |
| `core/rescue.py` | option comparison (wait / small RBF / RBF / CPFP / accelerate) and recommendation |
| `core/accelerator.py` | mempool accelerator client: estimate, invoice, invoice lookup, payment check, status |
| `core/planner.py` | send-later planning: item → window, re-plan, due/overdue, deadline guard |
| `core/store.py` | cache.json, snapshots.jsonl (10-min histogram+blocks), fixtures export |
| `core/backtest.py` | rolling-origin backtest; CLI prints the headline number |
| `service.py` | the daemon thread: schedule, fetch, record, compute, publish, offline fallback |
| `qt.py` | `Plugin(LowTidePlugin)` with hooks: `create_status_bar`, `init_menubar`, `load_wallet`, `close_wallet`, `transaction_dialog`, `transaction_dialog_update`, `qt_utxo_menu`, `create_send_tab`, `on_close_window` |
| `qt_panel.py` | forecast panel: 7-day chart (QPainter), rate checker, deadline planner, pile view, method sentence |
| `qt_stress.py`, `qt_consolidate.py`, `qt_rescue.py`, `qt_queue.py`, `qt_settings.py`, `qt_notify.py` | dialogs |
| `tests/` + `tests/fixtures/*.json` | unittest suite on recorded data |

Repo `~/lowtide-hackathon/lowtide/` (MIT): `lowtide/` (the package), `tests/`, `scripts/` (fetch fixtures, run backtest, build zip), `README.md`, `TEST_CHECKLIST.md`, `TECH_PLAN.md`. Dev loading: symlink `electrum/electrum/plugins/lowtide → repo/lowtide`. Zip: `contrib/make_plugin lowtide` (subpackage `core/` import under zipimport is verified at M0; fallback is a flat layout).

## 3. Data sources, resolution and caching

Verified today:

| Source | Resolution | Used for |
|---|---|---|
| `/api/mempool` `fee_histogram` (277 entries, fine below 1 sat/vB) | live | pile view, pile-jump, sub-1 ETA, "now" rate |
| `/api/v1/fees/mempool-blocks` (8 projected blocks, feeRange) | live | next-block rate, queue by rate |
| `/api/v1/blocks` (15 newest, decimal feeRange, pool) | live | arrival/mined-rate estimate, sub-1 mining evidence |
| `/api/v1/statistics/1w` 5 min · `1m` 30 min · `3m` 2 h | history | seasonal model + backtest (band 0 = all sub-1) |
| `/api/v1/mining/blocks/fee-rates/1m` (≈3 blocks/pt) · `3y`/`all` | history | cross-check, spike levels for the stress test |
| Electrum server `mempool.get_fee_histogram` (event `fee_histogram`) + `network.relay_fee` | live | privacy mode fallback, relay-fee warning |
| Own snapshots every 10 min: histogram compacted to 0.1-steps below 2 sat/vB + coarse above, newest block heights/feeRange | grows from now | sub-1 history, arrival rates |

Polling: live set every 10 min (configurable, min 5), `statistics/1w` hourly, `1m`/`3m` daily, spikes weekly. Exponential backoff on errors, never more than one in-flight fetch. Everything is written to `<electrum datadir>/lowtide/cache.json`, so the panel works offline from cache and always shows "data as of HH:MM". The data dir is per network, so testnet4/signet caches never mix with mainnet. Settings (`plugins.lowtide.*`): `mempool_url` (default `https://mempool.space`), `notifications` (on), `privacy_mode` (live data from the Electrum server only; forecast history still needs a mempool instance), `demo_mode`.

## 4. Forecasting method and backtest

**Target variable.** `r(t)` = fee rate needed to be in the next block. Live: from the fine histogram, the rate where cumulative vsize from the top reaches 1 vMB (0.1 sat/vB precision). History: same cut-off from the 39-band statistics series, interpolated inside the integer band; values inside band 0 are "sub-1" and carry no finer resolution until our own snapshots accumulate.

**Model (one sentence for the stage):** "Fees follow the week: we take the last 12 weeks, learn what each hour of the week usually costs, scale it by how today is running, and call a stretch 'low tide' when it's clearly cheaper than the rest of the coming week."

1. Hour-of-week seasonal profile: 168 slots, each holding the training samples of `log r`. Prediction = slot median; band = slot 20th–80th percentile.
2. Level adjustment: ratio of the last 24 h observed vs the profile for those hours, applied with decay `exp(-Δt/48h)` over the horizon.
3. Short horizon (≤ 6 h): geometric blend from the live rate to the seasonal value, so the chart starts where the mempool is now.
4. Low-tide windows: hours whose forecast median ≤ max(1.5 × weekly minimum, weekly 25th percentile); runs ≥ 2 h, gaps ≤ 1 h merged. Next window and its expected rate feed the status bar.
5. "Is now a good time?": percentile rank of the live rate in the past 7 days of the series.

**ETA model** (`eta.py`): for rate `x`, queue ahead `Q(x)` from the live histogram; arrivals above `x`, `λ(x)` vB/s, from mined vsize above `x` over the last 15 blocks plus the queue change between our snapshots when available; blocks are Poisson at 600 s clearing 1 vMB each. Blocks needed `k = ceil(Q / (1 vMB − 600·λ))`; ETA median/90% from the Erlang(k) quantiles (Poisson sum inversion, pure Python). If `600·λ ≥ 1 vMB` the rate never drains; ETA becomes "when the tide turns" = first forecast hour below `x`. Inverse: smallest `x` in 0.1 steps with ETA90 ≤ deadline. Best time to send: cheapest forecast hour before `deadline − ETA margin`.

**Pile-jump rate** (`histogram.py`): smallest 0.1-step rate `x` above the mempool minimum such that vsize in `[x, 1.0)` ≤ 0.5 vMB, i.e. within half a block of paying 1 sat/vB. Output: "0.4 sat/vB: ahead of 41.8 vMB, behind 5.9 vMB, likely by HH:MM". Today's live numbers: 41.3 vMB below 0.3, 0.5 vMB in 0.3–0.4, nothing in 0.4–0.5.

**Backtest** (`backtest.py`, also the fixture for the unit tests): rolling origin over the merged 3-month series at hourly resolution. For each of the last 4 weeks: train on the preceding 8, forecast the week. Metrics: (a) share of predicted low-tide hours whose actual `r(t)` was ≤ that week's actual median (headline), (b) same vs the 25th percentile, (c) 20–80 band coverage, (d) MAE of log₂ rate. Baseline for honesty: "always cheap" scores 50% on (a). The sub-1 limitation is stated next to the number.

## 5. Threading and async

- GUI thread: all widgets, all wallet calls that touch the UI, all `show_transaction`.
- `LowTideService` thread (`threading.Thread`, daemon): sleeps on an `Event`, fetches with `Network.send_http_on_proxy` (blocking wrapper that runs on Electrum's asyncio loop and honours proxy/Tor), computes, saves, calls listeners. Listeners are Qt bridge objects that only `emit` a signal; slots run on the GUI thread.
- Electrum events (`fee_histogram`, `network_updated`, `new_transaction`, `verified`) via `QtEventListener` on the Qt plugin.
- Lightning payment: `window.run_coroutine_from_thread(lnworker.pay_invoice(...), 'LowTide accelerate', on_result=...)`.
- Status polling for accelerations and the send-later scheduler piggyback on the service tick (every minute for timers, 10 min for data).
- Shutdown: `on_close` stops the thread; `close_wallet` drops per-wallet state.

## 6. Persistence

| What | Where |
|---|---|
| cache, snapshots, history, backtest result | `<datadir>/lowtide/*.json(l)` (per network) |
| send-later queue, accelerations, "notified windows", stranded flags | `self.get_storage(wallet)` → saved with `wallet.save_db()` |
| settings | `config` keys `plugins.lowtide.*` |

Queue item: `id, address, amount_sat|'!', label, deadline, max_feerate, created, status (queued|planned|sent|confirmed|missed), planned_start, txid`. Acceleration record: `txid, estimate, chosen_fee, total, invoice_id, bolt11, paid, status, block_height, updated`.

## 7. UI

- **Status bar** (`create_status_bar`): flat `QToolButton` left of the network icons: `⛵ 4.0 sat/vB · low tide Sun 03–07 ≈1`. Green during low tide, amber with "server blocks sub-1" when `network.relay_fee > 100`. Click → panel. Tooltip explains.
- **Tools ▸ LowTide** menu: Forecast, Wallet stress test, Consolidate…, Send later queue, Settings.
- **Forecast panel** (non-modal window per wallet window): header (now rate, data as of, source, privacy badge); 7-day hourly chart with band, low-tide shading, now marker (QPainter widget, no matplotlib); tabs *Checker* (rate → ETA; deadline → rate & best time), *Pile* (bars by 0.1 sat/vB with floor pile, pile-jump marker and `Use 0.4 sat/vB for next send`), *Method* (sentence + backtest number + "when to accelerate" cases).
- **Apply rate**: `Use … for next send` sets `config.FEE_POLICY = 'feerate:<sat/kvB>'`, so Electrum's confirm dialog opens pre-filled; a LowTide row in the Send tab (`create_send_tab` grid) shows the live suggestion with the same button. If the fee slider snaps the pre-fill to 1 sat/vB (to verify at M1), the fallback is LowTide building the tx from the send tab's outputs and opening `show_transaction`. Plugin-built transactions (consolidation, migration, batches) always use `FeePolicy('feerate:…')` and `show_transaction`.
- **Relay-fee warning**: status bar state + one-time popup with "Tools ▸ Network ▸ pick a server with 0.1 sat/vB relay fee".
- **Notifications**: `window.notify` (tray banner) + status bar highlight + non-modal popup. macOS banner test at M0 (source and app bundle); fallback `osascript -e 'display notification'` (no dependency) if Qt banners don't show.
- **Stress test dialog**: scenarios × (rate, sats, fiat, % of balance, uneconomical coins); tech check box; buttons Consolidate… / Migrate…. Real spike levels from `fee-rates/3y`+`all` (max block-group median): 2017 ≈ 936, 2021 ≈ 248, 2023 ≈ 467, 2024 ≈ 1190 sat/vB; fallback constants if offline.
- **Consolidation dialog**: groups by label (tx label, else address label, else "unlabeled"), per-group checkboxes, explicit "these groups will become linked" warning on override, rate choice (floor 0.1–0.2 / pile-jump / low-tide / custom), contrast line, `Prepare now` → `show_transaction`, `Queue for low tide` → send-later item. Also from the Coins tab menu (`qt_utxo_menu`). Skips frozen and unconfirmed coins.
- **Rescue dialog** (button in `TxDialog.buttons`, shown when unconfirmed; also from the panel's stranded list): option table; greyed options carry a reason; recommendation line; accelerator flow: estimate → bid → confirm (bid + base fee + vsize fee, sats and fiat) → invoice → pay via Lightning (confirm again) or BOLT11 + QR → status polling → notify. Hidden off mainnet.
- **Queue dialog**: table, add/edit/remove, plan column, `Send due now`, demo switch (`low tide now`, `deadline approaching`) when `demo_mode` is on.

## 8. Tests

- `python -m unittest discover tests` using the venv (pytest is not installed; stdlib only).
- Fixtures recorded by `scripts/fetch_fixtures.py` (live histogram, mempool-blocks, blocks, statistics 1w/1m/3m, fee-rates, accelerator estimate + history responses).
- Unit tests: histogram depth/pile-jump; history band → rate; seasonal model on a synthetic weekly signal (recovers the pattern) and on fixtures; ETA monotonicity and the Erlang quantiles; deadline inverse; low-tide windows; planner (window choice, overdue, re-plan); rescue comparison (availability rules, cheapest-first); accelerator parsing (estimate, history statuses incl. `completed_provisional`); consolidation grouping; stress sizes per script type.
- Backtest runs as a script and as a test that asserts the headline beats the 50% baseline.
- Manual checklist (`TEST_CHECKLIST.md`): load from source and zip, status bar, panel offline, sub-1 preview on a 0.1-relay server, relay warning on a 1 sat/vB server, stress/tech check on legacy wallet, consolidation preview, rescue on a stuck tx, accelerator up to the pay dialog, queue + demo switch, notifications.

## 9. Findings that change the plan

- Fees right now are not ~1 sat/vB: next block ≈ 5–6, economy 2, with ~41.8 vMB sub-1. Past week block medians: 0 (35 %), 1 (29 %), 2 (18 %), ≥3 (18 %). Demo numbers must be computed live, not hard-coded.
- History resolution: `statistics` 1w = 5 min, 1m = 30 min, 3m = 2 h, 6m = 3 h; `fee-rates` 1w ≈ 1.3 blocks/pt, 1m ≈ 3.2, 3m ≈ 12. The model trains on the 30-min merge of 1w+1m+3m.
- Accelerator estimate today: `cost 1000`, `options 1000/2000/10000`, `mempoolBaseFee 75000`, `vsizeFee 0`, bitcoin min 1000 / max 10,000,000; history statuses include `completed_provisional`. The invoice endpoint answers "missing parameters" on an empty body, so it exists; its response shape is confirmed at M3 by creating a real invoice.
- Test networks return "Cannot POST" for the accelerator → hidden there, as planned.
- `TxDialog` builds `self.buttons` before `run_hook('transaction_dialog')`, so a Rescue button can be inserted cleanly.
- `run_hook` asserts at most one non-false result; LowTide hooks return `None`.
- Electrum.app 4.8.2 is x86_64 and bundles Python 3.12 (same as the venv). `/etc/electrum/plugins_key` does not exist yet → the admin prompt happens at M0.
- No History-tab hook exists, so "stranded" shows in the status bar, the panel and the tx dialog, not in the history list.

## 10. Risks and cut lines

| Risk | Mitigation / cut |
|---|---|
| Qt tray banners invisible on macOS | osascript fallback; in-app cue always |
| Subpackage import inside zip | tested at M0; flatten if needed |
| Fee slider overrides the sub-1 pre-fill | LowTide-built preview fallback |
| mempool rate limits / outage | 10-min cadence, backoff, cache, server-histogram mode |
| Accelerator invoice shape unknown | parsed defensively; QR path always works |
| Time | F4 cut to "queue + batch now + demo switch" if M4 slips; F5 only if everything else is solid |

## 11. Demo and submission constraints (3-minute live demo, repeated per judge group)

Submission before Fri 15:30 Berlin; the Freeze at 13:00 already leaves 2.5 h for zip, README, screenshots and rehearsal. The repeated 3-minute format adds these requirements:

- **Repeatable in seconds.** A `Demo reset` action (dev settings) clears the queue, notified windows, stranded flags and demo switches, so every judge group sees the same start state. Nothing in the live path broadcasts: all flows end in Electrum's preview, which is closed unsigned, so the staged wallet (stuck 0.2 sat/vB tx, 20 small coins, legacy wallet) survives every round.
- **Instant demo switches.** `low tide now`, `deadline approaching` and `stranded` fire immediately (status bar highlight, popup, tray banner) without waiting for the 10-minute poll.
- **No fetch on the live path.** Panel, pile view, stress test, consolidation and rescue render from cache in under a second; stress and consolidation results are cached per wallet. The accelerator estimate is the only live call; if venue Wi-Fi fails it shows the last cached estimate for that txid and the screenshot covers it.
- **Run from source on stage** (venv, already warm, demo wallet and the legacy wallet pre-opened in two windows). The zip install in Electrum.app is shown as a screenshot and mentioned for the Electrum judges.
- **Deliverables add** `DEMO_SCRIPT.md` (the 3-minute script with timings and the screenshot cues) and `demo/screenshots/` (numbered PNGs for the parts not shown live: queue + demo switch + batched preview, zip install, notifications, Nostr if built). Rehearsal in the Freeze block: two full dry runs, one with Wi-Fi off.

Proposed live script (what to build for):

| Time | Live on screen | Spoken |
|---|---|---|
| 0:00–0:25 | status bar `⛵ 4 sat/vB · low tide Sun 03–07 ≈1` | hook: almost free or absurd; 40 MB pile; next spike |
| 0:25–1:00 | forecast panel: chart, next low tide, deadline planner typed live, backtest number | method in one sentence |
| 1:00–1:30 | pile view → `Use 0.4 sat/vB` → Send → preview shows 0.4 → close | Electrum stops at 1; LowTide gets the same spot for 60 % less |
| 1:30–2:00 | stress test on the demo wallet (now / low tide / 2024 spike); legacy wallet window: tech check | the next spike will cripple these wallets |
| 2:00–2:30 | consolidate at the floor: groups, linking warning, contrast line, Prepare → preview → close | 280 sats now vs 140,000 at the spike |
| 2:30–3:00 | rescue on the stuck tx: Wait / small RBF / RBF / CPFP / Accelerate side by side, four "when to accelerate" cases | fully implemented, pays from Lightning, not spending €57 on stage |

Send-later (queue two, demo switch, one batched preview) is the first thing to add live if a group runs ahead of time; otherwise it is the screenshot sequence. Submitting (making the repo public, uploading) is the user's step before 15:30; the zip, README and screenshots are ready at 13:00.
