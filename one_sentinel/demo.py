"""Generate explicitly synthetic quiet-price access restoration and loss scenario."""
import argparse,json
from pathlib import Path

T=1800000000
SYMBOL='NEWUSDT'
def metadata(status='BREAK'):
    return dict(symbols=[dict(symbol=s,quoteAsset='USDT',status=status if s==SYMBOL else 'TRADING',isSpotTradingAllowed=True,filters=[dict(filterType='LOT_SIZE',stepSize='0.001',minQty='0.001'),dict(filterType='MIN_NOTIONAL',minNotional='10')]) for s in ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT',SYMBOL]])
def mini(at,price='100',q='100000'):
    return [dict(e='24hrMiniTicker',s=SYMBOL,E=int(at*1000),c=price,v='1000',q=q)]
def bar(at):
    end=int(at//60*60*1000)-1
    return dict(s=SYMBOL,E=int(at*1000),k=dict(i='1m',x=True,t=end-59999,T=end,q='20000'))
def book(price='100',depth='10000'):
    return dict(symbol=SYMBOL,bids=[[price,depth]],asks=[[price,depth]])
def sequence():
    return [('metadata',T,metadata()),('mini',T+1,mini(T+1)),('metadata',T+2,metadata('TRADING')),('bar',T+3,bar(T+3)),('book',T+5,book()),('mini',T+7,mini(T+7)),('mini',T+10,mini(T+10,'96')),('book',T+12,book('96')),('metadata',T+15,metadata('BREAK')),('book',T+17,book('95'))]
def write(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);lines=[]
    for i,(s,t,d) in enumerate(sequence()):
        name=f'{i:02}-{s}.json';(root/name).write_text(json.dumps(d),encoding='utf-8')
        lines.append(json.dumps(dict(source=s,received_at=t,processed_at=t,classification='SYNTHETIC',raw_file=name)))
    (root/'inputs.jsonl').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # Restart boundary after entry fill; second half resumes the same database.
    for name,values in [('part1',lines[:5]),('part2',lines[5:])]: (root/f'{name}.jsonl').write_text('\n'.join(values)+'\n',encoding='utf-8')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);write(p.parse_args().out)
