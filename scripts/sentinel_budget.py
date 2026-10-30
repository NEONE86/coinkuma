"""Offline data-contract arithmetic from actual exported receipts, not a forecast."""
import argparse
import collections
import json
from pathlib import Path


def calculate(root, seconds=300):
    receipts = [json.loads(row['body']) for row in
                (json.loads(line) for line in (root/'ledger.jsonl').read_text().splitlines())
                if row['kind'] == 'receipt']
    sizes = collections.Counter()
    counts = collections.Counter()
    for receipt in receipts:
        sizes[receipt['source']] += receipt['bytes']
        counts[receipt['source']] += 1
    assert counts['metadata'] and seconds > 0
    metadata_size = sizes['metadata']/counts['metadata']
    ws_rate = (sizes['mini']+sizes['bar'])/seconds
    metadata_daily = metadata_size*288
    ws_daily = ws_rate*86400
    total = metadata_daily+ws_daily
    cap = 256*1024**2
    return dict(classification='EXTRAPOLATION_OF_OLD_300_SECOND_REUSED_DATA_NOT_24H_MEASUREMENT',
                source_receipt_counts=dict(counts), source_bytes=dict(sizes),
                metadata_daily_bytes=metadata_daily, ws_daily_bytes=ws_daily,
                combined_daily_bytes=total, unchanged_cap_bytes=cap,
                estimated_cap_hours=24*cap/total,
                lower_coverage_example=dict(metadata_all_status_hourly=24,
                    ws_seconds_per_hour=120, bytes_per_day=metadata_size*24+ws_rate*120*24,
                    missed_minutes_per_hour=58, recommended=False),
                coverage_preserving_proposal=dict(approved=False, application_byte_cap=8*1024**3,
                    http_total_attempts=2500, metadata=288, clock=1440, books_max=400,
                    ordinary_http_sum=2128, remaining_recovery_room=372),
                unchanged_http_total_cap=1000, unchanged_reserved_attempts=312,
                unchanged_active_http_cap=688, pins_automatically_deleted=False,
                caveats=['Old raw excludes discarded WS ACK/open-bar payloads and protocol overhead.',
                         'New meter includes delivered WS ACK/open-bar payloads; unknown reads retain upper bounds.',
                         'WS delivered application bytes may already be decompressed; compressed wire bytes unmeasured.',
                         'Compression/dedup can reduce retention, never this received-byte budget.',
                         '8 GiB and 2500 requests require separate approval; this calculator changes no policy.'])


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('actual',type=Path);p.add_argument('--out',type=Path)
    a=p.parse_args();result=calculate(a.actual.resolve())
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if a.out:a.out.write_text(text,encoding='utf-8')
    print(text)
