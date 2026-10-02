# Manual test checklist

Dev setup: Electrum 4.8.2 from source with the plugin symlinked; `-D` always points at a test data folder.

## Load
- [ ] Loads from source: log shows `loaded plugin 'lowtide'`, no tracebacks.
- [ ] Loads as a zip in the official 4.8.2 binary: Tools ▸ Plugins ▸ Add ▸ enable ▸ plugin password; status bar indicator appears.

## Forecast (F1)
- [ ] Status bar shows the next-block rate and the next low tide; tooltip explains; click opens the panel.
- [ ] Panel renders chart, band, windows, now marker; tooltips on hover.
- [ ] Rate checker: 0.4 gives an ETA; deadline planner gives a rate and a "wait until".
- [ ] The suggestion line names the pile-jump rate with an ETA; "Use" opens Electrum's send dialog prefilled at a sub-1 rate; Pay… shows that rate in the confirm dialog.
- [ ] On a server with a 1 sat/vB relay fee the status bar turns amber with "server blocks sub-1".
- [ ] Offline: the panel shows cached data with "data as of"; no errors.
- [ ] Notification: Settings ▸ Send test notification shows a banner and the in-app cue; the "low tide now" demo switch triggers the real one once.
- [ ] Nostr: Settings ▸ npub + Send test DM arrives on the phone; the low-tide and stranded alerts also arrive.

## Stress test, tech check, consolidation (F2)
- [ ] Stress table rows: now, low tide, spikes, custom slider; uneconomical coins counted; fiat shown when exchange rates are on.
- [ ] Native segwit wallet: green tech check. Legacy wallet: amber, ~54% saving, Migrate… button.
- [ ] Consolidate: groups by label, unlabeled separate; merge checkbox shows the linking warning; floor/pile-jump/low tide/custom rates; Prepare now opens the preview at the chosen rate; frozen and unconfirmed coins are skipped.
- [ ] Coins tab ▸ right-click ▸ Consolidate selected at low tide…
- [ ] Migrate: destination validation (network, not ours, native segwit); preview to the destination.

## Rescue (F3)
- [ ] Rescue… button only on unconfirmed, broadcast transactions (not on local or confirmed ones).
- [ ] Outgoing RBF-able tx: Small RBF step and RBF bump available; Choose opens Electrum's preview of the bump at the stated rate.
- [ ] Incoming tx: only Wait and Accelerator active; reasons shown on the greyed rows.
- [ ] Recommendation line changes with the deadline.
- [ ] Get estimate (mainnet): quote line with bid + base fee; Choose ▸ Continue ▸ invoice; BOLT11 amount matches the quoted total; QR shown; Pay with Lightning only when the wallet can pay; both confirmations appear.
- [ ] Stranded: the "stranded" demo switch flags unconfirmed txs, status bar shows ⚠, panel shows Rescue…, one notification per tx.
