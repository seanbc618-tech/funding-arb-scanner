# funding-arb-scanner

English | [简体中文](README.zh-CN.md)

Funding-rate research and an OKX Mode A execution prototype. The scanner and cross-exchange pool do not place orders. The executor defaults to dry run; submitting orders requires an explicit `--live` flag.

The original core consists of seven business scripts and a notification entry point, using ccxt and Python's built-in SQLite for accounting. It scans perpetual funding rates across 16 CEXs/DEXs, filters by trading volume, and uses seven-day historical averages to estimate annualized rates and stability. It produces three tables:

1. **Single-exchange funding ranking** — assets with the highest absolute funding rates.
2. **Cross-exchange funding spreads** — annualized funding-rate differences and estimated break-even days for shorting an asset on exchange A and going long on exchange B.
3. **Mode A candidates** — buying spot and shorting a perpetual on the same exchange, with nominal annualized estimates after trading fees.

> The delivery, deployment, and validation notes below preserve the historical record in the Chinese README. They are not a fresh verification of running services or exchange capabilities. Later dated deployment notes supersede earlier delivery status where stated.

## Usage

```bash
pip3 install ccxt
python3 scan.py
```

Connections are direct by default. If a local proxy is needed, set `export SCAN_PROXY=http://127.0.0.1:12334` (the Hiddify mixed proxy port in the original setup).

Results are printed to the terminal and saved as `funding_<timestamp>.csv` and `spread_<timestamp>.csv`.

Research and execution entry points:

```bash
python3 verify.py [COIN]         # Check 28 days of history in weekly slices; defaults to BTC
python3 pool.py                 # Mode C cross-exchange research dry run; separate from Mode A
python3 trade_a.py candidates   # Query OKX Mode A candidates using public market data
python3 trade_a.py open ZEC 200 # Dry run; 200 is target notional per leg, not total capital
python3 trade_a.py status
python3 trade_a.py close ZEC
```

Live actions require `--live` and the environment variables `OKX_API_KEY`, `OKX_SECRET`, and `OKX_PASSWORD`. Supported account settings are OKX Futures mode (`acctLv=2`), net position mode (`net_mode`), cross-margin perpetuals, and cash spot. The program checks these settings without changing account mode, leverage, or transferring funds. Other programs must not concurrently manage the same asset's spot balance or perpetual position.

Dry-run and live state are stored separately in `positions_a_dry.json` and `positions_a_live.json` beside the scripts. Process locks prevent concurrent operations within the same mode; state writes are atomic. A nonempty legacy `positions_a.json` blocks live open, close, and recover operations until manually reviewed and migrated. Dry runs and read-only checks do not automatically read or modify that legacy file.

Order identifiers are persisted before submission. A successful `create_order` return is not evidence of a fill. The executor queries the original order's terminal status, actual fills, and fees, then sizes the short against actual spot quantity after fees charged in the base asset. Perpetual closes explicitly use `reduceOnly`.

Live opens and closes use IOC limit orders with price bounds. The exchange cancels any unfilled portion. Before submission, the executor refreshes the order book and records the limit price, book timestamp, and estimated VWAP. Recovery follows actual remaining quantities after partial fills.

For timeouts, nonterminal orders, or missing fee evidence, first run `python3 trade_a.py recover ZEC --live`. Recovery only queries original orders and reconciles the account: it does not open replacement positions, resubmit, automatically cancel, or roll back orders. An unlocated order remains pending reconciliation. After verification, `close ZEC --live` exits the remaining quantity. State is saved after each leg fills; a partial close can be followed by another close. Dust below minimum size remains for manual handling. Closed-cycle history is retained, and status reconciles actual account positions with local remaining quantities.

Real-account operation and unattended execution have not been accepted as production-ready. The original delivery did not install trading schedules.

## Mode A accounting, checks, and management

