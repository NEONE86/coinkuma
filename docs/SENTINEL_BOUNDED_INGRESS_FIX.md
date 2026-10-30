# Public smoke failure and bounded-ingress repair

The first approved trial ran the exact clean `b71bc5b` source, using a new runtime
and no channel, paid model or orders. It failed before any market input could be
accepted. Twelve metadata responses each filled the 16 MiB + 1 byte read limit,
leaving twelve incomplete original byte prefixes (201,326,604 bytes total).
The previous collector did not record their HTTP status or an explicit truncation
flag. Do not label these complete JSON documents or successful source observations.

The metadata error escaped into the WS reconnect loop, so no broad miniTicker
input was committed. Each reconnect immediately retried metadata instead of
respecting the300s polling cadence. Failure bytes were not counted against the
256MiB budget. The operator stopped the exact trial process before the raw files
exceeded that cap. There is no normal completion record; the last recorded work
was about102s after start. This is NOT a completed five-minute trial, a forwarding
restart demonstration, or evidence of trading performance. The original source,
runtime and truncated responses are retained separately from this repair.

This repair does not raise byte/request/model budgets or alter trading rules:

- Request `exchangeInfo?showPermissionSets=false`, retaining all existing symbol
  status, spot eligibility and filter fields. The omitted permissionSets field
  is unused by this policy. Its documented switch is described in the
  [official API reference](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints).
  Real reduced-response size remains unverified until a new actual run.
- Run metadata in its own asynchronous task. Failed attempts respect cadence,
  including after restart, without blocking broad prices or forcing WS reconnects.
- Reserve read capacity before HTTP. Count failed/truncated/error body bytes and
  unresolved reservations alongside WS bytes. A receipt linked to the transport
  result is counted once. Crashed reads keep their original reservation.
- Retain HTTP status, byte count, truncation, specific bounded error code and
  receipt identity. HTTP error bodies are preserved within the same bound.
- Treat the normal end-of-window frame as an expiry boundary, not a stream error.
  Let in-flight bounded metadata finish recording instead of abandoning a thread.
- Obtain the actual processing time after acquiring the transaction for forward
  ingestion; a concurrent source cannot force backdated processing order.

The nine new tests reproduce resource exhaustion, HTTP error evidence, successful
byte accounting, WS observation despite metadata failure and restart cadence.
All transports are fake. They require only the standard library; real smoke still
uses the installed optional websockets package. The original model/order/channel
permissions remain unchanged. An old runtime is not migrated: source/contract
guards require a new approved runtime for this changed version.

```sh
python -B -m unittest discover -s tests -p "test_sentinel_ingress_limits.py" -v
python -B -m unittest discover -s tests -v
```

The full failed-actual evidence and exact source versions are supplied privately
in the public-smoke review ZIP, not committed to the repository. No repaired
source has yet been run against the market or deployed as a service.
