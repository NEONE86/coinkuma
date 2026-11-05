# Seven-day forward validation: preparation, not an activated run

The request authorizes public BTCUSDT/ETHUSDT/SOLUSDT L1 collection only after
storage and persistent execution are verified. This PR does not start collection,
install a production task, activate models, or claim a completed experiment.

## Frozen specification

PR26 `b6dcd93edc3e4dee812648b37591d6b183a95f02` and canonical protocol SHA256
`4fe0bdf2bb2208d54398fe411c1a89a9ba94a33c3c5d9d1df67ea91bdf67ab65` are bound by
the preparation module. It preserves the complete original protocol and selects
REVERSION/TREND/CASH,4h,RR2. In particular, REVERSION's conditions,300s grid,
0.11% allocation,10bp fees and5bp slippage per side,0.5s decision latency plus2s
execution delay, and1% visible L1 participation are unchanged. No tuning occurs.

The future cohort evaluates the whole newly collected interval, without the old
historical midpoint split. No capital or positions may reset at midnight. Expected
20 completed non-overlapping trades is a minimum sample requirement, not proof
of statistical power. Fewer20 after the fixed period means INSUFFICIENT, never
an automatic extension. Capacity100/500/1000 USDT is a separate counterfactual
size screen, never added to frozen-policy returns. The current helper checks
only L1 size at a supplied quote, not actual chronological entry/exit feasibility.

## Storage budget

The existing actual24h capture occupied1,415,878,705 bytes. Linear7d projection
is9,911,150,935 bytes, an estimate from one day, not a guarantee. Use the existing
collector's3GB daily limit:21GB raw campaign maximum,24GB maximum all campaign
artifacts, and preserve5GB free disk reserve. Require at least29GB free before
activation. Bytes already used by partial/restarted segments count against the
same day and campaign totals; never grant a fresh3GB allowance on a retry.

Actual preflight found the existing C: evidence volume insufficient, and another
local volume with sufficient space. Machine-specific measurements remain private.
An adequate disk alone does not authorize activation: the evaluator always reports
BLOCKED until a durable runner exists and is independently exercised.

## Observed continuity blocker

The existing ONE research tasks use Interactive logon and15-minute execution
limits. They were not modified. A separate one-minute, no-network S4U probe failed
at Windows task registration with Access denied. Therefore no successful session0
probe, logoff survival or production launch is claimed. Detached interactive
processes are not evidence of surviving Windows sign-out/reboot.

Resolution requires a Windows context permitted to register a dedicated bounded
noninteractive task, followed by an actual offline session0 witness and parent/
session-exit test. Administrative registration may require Windows UAC; no password
or credential belongs in a report, environment dump or repository. Existing tasks
must remain untouched. Do not start collecting just because the user previously
approved data access.

## Required runner work before activation (not implemented here)

1. Create a new private output root on the adequate volume, validate ACLs and the
   exact frozen execution source; claim a unique run ID once. Record start/deadline
   only when activating, with a hard604800s lifetime. Current timestamps are null.
2. Reuse the existing daily collector and independent Bybit auditor. Daily files
   remain separate. Closed segments receive SHA256 manifests and source audits.
   A killed partial file is preserved and audited INCOMPLETE; never invent a
   terminal event or append to it. Resume into a new segment with a recorded gap.
3. Build and test one durable supervisor with exclusive locking, atomic state,
   cumulative byte accounting, disk reserve checks and a fixed absolute deadline.
   Do not reset that deadline on reboot, retry or day rotation. Catch-up collection
   after the deadline is forbidden. Fatal source/clock problems stop safely.
4. Connect causal signals, pending orders and account state continuously across
   daily rotations. Log actual signal materialization time separately from source
   cutoff; never relabel post-hoc replay as decisions made in real time. Preserve
   rejection, gap, open-position and timeout evidence; no interpolation.
5. Verify stop/target order and strictly later original-receipt fills, cash/fee/
   slippage/equity/log-growth/drawdown independently. Apply the frozen open-exit
   handling across days. Test restart while holding/pending, duplicate startup,
   day rollover, disk full, truncated last line and final deadline.
6. At deadline stop network acquisition even if trades remain insufficient or
   unresolved. Final marks are not fills. Seal results and source hashes, audit
   capacity separately at the same receipt times, and preserve a private remote
   archive with verified download SHA256. Report REJECT/CANDIDATE/INSUFFICIENT;
   no7d report exists yet.

## Read-only preparation command

```powershell
python -B -X utf8 -m one_trading.ai_native.seven_day_preflight --volume D:/ --output NEW_PRIVATE_PREFLIGHT_DIRECTORY
python -B -X utf8 -m unittest discover -s tests -p test_seven_day_preflight.py -v
```

The CLI writes a proposed frozen contract and measured disk assessment. It cannot
launch a collector and does not implement the six runner requirements above.
Preparation tests and previous historical outcomes must not be reported as seven
days of forward trading. Await a verified durable execution environment.
