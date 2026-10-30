"""Keep synthetic approval claims out of the operator's actual account state."""
from pathlib import Path
from unittest.mock import patch

def isolate_claims(case,temporary_root):
    replacement=patch('one_sentinel.approvals.claim_store',return_value=Path(temporary_root)/'synthetic-claims.sqlite3')
    replacement.start();case.addCleanup(replacement.stop)