```bash
python3 check_a.py ZEC --notional 200           # Public order-book and planned hedge checks
python3 check_a.py ZEC --notional 200 --account # Also check account mode, balances, and margin
python3 check_a.py ZEC --account                # Reconcile local live positions with the account
python3 accounting.py ZEC                      # Read account fills/bills for the current cycle
python3 accounting.py ZEC --previous 1          # Previous cycle; position records are unchanged
python3 manage_a.py                            # One dry-run status check and recommendation
python3 manage_a.py --account                  # One read-only account recommendation
python3 manage_a.py --live                     # Explicitly permit a real close for this exit decision
```

`check_a.py --account` and `accounting.py` query private endpoints without submitting trades. Account checks require an explicit finite, nonnegative spot balance and list responses from position and open-order endpoints. Missing keys, nulls, or invalid responses block action; they must not be interpreted as no positions or orders. Live state remains in `positions_a_live.json`, with the legacy-state migration gate still enforced.

Conservative defaults are defined at the top of `check_a.py`:

| Check | Default boundary | Action |
|---|---|---|
| Order book | Both legs must be executable within 50 levels; each limit price within 20 bps of the best price | BLOCK if insufficient; closes also respect the price bound |
| Freshness | Book age at most 5 seconds; entire check at most 10 seconds | BLOCK on missing or stale data |
| Entry basis | Absolute spread between executable average prices at most 100 bps | Block entry if exceeded |
| Entry capital | Spot cost plus 1x perpetual notional reserve, plus a 10% buffer | Block entry if insufficient; no transfers or leverage changes |
| Net exposure | At most 2% of the larger leg's asset quantity | Block entry; recommend EXIT for an existing position if exceeded |
| Margin ratio | Raw OKX `mgnRatio` greater than 3 (300%) | Recommend EXIT on breach; BLOCK if missing |
| Short liquidation distance | `(liquidation price - mark price) / mark price` greater than 10% | Recommend EXIT on breach; BLOCK if missing |
| Current funding rate | Nonnegative | Recommend EXIT if negative; predicted rates can still change |

These are adjustable engineering thresholds, not optimal parameters validated in live trading. Other account positions affect cross margin; closing this strategy does not guarantee removal of account-wide risk. The strategy does not automatically open positions, chase higher rates, or change leverage.

`manage_a.py` returns HOLD / EXIT / BLOCK / CLOSED. It recommends exiting `opening`, `closing`, or `needs_close` states when the evidence can be reconciled. Closing is called only with both `--live` and EXIT. Pending orders, account mismatches, insufficient depth, or incomplete evidence produce BLOCK.

F02 separates the result into `risk.status/reasons/gaps` for known risks and evidence gaps, `execution.status/limitations` for current action constraints, and `data_status` for evidence completeness. For example, a stale book with a margin breach yields `risk.status=EXIT_REQUIRED`, `execution.status=BLOCKED`, and `action=BLOCK`, retaining the risk reason. `CLEAR` only means no existing rule was triggered within the checked scope; `risk.scope=local_and_public` excludes account verification. `READY` is not trading authorization: the manager follows the top-level `action`.

When risk evidence is incomplete, a known breach retains `EXIT_REQUIRED`; without a known breach, the result is `UNKNOWN`. Entry-risk breaches produce `ENTRY_BLOCKED` with top-level BLOCK. `checked_at/completed_at/evidence_at` are local check/API-return times, not original exchange update times. Checks exceeding 10 seconds, or books older than 5 seconds at completion, block action without erasing known risks. CLOSED applies only to local historical records with no pending orders and both legs confirmed at zero; it does not prove the real account is flat.

Failed exits return NEEDS_ATTENTION and retain orders and remaining quantities. Recover/reconcile before handling them again. This is a single-run entry point, not a daemon or cron installer. Market and account checks are not an atomic snapshot, and IOC orders can fill partially.

