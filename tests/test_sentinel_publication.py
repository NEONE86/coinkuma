"""Public/private boundary controls; no network or actual evidence reads."""
import copy,json,unittest
from pathlib import Path
from scripts.check_observer_publication import check_files,validate_reference

class PublicationBoundary(unittest.TestCase):
    def setUp(self):
        self.row=json.loads((Path(__file__).resolve().parents[1]/'docs/observer/runs/ONE_OBSERVER_20261030_114233_2D67741.json').read_text())
    def pending(self):
        return dict(self.row,storage_status='AWAITING_PRIVATE_STORAGE',private_url=None,private_asset_id=None,remote_digest_verified=False)
    def stored(self):
        return dict(self.row,storage_status='STORED_PRIVATE',private_url='https://github.com/NEONE86/coinkuma-observer-evidence/releases/tag/synthetic-test',private_asset_id=123,remote_digest_verified=True)
    def test_pending_is_explicit_and_not_completed(self):
        validate_reference(self.pending())
        with self.assertRaisesRegex(ValueError,'PRIVATE_EVIDENCE_NOT_STORED'):validate_reference(self.pending(),True)
    def test_stored_reference_has_bound_private_destination(self):validate_reference(self.stored(),True)
    def test_public_repo_and_credential_urls_are_rejected(self):
        for url in ('https://github.com/NEONE86/coinkuma/releases/tag/x','https://user:secret@github.com/NEONE86/coinkuma-observer-evidence/releases/tag/x','https://github.com/NEONE86/coinkuma-observer-evidence/releases/tag/x?token=secret'):
            with self.subTest(url=url),self.assertRaisesRegex(ValueError,'PRIVATE_LINK'):validate_reference(dict(self.stored(),private_url=url))
    def test_raw_field_cannot_hide_in_reference(self):
        with self.assertRaisesRegex(ValueError,'REFERENCE_FIELDS'):validate_reference(dict(self.row,raw_payload={}))
    def test_boolean_asset_ids_and_false_digest_claims_rejected(self):
        for change in (dict(private_asset_id=True),dict(remote_digest_verified=False)):
            with self.assertRaisesRegex(ValueError,'UPLOAD_NOT_VERIFIED'):validate_reference(dict(self.stored(),**change))
    def test_pending_cannot_claim_uploaded_asset(self):
        with self.assertRaisesRegex(ValueError,'FALSE_UPLOAD_CLAIM'):validate_reference(dict(self.pending(),private_asset_id=5))
    def test_payload_paths_rejected_without_reading_contents(self):
        for name in ('raw/anything.txt','runtime/record.txt','tests/prices.bin','tests/data.sqlite3','evidence.zip','ledger.jsonl','approval.final.json'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'PRIVATE_PAYLOAD_IN_PUBLIC_GIT'):check_files([(name,b'')])
    def test_real_receipts_cannot_be_disguised_as_json_fixture(self):
        with self.assertRaisesRegex(ValueError,'PRIVATE_PAYLOAD_IN_PUBLIC_GIT'):check_files([('tests/fixtures/sample.json',b'{"classification": "REAL_FORWARD"}')])
        check_files([('tests/fixtures/sample.json',b'{"classification": "SYNTHETIC"}')])
    def test_reference_directory_is_not_a_raw_data_exemption(self):
        with self.assertRaisesRegex(ValueError,'REFERENCE_FIELDS'):
            check_files([('docs/observer/runs/raw.json',b'{"classification":"REAL_FORWARD","raw":123}')])
        with self.assertRaisesRegex(ValueError,'PRIVATE_PAYLOAD_IN_PUBLIC_GIT'):
            check_files([('docs/observer/runs/nested/raw.json',b'{"classification":"REAL_FORWARD"}')])
    def test_escaped_ledger_body_is_rejected(self):
        raw=json.dumps(dict(body=json.dumps(dict(classification='REAL_FORWARD')))).encode()
        with self.assertRaisesRegex(ValueError,'PRIVATE_PAYLOAD_IN_PUBLIC_GIT'):
            check_files([('tests/fixtures/disguised.jsonl',raw)])

if __name__=='__main__':unittest.main()
