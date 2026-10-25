"""Collect the unchanged PR3 Binance archive specification with a portable lock."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from one_trading.historical_data import _collect, ms


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', required=True)
    p.add_argument('--cache', required=True)
    p.add_argument('--workers', type=int, default=24)
    a = p.parse_args()
    if not 1 <= a.workers <= 64:
        p.error('workers must be 1..64')
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    with open(a.db + '.collect.lock', 'a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if lock.tell() == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _collect(a.db, a.cache, ms('2018-01-01'), ms('2026-10-01'),
                 workers=a.workers, memory_build=True)


if __name__ == '__main__':
    main()
