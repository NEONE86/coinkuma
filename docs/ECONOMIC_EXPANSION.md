# Fixed economic expansion and bounded receipt pilot

This follows PR15 without altering its sealed Alpha implementation or claims.
The actual 2021–2022 expansion plus the full compatible previously used PR12
archive rejects the pooled 4h/24h candidates under the frozen exploratory rules.
It establishes no investment edge. Event timing remains unresolved: matched
price selection uses the same event clocks and cannot identify timing value.

## Historical execution

`one_trading.historical_expansion` reuses the existing economic selectors,
all-hour coverage checks and transitive 26h clusters. It freezes a data-use
manifest, all calendar blocks, 2h entry delay, 4h/24h exits, costs and cluster
weighting before any new performance calculation. The original 26h lockout is
preserved for both horizons, including across quarter boundaries.

The registered acquisition fetched 63 public Binance spot/funding responses;
the older futures/index archive cannot replace spot taker-quote observations.
Unknown previous use is exploratory, never untouched out-of-sample evidence.
The chronological first two full years fit the existing request limit; the
unfetched remainder is budget-blocked. No additional horizon search occurred.

The first strict read failed on 11 short terminal bars. Its task, charge and
source version remain preserved. An explicit successor reused exactly the same
receipts, quarantined those bars without extending them, and completed with
372 missing common clocks out of 18,237 expected clocks. No additional HTTP was
issued by this repair. There were 17,865 complete clocks, 140 accepted economic
events in 123 temporal clusters and 606 full-clock price-policy entries.

Equal-cluster means (USDT per fixed 1,250 nominal opportunity):

| Fixed horizon | Economic net | Economic minus matched price selector |
|---|---:|---:|
| 4h | -10.676134 | -5.221028 |
| 24h | -7.217318 | -5.255968 |

These are descriptive historical scores, not independent forward observations,
calibrated confidence intervals, a compounded portfolio or risk-adjusted alpha.
The nine absorption entries alone remain a small sample. The archive includes
the five old events; it does not count them as newly independent evidence.
All eight quarterly blocks and the prior archive block are retained, including
no-signal periods. Separate sleeves must not be added into funded returns.
Historical opens and assumed spread/slippage are not executable old quotes.

```sh
python -B -m one_trading.historical_expansion --runtime EXISTING_BATCH register --manifest data-use-manifest.json
python -B -m one_trading.historical_expansion --runtime EXISTING_BATCH run
# Only for the explicitly recorded short-bar failure, preserving the parent:
python -B -m one_trading.historical_expansion --runtime EXISTING_BATCH repair
python -B -m one_trading.historical_expansion --runtime EXISTING_BATCH run
# Offline independent audit, no ONE imports or network:
python -B scripts/verify_historical_expansion.py EXTRACTED_BUNDLE/latest
```

The cached final `run` returns the same receipt without repeating acquisition,
analysis or charges. Do not initialize a replacement authority to get more quota.
Interrupted external work with no durable result is terminal and conservatively
charged the reservation; a durable result recovers its acknowledgement exactly
once. The data manifest and imported original cached bytes are private inputs,
not invented by the public checkout.

## Forward receipt feasibility

`one_trading.receipt_pilot` is the last step of the existing Windows wrapper.
It creates no scheduler and changes no task account, default frequency or budget.
The two PR15 designs remain unmodified and their missed starts are recorded.
The separate receipt pilot samples every four hours, receives features within
five minutes, and records event decisions before any entry/exit quote request.
It uses the same gates, 2h delay and 26h lockout. Its purpose is missingness,
receipt latency, spreads and event occurrence, not profitability validation.

Each HTTP attempt has a durable intent and response/error bytes, receipt clock,
elapsed time and shared-authority settlement. Unknown attempts count against
limits. All batch HTTP/retrieval activity also counts against a conservative
rolling 90-attempt limit; no-signal, missing and budget-blocked are different.
The pilot handles one bounded job after critical maintenance per invocation.
It stops at the earlier of 14 days and the existing research-window deadline;
it does not silently extend pending observations. Every HTTP attempt checks the
absolute deadline. Missing books remain missing; there are no order or fill APIs.

```sh
python -B -m one_trading.receipt_pilot --runtime EXISTING_BATCH status
python -B -m one_trading.receipt_pilot --runtime EXISTING_BATCH service
python -B -m one_trading.receipt_pilot --runtime EXISTING_BATCH stop
```

Registration alone is not evidence the scheduler or market call ran. The private
delivery separates the latest actual scheduler receipt, features, signals,
quotes, model subprocess starts, valid responses and matured outcomes. A future
normal Alpha case keeps its original costs, validity and maturity; no expired
case is reopened to demonstrate recovery. Provider usage summaries are not bills.

Validation: 487 inherited tests plus 11 focused regressions; original reviewer
probes retained. The reviewer's two POSIX mocked path expectations need native
`Path` normalization on Windows; the unchanged probe and exact adaptation are
both delivered. Synthetic tests are not financial performance evidence.

The highest-value next research question is whether an actually received end of
forced liquidation creates a cost-adjusted advantage over matched price-only
and cash decisions in the existing sealed Alpha experiment. The losing historic
4h/24h variants are not adopted; the receipt pilot only measures feasibility.
