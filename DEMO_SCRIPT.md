# LowTide — 3-minute live demo

Everything on mainnet, from source, two windows pre-opened: `lowtide_demo` (the stuck transaction) and `lowtide_fake_segwit` offline (coins and queue). Before each judge group: Tools ▸ LowTide ▸ Settings ▸ **Demo reset** (also clears the demo switches), close any open dialogs, Send tab empty.

| Time | Click | Say |
|---|---|---|
| 0:00 | Point at the status bar: `⛵ 1.1 sat/vB · low tide Sat 03–16` | "Bitcoin fees are either almost free or absurd. Right now they're almost free, and that created new problems: a permanent 40 MB pile of cheap transactions that get stuck, and wallets the next spike will cripple. LowTide is an Electrum plugin that turns the fee tide into a plan." |
| 0:25 | Click the indicator. Chart, band, green windows. Type a deadline in "Rate & deadline". Method tab: the backtest line. | "Fees follow the week. We learn what each hour of the week costs, scale it by today, and call the cheap stretches low tide. Backtested: 70% of predicted low-tide hours were cheaper than the weekly median, baseline 51%." |
| 1:00 | "Rate & deadline" tab, first line: the suggestion. Press **Use 0.4 sat/vB**, Send tab, paste address + amount, Pay… The confirm dialog opens at 0.4. Close. | "Electrum stops at 1 sat/vB. The whole pile sits below 0.3. 0.4 gets you nearly the same spot in line for 60% less, and it's one click in Electrum's own dialog." |
| 1:30 | Fake window: Tools ▸ LowTide ▸ Wallet stress test. Then File ▸ Open legacy wallet (pre-opened second window) for the amber tech check. | "Twenty small coins: 294 sats to move them now, 1.7 million at the 2024 spike. Legacy wallet: every input costs 54% more than native segwit; migrate at low tide." |
| 2:00 | Consolidate… groups by label, contrast line, **Prepare now** → Electrum preview at 0.2 sat/vB. Close. | "Privacy-aware: labels are never merged unless you say so. 158 sats now versus a hundred thousand later." |
| 2:30 | Live window: History ▸ the stuck transaction ▸ **Rescue…**. Table. **Get estimate**. | "Wait, a tens-of-sats RBF step, RBF, CPFP and the mempool accelerator side by side, with the cheapest that meets your deadline recommended. The accelerator is fully implemented and pays from this wallet's Lightning; we're not spending €57 on stage. It's the right call when you can't sign, when the txid must not change, for incoming payments, or when your coins are tied up but you have Lightning." |
| 2:55 | Close. | "Cheap by default, on time when it matters. Aggregate-only data, Tor-friendly, works with your own mempool, no new dependencies, and you always sign." |

If a group is ahead of time: Settings ▸ demo switch **Low tide now** → desktop banner, in-app alert and (if set up) the Nostr DM on the phone; then **Stranded transaction** → status bar ⚠ and the rescue prompt.

Screenshots in `demo/screenshots/` cover what is not clicked: zip install in Electrum.app, notifications and the Nostr DM, migration dialog.

## Fallbacks

- Wi-Fi down: the panel, pile view, stress test and consolidation all run from cache ("data as of"). Rescue works except the live estimate; the screenshot shows the quote.
- Rescue dialog on a transaction that confirmed in the meantime: use the fake window's "stuck payment" (offline, so the accelerator says offline) and show the quote screenshot.
- If Electrum asks for a password: the demo wallets have none.
