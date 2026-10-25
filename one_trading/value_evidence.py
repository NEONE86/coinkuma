"""Read-only public evidence capture. Raw receipts belong outside the Git tree."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

HOSTS = {'eth.blockscout.com', 'api.github.com', 'raw.githubusercontent.com',
         'ipfs.io', 'cloudflare-ipfs.com', 'dweb.link', 'gateway.pinata.cloud',
         'etherscan.io', 'blog.ethereum.org', 'eips.ethereum.org',
         'governance.aave.com', 'gov.uniswap.org', 'blog.uniswap.org',
         'developers.uniswap.org', 'www.aave.com', 'api.routescan.io'}


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False)+'\n', encoding='utf-8', newline='\n')


class EvidenceStore:
    """Immutable entity bytes; first_seen is receipt, never source-claimed time.

    Serial writer, bounded retries, no credentials/order endpoints or POST. The
    cached object is verified before reuse. Each failed/successful HTTP attempt
    retains a receipt; this is a local audit trail, not an external timestamp.
    """
    def __init__(self, root, offline=False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root/'raw').mkdir(exist_ok=True)
        self.offline = offline
        path = self.root/'sources.json'
        self.sources = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        # The append-only receipts are authoritative if a process stopped before
        # writing the cache index. Recover actual earliest receipt per version.
        receipts=self.root/'requests.jsonl';first={}
        if receipts.exists():
            for line in receipts.read_text(encoding='utf-8').splitlines():
                r=json.loads(line)
                if r.get('status')==200 and 'path' in r and 'error' not in r:
                    key=(r['url'],r['sha256'])
                    first[key]=min(first.get(key,r['received_at']),r['received_at'])
                    self.sources[r['url']]=dict(r,first_seen_at=first[key])

    def get(self, url, refresh=False):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password:
            raise ValueError('source_not_allowlisted')
        if url in self.sources and not refresh:
            s = self.sources[url]
            b = (self.root/s['path']).read_bytes()
            if sha(b) != s['sha256']:
                raise ValueError('cached_source_tampered')
            return b, s
        if self.offline:
            raise ValueError('uncached_source_in_offline_mode')
        for attempt in range(3):
            receipt = dict(url=url, requested_at=utc(), attempt=attempt+1)
            try:
                req = Request(url, headers={'User-Agent':'ONE-value-accrual-research/1.0', 'Accept-Encoding':'identity'})
                with urlopen(req, timeout=30) as response:
                    final = urlparse(response.url)
                    if final.scheme != 'https' or final.hostname not in HOSTS:
                        raise ValueError('redirect_not_allowlisted')
                    b = response.read(12*1024*1024+1)
                    if len(b)>12*1024*1024:
                        raise ValueError('source_too_large')
                    receipt.update(status=response.status, headers={k:v for k,v in response.headers.items()
                        if k.lower() in {'date','etag','last-modified','content-type','content-encoding'}}, final_url=response.url)
                receipt.update(received_at=utc(), sha256=sha(b), size=len(b))
                path = 'raw/'+receipt['sha256']+'.bin'
                p = self.root/path
                if not p.exists():
                    p.write_bytes(b)
                elif p.read_bytes()!=b:
                    raise ValueError('content_address_collision')
                previous = self.sources.get(url)
                first = previous['first_seen_at'] if previous and previous['sha256']==sha(b) else receipt['received_at']
                receipt.update(path=path, first_seen_at=first)
                self.sources[url] = receipt
                save_json(self.root/'sources.json', self.sources)
                self._receipt(receipt)
                return b, receipt
            except Exception as e:
                receipt.update(received_at=utc(), error=type(e).__name__+': '+str(e))
                self._receipt(receipt)
                if attempt==2:
                    raise
                time.sleep(attempt+1)

    def _receipt(self, record):
        with (self.root/'requests.jsonl').open('a', encoding='utf-8', newline='\n') as f:
            f.write(canonical(record).decode()+'\n')

    def json(self, url):
        b, s = self.get(url)
        return json.loads(b), s


def timestamp(value):
    d = datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None:
        raise ValueError('timezone_required')
    return int(d.timestamp()*1000)


def iso(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).isoformat()


def word(data, offset):
    if offset<0 or offset+32>len(data):
        raise ValueError('abi_word_bounds')
    return int.from_bytes(data[offset:offset+32], 'big')


def abi_bytes(data, offset):
    n = word(data, offset)
    if offset+32+n>len(data):
        raise ValueError('abi_bytes_bounds')
    return data[offset+32:offset+32+n]


def uni_created(raw):
    """Decode id and description directly from GovernorAlpha/Bravo log data.

    ProposalCreated(uint256,address,address[],uint256[],string[],bytes[],
                    uint256,uint256,string); all parameters non-indexed.
    """
    data = bytes.fromhex(raw.removeprefix('0x'))
    offset = word(data, 8*32)
    if offset<9*32 or offset%32:
        raise ValueError('abi_description_offset')
    text = abi_bytes(data, offset)
    return word(data, 0), text


def base58(data):
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    n=int.from_bytes(data,'big'); result=''
    while n:
        n,r=divmod(n,58); result=alphabet[r]+result
    return '1'*(len(data)-len(data.lstrip(b'\0')))+result


def varint(n):
    out=bytearray()
    while n>127:
        out.append((n&127)|128); n >>= 7
    return bytes(out)+bytes([n])


def unixfs_cid(text):
    """CIDv0 for a single <=262144-byte UnixFS file, standard IPFS import.

    Reject larger/multipart files instead of assuming SHA256(file)==CID hash.
    UnixFS protobuf Data(Type=File, Data=bytes, filesize=n), wrapped PBNode.
    """
    if len(text)>262144:
        raise ValueError('multipart_ipfs_not_supported')
    unix=b'\x08\x02\x12'+varint(len(text))+text+b'\x18'+varint(len(text))
    node=b'\x0a'+varint(len(unix))+unix
    return base58(b'\x12\x20'+hashlib.sha256(node).digest())
