"""Read-only independent archive QA; report anomalies without repairing prices.

Run as ``python -m scripts.cashflow_data_audit --archive ... --output ...``.
Collected data and the detailed output stay outside the repository.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3

from one_trading.historical_data import coverage, verify_derived, write_json
from one_trading.replay import verify_archive
from one_trading.reference_data import compare


PR2_NORMALIZED_SHA256 = "b139383a4eacb5cf9150354576eb9cf2f44937c9a3e019bacfa88f3e37d2b98f"


def audit(archive: Path, output: Path, reference: Path | None = None) -> dict:
    evidence = verify_archive(archive)
    evidence.update(verify_derived(archive))
    db = sqlite3.connect(f"file:{archive.resolve()}?mode=ro", uri=True)
    try:
        cov = coverage(db)
        checksums = dict(db.execute(
            "SELECT s.url,r.raw FROM sources s JOIN snapshots r ON r.id=s.snapshot_id "
            "WHERE s.kind='publisher_checksum'"))
        verified = 0
        for url, raw in db.execute(
                "SELECT s.url,r.raw FROM sources s JOIN snapshots r ON r.id=s.snapshot_id "
                "WHERE s.kind='binance_monthly_zip'"):
            tokens = checksums[url + '.CHECKSUM'].decode().strip().split()
            if len(tokens) != 2 or tokens[0].lower() != hashlib.sha256(raw).hexdigest() \
                    or tokens[1].lstrip('*') != url.rsplit('/', 1)[-1]:
                raise ValueError('publisher_checksum_mismatch:' + url)
            verified += 1
        evidence['publisher_zip_checksums_verified'] = verified
        zero_volume = db.execute('SELECT count(*) FROM candles WHERE volume=0 OR quote_volume=0').fetchone()[0]
        invalid = db.execute('SELECT count(*) FROM candles WHERE min(o,h,l,c)<=0 '
                             'OR volume<0 OR quote_volume<0 OR l>min(o,c) OR h<max(o,c) OR l>h').fetchone()[0]
        if invalid:
            raise ValueError('invalid_normalized_rows')
        terminal = db.execute('SELECT max(open_ms) FROM candles').fetchone()[0]
        ended = sum(row['last_ms'] < terminal for row in cov['coverage'])
        years = [dict(year=year, bars=bars, markets=markets) for year,bars,markets in db.execute(
            "SELECT strftime('%Y',open_ms/1000,'unixepoch'),count(*),count(DISTINCT symbol) "
            "FROM candles GROUP BY 1 ORDER BY 1")]
        jumps = db.execute("""WITH prior AS (
            SELECT symbol,open_ms,c,lag(open_ms) OVER (PARTITION BY symbol ORDER BY open_ms) AS prior_ms,
                   lag(c) OVER (PARTITION BY symbol ORDER BY open_ms) AS prior_c,source_id
            FROM candles)
            SELECT symbol,open_ms,prior_c,c,c/prior_c,s.url FROM prior p JOIN sources s ON s.id=p.source_id
            WHERE open_ms-prior_ms=86400000 AND (c/prior_c>=5 OR c/prior_c<=0.2)
            ORDER BY symbol,open_ms""").fetchall()
        result = dict(
            audited_at_utc=datetime.now(timezone.utc).isoformat(),
            archive=evidence, collection=cov, year_coverage=years,
            zero_volume_rows=zero_volume, invalid_normalized_rows=invalid,
            histories_ending_before_dataset_end=ended,
            extreme_consecutive_close_ratio_rows=len(jumps),
            extreme_ratio_definition='Current close / previous consecutive daily close >=5 or <=0.2. Flags only; crashes, rallies and identity events require review, not automatic exclusion.',
            pr2_normalized_sha256=PR2_NORMALIZED_SHA256,
            matches_pr2_normalized_candles=(cov['normalized_candles_sha256']==PR2_NORMALIZED_SHA256),
            limits=[
                'Archive availability is not a certified historical listing register.',
                'Last observed candle does not prove delisting; missing markets require explicit handling.',
                'Checksums verify captured bytes, not historical publication time or complete token identity.',
                'Cross-venue major-pair checks do not validate every asset or USDT/USD parity.',
                'Zero-volume and shortened bars are retained, not price-repaired.',
            ],
        )
    finally:
        db.close()
    output.mkdir(parents=True, exist_ok=True)
    with (output/'extreme_close_ratios.csv').open('w',newline='') as handle:
        writer=csv.writer(handle,lineterminator='\n')
        writer.writerow(['symbol','open_ms','previous_close','close','ratio','source_url'])
        writer.writerows(jumps)
    if reference:
        compare(archive, reference, output/'crossvenue_comparison.json')
    write_json(output/'data_audit.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    result = audit(args.archive, args.output, args.reference)
    print({key: result[key] for key in ('archive','zero_volume_rows','invalid_normalized_rows',
                                      'histories_ending_before_dataset_end','matches_pr2_normalized_candles')})


if __name__ == '__main__':
    main()
