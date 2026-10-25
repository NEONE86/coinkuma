"""Public-source availability/timestamp probe; never computes strategy returns."""
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from one_trading.historical_data import Cache,INDEX,BULK,NS,write_json


def listing(cache,prefix):
    marker=None;entries=[];evidence=[]
    while True:
        params={'prefix':prefix,'max-keys':1000}
        if marker:params['marker']=marker
        raw,m=cache.get(INDEX+'?'+urllib.parse.urlencode(params));evidence.append(m)
        root=ET.fromstring(raw)
        for e in root.findall('s:Contents',NS):
            entries.append(dict(key=e.findtext('s:Key',namespaces=NS),
                last_modified=e.findtext('s:LastModified',namespaces=NS),size=int(e.findtext('s:Size',namespaces=NS))))
        if root.findtext('s:IsTruncated',namespaces=NS)!='true':break
        marker=root.findtext('s:NextMarker',namespaces=NS) or entries[-1]['key']
    return entries,evidence


def probe(output):
    out=Path(output);out.mkdir(parents=True,exist_ok=False);cache=Cache(out/'http');result={}
    for symbol in ('BTCUSDT','ETHUSDT'):
        result[symbol]={}
        for stream in ('klines','markPriceKlines','indexPriceKlines','fundingRate','metrics'):
            prefix=f'data/futures/um/{"daily" if stream=="metrics" else "monthly"}/{stream}/{symbol}/'
            if stream not in {'fundingRate','metrics'}:prefix+='1h/'
            entries,evidence=listing(cache,prefix);zips=[e for e in entries if e['key'].endswith('.zip')]
            samples=[]
            for e in ([zips[0],zips[len(zips)//2],zips[-1]] if zips else []):
                raw,m=cache.get(BULK+e['key']);check,cm=cache.get(BULK+e['key']+'.CHECKSUM')
                if check.decode().split()[0]!=hashlib.sha256(raw).hexdigest():raise ValueError('checksum')
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    rows=list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode('utf-8-sig'))))
                has_header=not rows[0][0].isdigit()
                samples.append(dict(**e,sha256=m['sha256'],fetched_ms=m['fetched_ms'],
                    header=rows[0] if has_header else [],header_present=has_header,
                    first_timestamp=rows[1 if has_header else 0][0],last_timestamp=rows[-1][0],rows=len(rows)-int(has_header)))
            result[symbol][stream]=dict(zip_count=len(zips),first=zips[0] if zips else None,
                last=zips[-1] if zips else None,samples=samples,index_evidence=evidence)
            write_json(out/'probe.json',result)
            print(json.dumps(dict(symbol=symbol,stream=stream,zip_count=len(zips),samples=samples)),flush=True)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True)
    probe(p.parse_args().output)
