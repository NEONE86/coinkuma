"""Application-body receipt/charge/retention meters, not packet-wire billing."""
from .store import raw_usage

def snapshot(ledger):
    receipts=ledger.all('receipt');http=ledger.all('http_result');ws=ledger.all('ws_result')
    ids={r['id'] for r in receipts}
    at=max([r['received_at'] for r in receipts+http+ws]+[r['at'] for r in ledger.all('http_intent')+ledger.all('ws_intent')]+[0])
    known=sum(r['bytes'] for r in receipts)+sum(r['bytes'] for r in http+ws if r.get('input_receipt_id') not in ids)
    linked={r.get('input_receipt_id') for r in http}
    http_bytes=sum(r['bytes'] for r in http)+sum(r['bytes'] for r in receipts if r['source'] in ('metadata','book','clock') and r['id'] not in linked)
    return dict(as_of=at,known_received_application_bytes=known,
        http_identity_body_bytes=http_bytes,ws_decoded_application_bytes=known-http_bytes,
        application_bytes_after_decoding=known,wire_compressed_received_bytes=None,
        decoding='HTTP requires identity encoding; WS is the library-delivered payload after any negotiated decompression. The cap counts application bytes, not compressed wire traffic.',
        rolling_24h_upper_bound_charge=raw_usage(ledger,at),
        physical_retained_bytes=sum(p.stat().st_size for p in (ledger.root/'raw').glob('*.bin')),
        http_attempts=len(ledger.all('http_intent')),ws_read_intents=len(ledger.all('ws_intent')),
        delivered_ws_messages=sum(r['bytes']>0 for r in ws),
        uncertain_results=sum(r.get('accounting_state')=='UNCERTAIN' for r in http+ws),
        evidence_pins=len(ledger.all('evidence_pin')),automatic_deletion=False,
        not_measured=['TLS/packet overhead','provider billing','undelivered WS internal buffers beyond conservative reservations'])