`accounting.py` stores raw fills, bills, and reports in the ignored `accounting_a.sqlite`. It deduplicates by hashed account identity, record type, and billId. Changed content under the same ID is a conflict; prior evidence is never overwritten. Pagination follows billId until an empty page, and fill totals and fees are reconciled by order ID. Data beyond the conservative 89-day source window, pagination failures, or inconsistent records yield `INVALID_DECLARED_GAP` / null. The program does not request long-term archives automatically or fabricate missing records.

Trade cash flows and funding postings use Decimal. Spot fees paid in the base asset reduce sellable quantity and are not deducted twice. Unpriced fees, unattributed fills/account charges, open positions, and unconfirmed orders prevent a net-profit conclusion. `RECONCILED_AS_OF_QUERY` and `net_pnl_usdt` require at least one hour after closing plus successful fill, fee, and bill reconciliation. This status applies only to the queried API window; later funding postings or vendor corrections remain possible, so queries may be repeated. Capital return and annualized return stay null because total strategy capital has not been allocated.

Bill fields and cursors follow the [OKX account bills documentation](https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-bills-details-last-3-months). Validation used offline fixtures; real-account archive completeness still needs separate verification before deployment. Order parameters follow the [OKX V5 order documentation](https://www.okx.com/docs-v5/en/#order-book-trading-trade-place-order), with offline inspection of requests generated by ccxt 4.5.77. This does not constitute real-account API acceptance.

`candidates` is a research filter; `open` is a manual execution-path validation entry point, not an APR-driven automatic trader.

The nominal return estimate is:

```text
annualized historical funding rate
  - total opening/closing fee rate for both legs × 365 / assumed holding days
```

Legacy fields `apr_a_net` in CSV and `net_apr` / `entry_net` in the pool are retained for compatibility and refer only to this estimate. It excludes actual slippage, basis changes, and financing costs, and does not divide by total committed capital such as spot funding plus margin. Actual strategy P&L comes from accounting records; scanner estimates are not account net returns.

Missing volume or history is marked `INVALID_DECLARED_GAP`, with reasons in scan CSVs and APR left blank. History shorter than five days, stale history, or internal gaps exclude a candidate. Mode A requires at least 90% positive funding observations and at least $10 million turnover on each leg. `verify.py` checks endpoints and internal intervals for full weeks; CSVs retain `symbol` identity, and old CSVs are not rewritten.

`pool.py` stores positions in `positions.json` and appends open/hold/close decisions to `pool_log.csv`. Separate entry and exit thresholds provide hysteresis: defaults are 8% to enter, 3% to exit, and a minimum three-day hold. A rotation incurs four taker fees (approximately 0.22%, or 2.7 annualized percentage points over 30 days). Using one threshold for both entry and exit can cause costly churn.

## Configuration (constants at the top of the files)

### Telegram run notifications

The personal Telegram Notifier plugin determines the destination group through private configuration. These entry points send a summary after each run, including an exit code on failure, without full logs, credentials, personal paths, or server addresses:

```bash
python3 notify_run.py manage_a.py           # Dry-run check and report
python3 notify_run.py manage_a.py --account # Read-only account check and report
python3 notify_run.py accounting.py ZEC     # Accounting and report
python3 notify_run.py scan.py               # Report after scanning
```

The local setup discovers a unique installed plugin version and private user configuration without copying tokens into the project. Other environments can set `TELEGRAM_NOTIFY_SCRIPT`, `TELEGRAM_NOTIFY_CONFIG`, and `TELEGRAM_NOTIFY_PYTHON`, or use the plugin's `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` variables.

Unconfirmed delivery returns exit code 2, unless the business command itself failed, in which case its exit code is preserved. Check the group message before doing anything else; never blindly rerun a trade. The wrapper does not add `--live`, recover automatically, or start schedules. Running the underlying scripts directly does not send notifications.

Manual per-run notifications continue to use `notify_run.py`. F03 adds the independent `notify_events.py`, which reads monitoring snapshots and sends events. Remote OKX credentials and notification configuration are stored in separate private files. At the documented deployment stage, the remote service was enabled; without its private environment configuration, it remains `CONFIG_MISSING` instead of falsely reporting an empty account.

### Continuous read-only position monitoring

`monitor_a.py` only queries local live state and the OKX account. It does not call order submission, cancellation, or closing functions. `--account` requires the three OKX credentials. Missing credentials produce `CONFIG_MISSING`, never “no positions.” Each cycle emits JSON to the log and atomically writes `monitor_a_status.json` with mode 600.

The VPS setup uses user-level systemd, without root:

```bash
mkdir -p ~/.config/funding-arb
chmod 700 ~/.config/funding-arb
# Put the three private OKX_* credentials in ~/.config/funding-arb/monitor.env; chmod 600
systemctl --user daemon-reload
systemctl --user enable --now funding-arb-monitor.service
systemctl --user status funding-arb-monitor.service
journalctl --user -u funding-arb-monitor.service -f
cat ~/funding-arb/monitor_a_status.json
```

This service has no `--live` flag and does not trade automatically. F03 deployed the plugin sender and private notification configuration separately; tokens are excluded from the repository, logs, and release packages.

| Parameter | Default | Meaning |
|---|---|---|
| `MIN_VOL_USD` | $10 million | Minimum 24-hour turnover to filter out illiquid assets |
| `LOOKBACK_DAYS` | 7 | Historical averaging window |
| `MIN_HIST_DAYS` | 5 | Minimum history for ranking, excluding newly listed contracts |
| `MIN_SPREAD_APR` | 10% | Minimum annualized cross-exchange spread for ranking |
| `MIN_SAME_SIGN` | 85% | Required funding-direction stability for both legs |
| `HOLD_DAYS` | 30 | Assumed holding period for amortizing entry/exit costs |
| `MAJORS` / `MAJORS_ONLY` | 32 major assets / True | Mode A asset universe |
| `STABLES` | USDT/USDC/USD | Recognized stablecoin collateral currencies |

## Data pitfalls that motivated this project

Raw ccxt responses contain traps that can produce attractive but misleading annualized estimates. The following describe observations recorded during development.

**Reported settlement intervals can be missing or wrong.** Binance's `fetch_funding_rates` uses premiumIndex, which does not return `fundingIntervalHours`, leaving ccxt with `None`. Assuming eight hours when the actual interval is four halves the annualized estimate. The scanner infers intervals from the median gap between historical timestamps and makes an additional fetch when it detects limit truncation.

**Contract `baseVolume` can be contract count, not coin quantity.** Multiply by `contractSize` to obtain asset quantity. A MEXC BTC contract represented 0.0001 BTC; omitting this conversion overstated volume by 10,000 times, once producing an implausible $22 trillion daily turnover. `quoteVolume` conventions also differ; the observed OKX value represented coin quantity rather than dollars. The scanner calculates volume both ways and uses the smaller value.

**A single funding observation is not a reliable annualized estimate.** Multiplying a single rate by 1,095 can suggest 500% APR, while a seven-day average may be only tens of percent. Direction stability also matters: the same-sign ratio measures the proportion of periods with the same funding direction. Around 50% offers little directional persistence; reversed funding can erase receipts while fees remain.

**A newly listed contract's “seven-day average” may contain only four observations.** Early rates can be highly volatile and disappear as the market settles. This motivates `MIN_HIST_DAYS`.

**An asset ticker is not a contract identity.** BTC may have USDT, USDC, or USD1 contracts on the same exchange. Matching by ticker alone conflates distinct contracts. The scanner distinguishes `(asset, settlement currency)`.

**Some responses appeared to contain placeholder rates.** In the recorded BingX sample, more than half the contracts returned `lastFundingRate=0.0001`; 23 of 37 rows had identical annualized rates. BingX was removed from the configured list.

## Settlement currency (`settle`)

Cross-exchange legs may use different collateral currencies. A BTC long on a USDC venue and a BTC short on a USDT venue can offset BTC exposure while retaining stablecoin depeg risk. The original notes cited historical differences of approximately 0.1–0.3%; this is not a bound on possible depegs. USDC venues such as backpack, hyperliquid, pacifica, and woofipro can therefore participate in cross-exchange matching.

Mode A requires matching spot and perpetual settlement currencies: the implementation does not match USDT-funded spot against a USDC-collateralized short.

## Exchanges

Configured integrations: binance, bybit, okx, bitget, gate, coinex, bitmex, krakenfutures, hyperliquid, aster, pacifica, woofipro, backpack, mexc, kucoinfutures, and phemex.

Recorded exclusions and reasons:

- **htx** — ccxt implemented bulk funding only for inverse contracts; linear contracts raised `NotSupported`.
- **bingx** — apparent placeholder funding-rate data, as described above.
- **lighter** — no funding-history support in the ccxt integration used.
- **paradex / extended / apex** — no individual funding endpoint in the integration used.
- **dydx** — no spot market.
- **bitrue / latoken** — no funding history.

Blofin was excluded because of public-endpoint connection problems. Venues without bulk `fetchFundingRates` support (backpack, mexc, kucoinfutures, and phemex) are queried per symbol, which is slower.

## Disclaimer

This is a personal research and execution prototype, not investment advice. Funding arbitrage can appear neutral while still carrying liquidation, delisting, single-leg failure, and exchange failure risks. Independently verify every number before using real funds.

## License

MIT

### F03 — Continuous event notifications

`funding-arb-notify.service` independently reads `monitor_a_status.json` every 10 seconds without accessing the account or invoking trading entry points. It notifies on changes to status, exception category, local phase/confirmed remaining quantity/pending state, and margin or liquidation breach categories already present in the snapshot. Identical events are deduplicated across restarts. Frequent changes are coalesced into the latest snapshot, with at least 300 seconds between sends globally; transient events are not guaranteed individual delivery. Timestamp-only changes and market changes without a threshold breach do not trigger messages. Risk thresholds come from `check_a.py`, and reports are limited to snapshot evidence.

Before sending, intent is atomically persisted as ATTEMPTING; confirmed delivery becomes CONFIRMED, otherwise DELIVERY_UNKNOWN. An interrupted ATTEMPTING state or unknown receipt prevents resending the same event. This trades possible missed delivery for avoiding blind duplicates. Corrupt state pauses notifications instead of resetting it. Notification failures write only allowlisted status to the independent service log, without rerunning monitoring or business commands. The ignored `notify_events_state.json` has mode 600; do not delete it to retry uncertain delivery.

Messages contain only fixed statuses, enumerated reasons, restricted asset codes, and numbers. Raw exceptions, account identifiers, and configuration are not forwarded. Local open/closed phases are observations, not fill evidence. Snapshots older than 180 seconds or dated in the future are not sent as current facts. Scheduled reports and external-loss alerts belong to F06; F03 cannot detect its own loss of connectivity.

Remote `~/.config/funding-arb/notify.env` sets `TELEGRAM_NOTIFY_SCRIPT`, `TELEGRAM_NOTIFY_PYTHON`, and `TELEGRAM_NOTIFY_CONFIG`. Notification credentials belong in a separate mode-600 file, never in the repository or service unit. Inspect with `systemctl --user status funding-arb-notify.service` and `journalctl --user -u funding-arb-notify.service -n 20`. Recorded validation, version hashes, and deployment boundaries are in `artifacts/F03/REPORT.md`.

### F04 — Incremental funding and fill collection

`accounting.py` retains per-cycle accounting and adds account-wide raw-evidence collection:

```bash
# Load credentials into the process environment first; dates are UTC. Examples only.
umask 077
python3 accounting.py --account-sync --since 2026-09-08
# Continuous collection; Ctrl+C stops it. No background service is installed.
python3 accounting.py --account-sync --since 2026-09-08 --interval 300
```

Collection does not require strategy positions. `EVIDENCE_COLLECTED` only proves that the specified endpoint window was queried; profit remains null. SQLite `collection_windows` records coverage and pagination cursors by account/data stream, committing successful pages and cursors together. Duplicate IDs are not reinserted. `evidence_conflicts` preserves observed corrections without overwriting original evidence. Conflicts block net-profit conclusions; they are neither cleared automatically nor resolved by choosing the newest value. The last seven days are rechecked with overlap; older late postings or corrections require explicitly widening the review window.

Beyond 89 days, genuine evidence remains usable if complete local query coverage already exists. Old evidence rows alone do not prove complete coverage. Missing coverage or collection interruptions spanning the source window remain `INVALID_DECLARED_GAP` / null, without fabrication or automatic long-history requests. Account-wide `ALL:SPOT` / `ALL:SWAP` and per-contract coverage are recorded separately; account-wide coverage is not automatically converted to strategy-cycle coverage. This delivery installed no collection service; continuous scheduling integration was deferred to F12. The F04 report records the remote validation copy and separate raw database.

### F05 — Read-only position P&L and capital reports

`python3 portfolio_a.py --db accounting_a.sqlite --markdown` produces separate account and strategy reports; omit `--markdown` for JSON. The process needs the three OKX credentials; a systemd environment is not automatically inherited by an ordinary SSH shell. This entry point has no `--live`, places no orders, and does not write to the accounting database. P&L requires F04 evidence for the same account, contract, and period, plus locally confirmed orders. External positions are not automatically attributed to this strategy.

Reports separate posted funding, fees in their original currencies, realized/unrealized P&L under moving weighted-average cost, estimated exit costs, and estimated post-exit net P&L. Base-asset spot fees already affect net quantity and cost and are not deducted twice. Unconvertible fees remain gaps. Valuation uses fresh book midprices; exit estimates use both legs' VWAP and account taker rates. Neither is a fill or settlement guarantee. Closed net P&L as of query is shown only for closed positions satisfying the original accounting checks.

Spot cost/value is shown in USDT, while OKX account equity/initial margin is shown separately in USD. Account equity includes external assets. Without strategy capital allocation records, `strategy_nav_usdt` and `capital_return` remain null; total account equity cannot stand in for strategy return. Examples, validation, and the remote report location are in `artifacts/F05/REPORT.md`. Insufficient quotes, fees, coverage, or attribution leave fields null.

### F06 — Scheduled reports and an independent watchdog

An independent systemd oneshot/timer runs `report_watch.py` every minute to check monitoring snapshots, notification heartbeats, and service status. The stale threshold is 180 seconds; exception changes/recovery have a 300-second cooldown. A running process with a stalled snapshot still triggers an alert. Checks on the same machine cannot cover loss of the entire VPS or network.

Defaults are hourly summaries and a daily report at 08:00 Beijing time, including that hour's summary. `--summary-hours 4` changes the interval; `0` keeps only daily reports and exceptions. Restarts report the current period without flooding historical messages. Persisted send intent prevents duplicates; unknown receipts are not retried. Do not casually delete `report_watch_state.json`.

Daily reports require F04 coverage of the full calendar day for the same account; gaps remain null. Account funding and fees are not strategy daily profit, for which complete evidence was not yet available. Reports query read-only and do not start collection. The local default is `accounting_a.sqlite`; the deployment explicitly reads the separate existing F04 evidence database. Collection scheduling was deferred to F12.

The deployment uses modules in a dedicated release directory, leaving trading modules in the original runtime directory unchanged. `funding-arb-reports.timer` should be enabled/active. An inactive/dead oneshot service after completion is normal; check `Result=success`. Delivery and rollback details are in `artifacts/F06/REPORT.md`.

### F01–F06 — Combined review status

The 2026-09-09 review fixed five local issues, with 198 checks passing; patches had not yet been deployed at that review stage. Differences and limitations are in `artifacts/F01-F06-review/REPORT.md`. Historical regression checks were restored to `checks/funding_arb_regression.py` and `checks/funding_modules_regression.py`, removing their dependency on temporary directories.

### F07 — Persistent execution state and startup gates

New cycles persist trade IDs, both legs' targets and confirmed remaining quantities, order identity/confirmation state, fees, and exposure. `trade_a.py startup` checks dry-run state; `--live startup` queries the real account and maintains execution locks/path binding without placing orders. Open or unknown orders, unfinished actions in other assets, account-mode/ownership mismatches, or reconciliation failures block new actions.

Open/close/recover function entry points acquire both state and account locks. Account locks under `~/.local/state/funding-arb/execution-locks/` are shared across checkouts for the same user on the same host and bind to one state path. Do not delete lock files to bypass binding; there is no cross-host guarantee. After interruption following order acceptance, only the original client order ID may be queried; the order must not be resubmitted. Observed nonterminal fills remain separate from terminally confirmed quantities.

Unversioned legacy state remains readable; modification requires manually reviewed migration. This delivery did not migrate, deploy, or enable live trading. Default dry run is not the F08 market-data paper model. Acceptance boundaries are in `artifacts/F07/REPORT.md`; F07 was pending review at this stage.

### Recorded deployment status — 2026-09-09

F01–F07 and the combined/F07 review patches were deployed to release directory `f01-f07-20260909-586bd8ed1c65`. Monitoring, event notifications, and scheduled reports were restored. The F07 executor was installed without starting trading. Earlier “not deployed” statements describe historical delivery stages. Acceptance, hashes, and rollback boundaries are in `artifacts/deploy-F01-F07-20260909/REPORT.md`.

## F08 — Simulated fills using real order books (local delivery)

These independent commands use only public market data and need no OKX credentials. Initialization explicitly sets simulated capital and fees. The following fee rates are examples, not your account's actual tier:

```bash
python3 -B paper_a.py init --cash 10000 --spot-fee 0.001 --perp-fee 0.0005
python3 -B paper_a.py open BTC --notional 200
python3 -B paper_a.py status
python3 -B paper_a.py close BTC
```

`--notional` is the target USDT amount for one leg; both legs require additional capital and fees. Insufficient depth produces partial fills and cancels the remainder. `needs_close` retains filled positions for subsequent close operations. `pending` means persistence is incomplete: do not automatically retry or delete the ledger; inspect intent and state first. Repeated init does not overwrite an existing ledger.

State is isolated from live trading in `paper_a_state.json`. Perpetuals reserve 1x initial notional and all fees are simulated in USDT. This F08 stage did not include funding settlement, dynamic margin/liquidation, continuous scheduling, or any guarantee of live fills. See the [F08 acceptance report](artifacts/F08/REPORT.md).

The [F08 review](artifacts/F08-review/REPORT.md) fixed depth omitted by limit-price rounding and close-capital checks. Spot sale proceeds can pay simulated fees; closing the short accounts for released margin and price losses. Insufficient capital preserves the position and rejects that simulated fill.

## F09 — Single-period simulated funding (local review and public-API validation passed)

```bash
python3 -B paper_a.py settle BTC --at-ms <actual_settlement_timestamp_in_milliseconds>
python3 -B paper_a.py status
```

Replace the placeholder with `fundingTime` from historical funding data and wait until at least that minute has ended. Paper funding uses the confirmed short position at that time, the actual settled rate, and that minute's opening mark price. The minute-open convention is a simulation assumption, not an exact account bill. Repeated calls do not pay twice.

Results are stored in the `funding` field of `paper_a_state.json`: POSTED means simulated posting, NO_POSITION means no position at that time, and `INVALID_DECLARED_GAP` / `payment_usdt=null` means missing data. Gaps do not change cash. Missing execution times on old F08 orders are not backfilled or guessed. One successful period does not establish complete historical coverage. F09 started no timer and changed no real ledger. See the [F09 report](artifacts/F09/REPORT.md).

Additional public-API validation passed: real settled rates and mark prices drove accounting for synthetic positions, including restart deduplication. The full suite passed 268 checks. This validation did not deploy or trade live. See the [F09 review](artifacts/F09-review/REPORT.md).

## F10 — OKX net-return ranking by notional (local delivery)

```bash
python3 -B rank_a.py BTC ETH --notional 1000 --hold-hours 720 --margin-ratio 1 --reserve-usdt 100 --basis-stress-bps 50
```

Uses existing OKX environment credentials to read account fee rates and market data, returning JSON. Margin ratio and reserves are explicit scenario assumptions, not actual available account capital. Ranking uses expected net profit divided by total committed capital. Missing data, insufficient depth, or nonpositive estimated net profit exclude candidates; all results may be HOLD_CASH. CANDIDATE is for review, not execution authorization.

Current forecasts and historically settled rates are separate. Four-trade book/fee costs, expected income, net profit, break-even days, and basis stress are itemized. See the [F10 report](artifacts/F10/REPORT.md) for assumptions and limitations. `scan.py` remains the original research scanner, not automatic trading logic.

## F11 — Paper portfolio budgets (local delivery)

```bash
python3 -B allocate_a.py reserve ranking.json --per-coin-gross-usdt 2500 --total-gross-usdt 5000 --max-positions 2 --buffer-usdt 100
python3 -B allocate_a.py execute <reservation_id>
python3 -B allocate_a.py release <reservation_id>
```

Limits apply to the sum of both legs' notionals; choose your own values. Input is raw F10 JSON, with matching paper fees and a margin ratio of 1. Reservations reduce capital available to other candidates. Partial fills retain actual capital usage and release the remainder. Unknown orders block execution. Quotes and reservations last only five seconds; expired reservations require release and a fresh quote. At this delivery stage, F12 automatic orchestration was not yet available. After initial configuration, direct paper open cannot bypass limits; close remains available. See the [F11 report](artifacts/F11/REPORT.md).

## F12 — Paper main loop (local delivery)

```bash
python3 -B paper_loop.py BTC ETH --cycles 10 --interval 1 --notional 1000 --hold-hours 720 --per-coin-gross-usdt 2500 --total-gross-usdt 5000 --max-positions 2 --buffer-usdt 100 --basis-stress-bps 50
```

First initialize the F08 ledger and configure read-only account fee credentials, keeping F11 limits and fee assumptions consistent. Scanning runs in a separate process. The main loop checks existing positions, consumes ranking/funding data, and simulates execution within budget. The default is one cycle; `--cycles 0` runs continuously. An initial WAIT_SCAN is normal. Ctrl-C stops the process without closing positions. Status is written to `paper_loop_status.json`.

Limits are examples. This F12 delivery has no live path or automatic exits. Evidence gaps and incomplete execution block new positions. Expired quotes are not executed, though network calls may still be delayed. See the [F12 report](artifacts/F12/REPORT.md).

The combined F10–F12 review fixed mismatches between executed and ranked quantities, settlement boundaries, stale position checks, and batch reservation ownership. All 322 checks passed; these patches were not yet deployed at that review stage. See the [combined review](artifacts/F10-F12-review/REPORT.md).

## F13 — Automatic exits (local, pending review)

Enable only by appending `--auto-exit` to the existing `paper_loop.py` command. Optional `--max-hold-hours 720` sets an explicit maximum holding period; none is set by default. Exit priorities are exposure risk, confirmed remaining exit quantities, negative funding, and an explicit holding deadline. Missing data or unknown orders pause actions. Exits retain the existing conservative order-book bounds. The online service had not enabled this feature at the documented stage. See the [F13 report](artifacts/F13/REPORT.md).
