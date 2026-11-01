# Bounded Bybit public source capture (PAPER input only)

This is code-only; nothing runs by default. It captures only the three public
Bybit spot `orderbook.1` topics. There are no credentials, model calls, exchange
orders, schedulers, or automatic deployment. It does not run a strategy. Public
endpoint and data semantics: [connection](https://bybit-exchange.github.io/docs/v5/ws/connect),
[order book](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook).

An operator with separate authorization, network access and an already-installed
compatible `websockets` package (with `recv(decode=False)`) can run exactly:

```sh
python -m one_trading.ai_native.bybit_forward --output-dir /private/existing-parent/bybit-pilot-unique --duration-seconds 86400 --max-bytes 1000000000 --enable-public-network
```

The target directory must not exist, must be outside this repository, and its
parent must already exist in appropriately private storage. Confirm free space,
permission, source availability and approval before starting; do not commit or
publish any runtime files. Output contains original WS message bytes encoded
losslessly as base64 and their SHA-256, local UTC arrival and monotonic elapsed
sequence, exact PAPER packet JSONL and per-packet source receipt IDs, missing
samples, connection resets and terminal metadata. Hashes prove byte consistency,
not exchange authenticity. There is no restart/resume or quota reset. The cap
includes all three output files and reserves terminal space; it does not measure
filesystem metadata. The process stops at 24 hours or the cap, whichever comes
first. Ctrl+C stops it; no process continues in the background.

Each actual sample occurs at most once per one-second monotonic interval, after
receipts, with no backfilling. Sampling skips periods delayed by the process.
Each of the three latest received quotes must be strictly younger than five
seconds in both local receipt and source matching-engine time, and must not be
from the future; malformed books invalidate the affected symbol. Reconnection
clears all quotes. The exchange `cts` supplies `exchange_at`; `ts`, `u` and
`seq` remain in original payloads. Bybit may repeat a snapshot with the same
update ID; a new receipt is retained. Clock skew can suppress packets.
Regressed cross-sequences or engine timestamps invalidate the symbol, except a
documented `u=1` restart with nonregressing `cts`, which is logged and accepted
as a new source sequence for that symbol. Reconnect resets all source clocks.

The process stops on storage cap, duration, local clock regression, interruption,
or inability to write evidence. Missing
or stale periods are logged and omitted, not interpolated; investigate repeated
misses and stop manually if source integrity or operator authorization is in
doubt. Confirm the terminal event and three files; abrupt process or disk failure
can leave incomplete evidence and must not be presented as a complete capture.
The adapter sends Bybit's public JSON heartbeat every 20 seconds; reconnects use
capped exponential backoff and stop after 20 consecutive connection failures.
A received message resets that counter. Explicit subscription rejection stops the
capture. Five minutes without an emitted valid book stops with `NO_VALID_BOOK`;
duration with zero valid frames is not a successful capture. Network/library failures are not proof
of source behavior.
An intermediary returning HTTP 200 rather than the WebSocket upgrade status 101
stops immediately with `WS_UPGRADE_NOT_AVAILABLE`; use a properly configured
host instead of reinterpreting a failed connection as an exchange observation.

This is prospective **source collection** only. Later replay, especially model
calls made after capture, is offline reconstruction and is not contemporaneous
prospective AI-decision evidence. A real-time decision experiment needs separately
approved contemporaneous inference, durable decision timing and later quotes.
Existing PAPER execution rules remain unchanged; this adapter makes no fills and
does not enable same-packet execution. Synthetic tests are not market evidence.

## Independent read-only verification

After a run ends, check the **original, private output directory**, without
copying its contents into the public repository:

```sh
python -m one_trading.ai_native.bybit_audit --capture-dir /private/existing-parent/bybit-pilot-unique
```

This separate auditor never imports the source collector or trading engine.
It independently decodes and hashes every raw receipt, reconciles framed
bid/ask/size and exchange timestamps against the specific source receipt,
reconstructs the valid book from **every preceding raw receipt** (including
malformed updates and reconnection invalidation), and verifies each frame's
explicit `raw_cutoff_seq` source-processing boundary. It checks
received-before-sample timing, samples strictly inside the configured duration
and terminal bounds, connection generations, sequence restarts,
missing-sample totals, terminal counts and the total byte quota. It also detects
price changes made to a frame and **rehashed** changes to its source that no
longer match the frame. A nonzero exit means evidence is unfit for promotion.

The result `PASS_SOURCE_CONSISTENCY_ONLY` is deliberately limited: it does not
authenticate Bybit's server, protect against all coordinated rewriting of the
entire evidence directory, prove that AI decisions occurred concurrently or
show an economic advantage. Preserve a private, externally recorded directory
hash/manifest and run ID if the evidence will be used in later economic tests.
