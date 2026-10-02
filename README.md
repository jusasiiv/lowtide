# LowTide — send, consolidate and rescue at low tide

An [Electrum](https://electrum.org) 4.8 desktop plugin that turns the fee tide into a plan: it forecasts when confirming is cheap, applies fee rates Electrum itself cannot reach (below 1 sat/vB), stress-tests your wallet against the next fee spike, consolidates coins without linking what you kept separate, and rescues stuck transactions with an honest side-by-side comparison. Aggregate network data only, Tor-friendly, works with your own mempool instance, no new dependencies, and **you always sign**.

Built at bitcoin++ Berlin 2026 (payments edition) for the Electrum plugin challenge and the mempool.space accelerator challenge.

![Forecast panel](demo/screenshots/01-forecast-panel.png)

## Why now

Data checked on mempool.space, Oct 1, 2026:

- **Fees are the lowest in years.** The block median has sat around 1 sat/vB for a year.
- **Fees follow a predictable tide.** Over the last 3 months, blocks mined around 04:00 UTC averaged 0.5 sat/vB against 2.3 around 19:00 UTC; Sundays averaged 0.8, Fridays 1.7.
- **Cheap transactions really do get stuck.** Nodes and Electrum now accept 0.1 sat/vB, so a permanent pile of roughly 40 vMB below 1 sat/vB sits in the mempool.
- **Sub-1 sat/vB is the biggest everyday opportunity.** The pile is lopsided: ~32 vMB at 0.1–0.2 sat/vB, ~10 vMB at 0.2–0.3 and almost nothing between 0.3 and 1. A transaction at ~0.4 sat/vB jumps the whole pile and lands nearly where 1 sat/vB does, for about 60% less.
- **Electrum hides this.** Its estimates never go below 1 sat/vB and its slider starts at 1. Sub-1 rates are reachable only by typing them, and only on a server that relays 0.1 sat/vB (24 of 33 default servers do).
- **The next spike will hurt** wallets full of small coins or on legacy address types. 2017, 2021, 2023 and 2024 all had one.

Fees are bimodal: almost free, or absurd. Housekeeping belongs in the free regime. Non-urgent sends should wait out the absurd one.

## What it does

### Tide forecast (the hub)

- **Status bar indicator**, always visible: `⛵ 1.1 sat/vB · low tide Sat 03–16`. Green during low tide. Amber with "server blocks sub-1" when the connected Electrum server refuses fees below 1 sat/vB, with instructions to switch.
- **Forecast panel** (click the indicator, or Tools ▸ LowTide): the next 7 days hour by hour with an uncertainty band, low-tide windows highlighted, a "now" marker and the past week for context.
- **Rate checker**: enter a rate, get "likely by …" with a 90% bound. **Deadline planner**: enter "confirmed by", get the cheapest safe rate now and the better time to wait for.
- **Pile-jump suggestion**: the cheapest 0.1-step rate that lands within half a block of paying 1 sat/vB, with its ETA. One click applies it to your next send: Electrum's own confirm dialog opens prefilled, sub-1 included.
- **Send tab row**: the live suggestion and a "Use" button.
- **Notifications** when low tide starts (desktop banner plus an in-app cue), at most once per window. Optionally also as an encrypted Nostr DM to your phone.

**Method, in one sentence:** fees follow the week, so LowTide learns what each hour of the week has cost over the last 12 weeks, scales that by how today is running, and calls a stretch "low tide" when it is clearly cheaper than the rest of the coming week.

**Accuracy:** in a rolling backtest over the last 4 weeks (train on 8, predict 1), predicted low-tide hours were cheaper than the week's median **70%** of the time, against a 51% baseline for "every hour is cheap".

**Resolution below 1 sat/vB:** mempool.space history cannot resolve below 1 sat/vB, so sub-1 advice comes from the live fee histogram (fine-grained) and LowTide records its own 10-minute snapshots to build sub-1 history over time. It works offline from cached data and shows "data as of …".

![Rate and deadline](demo/screenshots/02-rate-deadline.png)

### Wallet stress test and tech check

What it costs to spend every coin now, at the next low tide, at the real spike levels of 2017, 2021, 2023 and 2024 (highest block-median rates, from mempool.space), and at a custom rate; the share of your balance lost to fees and which coins become uneconomical. Fiat through Electrum's exchange-rate settings. The tech check names your script type and what each input costs compared with native segwit (legacy p2pkh inputs are ~54% more expensive than p2wpkh) and offers a guided migration. Taproot is not recommended because Electrum cannot create Taproot wallets.

![Stress test](demo/screenshots/03-stress-test.png)

### Privacy-aware consolidation

Coins with different labels are never merged by default; the dialog shows exactly what would become linked if you override. Consolidation is never urgent, so it defaults to the floor of the mempool (0.1–0.2 sat/vB). The contrast is shown plainly: 20 native segwit inputs cost ~280 sats at 0.2 sat/vB and ~140,000 sats at a 100 sat/vB spike. "Prepare now" opens Electrum's transaction preview. Also in the Coins tab context menu. Frozen and unconfirmed coins are skipped; hardware and multisig wallets sign through Electrum's normal flow.

![Consolidation](demo/screenshots/04-consolidation.png)

### Stuck transaction rescue

A `⛵ Rescue…` button in Electrum's transaction dialog for any unconfirmed transaction, incoming or outgoing, and a "stranded" indicator when a transaction has waited longer than the forecast expected. Options side by side, each with cost (sats and fiat), expected confirmation and requirements; unavailable ones are greyed out with the reason:

| Option | Notes |
|---|---|
| Wait | free; ETA from the forecast |
| Small RBF step | to the pile-jump rate; for a transaction stuck in the floor pile this costs tens of sats |
| RBF bump | to the rate your deadline needs, through Electrum's own bump-fee machinery |
| CPFP | when the wallet controls an output |
| mempool Accelerator | cost from the live estimate; paid over Lightning from this wallet or from any wallet via QR |

LowTide recommends the cheapest option that meets your deadline and says why. A sub-1 safety net warns when the mempool minimum rises above your rate (nodes start evicting) or a transaction approaches the 2-week expiry.

The accelerator flow is complete: estimate → pick a bid → explicit confirmation of the total (bid + base fee + size fee) → invoice → pay from this wallet's Lightning (second confirmation) or show the BOLT11 and QR → track status and notify. Records are kept per transaction. Verified against the live API: a 2,000 sat bid with the 75,000 sat base fee produced an invoice that decodes to exactly 77,000 sats. **Nothing is ever paid without your explicit confirmation**, and the txid is sent to mempool.space only when you press "Get estimate".

![Rescue](demo/screenshots/05-rescue.png)

## When is acceleration the right call?

RBF and CPFP are usually cheaper. Pay for acceleration when:

1. **You can't sign**: a watch-only merchant wallet, a multisig cosigner who is away, a hardware wallet that isn't at hand, or pre-signed transactions such as Timelock Recovery plans or Lightning force-closes.
2. **The txid must not change**: invoices, exchange deposits and payment proofs track the txid, and RBF changes it.
3. **It's an incoming payment** someone else underpaid.
4. **Your on-chain coins are tied up, but you have Lightning balance.**

## Privacy

- The forecast uses only aggregate network data (mempool fee histogram, projected blocks, recent blocks, fee statistics). Nothing about your wallet leaves the machine.
- A txid goes to mempool.space only when you press "Get estimate" in the rescue dialog.
- All HTTP goes through Electrum's proxy-aware helper, so Tor and proxy settings apply; with Tor on, mempool.space's onion endpoint is used.
- The mempool instance is configurable (for example a self-hosted mempool on RaspiBlitz or Umbrel). The accelerator itself only exists on mempool.space mainnet.
- Privacy mode: live mempool data from your Electrum server only (the forecast history still needs a mempool instance).

## Install

Requires Electrum 4.8.2 desktop (Qt).

**From the zip** (release binaries): Tools ▸ Plugins ▸ Add, select `lowtide-0.1.0.zip` (built with `scripts/build_zip.sh`, or from the releases page), enable it and set a plugin password when asked. Electrum stores a key for authorizing third-party plugins once; on macOS that is an admin prompt.

**From source** (running Electrum from a git checkout):

```bash
ln -s /path/to/lowtide/lowtide /path/to/electrum/electrum/plugins/lowtide
```

Then enable LowTide under Tools ▸ Plugins. Never use your everyday data folder for testing; pass `-D` to Electrum.

Settings (Tools ▸ LowTide ▸ Settings): mempool instance URL, refresh interval, notifications, Nostr DM alerts (recipient npub, relays), privacy mode, demo mode ("low tide now" and "stranded" switches for rehearsals).

## Development

```bash
python -m unittest discover -s tests -t .      # unit tests on recorded fixtures, stdlib only
python scripts/fetch_fixtures.py               # re-record live mempool.space responses
python scripts/dev_fake_wallets.py <electrum> <datadir>   # wallets with fake coins and stuck txs, for --offline GUI testing
scripts/build_zip.sh                           # dist/lowtide-0.1.0.zip via Electrum's contrib/make_plugin
```

`lowtide/core_*.py` are pure Python (histogram math, history merge, hour-of-week forecast, ETA model, backtest, stress test, consolidation grouping, rescue comparison, accelerator client). `service.py` is the one background thread; the `qt_*.py` modules are the UI. See `TECH_PLAN.md` for the design and `TEST_CHECKLIST.md` for the manual checks.

## Out of scope (for now)

Android/QML, CLI commands, pre-signed fee ladders, a send-later queue (Electrum 4.8 already batches new payments into an unconfirmed transaction), Taproot, Lightning beyond paying an acceleration invoice.

## License

MIT.
