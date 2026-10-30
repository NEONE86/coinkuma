"""Unittest discovery with external sockets denied; Windows self-pipe permitted.

Only the stdlib socketpair call may open its own loopback pair. This is asyncio
process-local plumbing, not an exemption for arbitrary loopback/API requests.
"""
import argparse,ipaddress,json,socket,sys,threading,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    p=argparse.ArgumentParser();p.add_argument('--pattern',default='test_sentinel*.py');p.add_argument('--log',required=True,type=Path);a=p.parse_args()
    connect=socket.socket.connect;pair=socket.socketpair;local=threading.local();counts={'self_pipe_connections':0,'external_calls':0,'denied_attempts':0}
    def guarded(sock,address):
        if getattr(local,'in_pair',False) and isinstance(address,tuple) and ipaddress.ip_address(address[0]).is_loopback:
            counts['self_pipe_connections']+=1;return connect(sock,address)
        counts['denied_attempts']+=1;raise AssertionError('OFFLINE_TEST_NETWORK_BLOCKED')
    def socketpair(*args,**kw):
        local.in_pair=True
        try:return pair(*args,**kw)
        finally:local.in_pair=False
    def blocked(*args,**kw):counts['denied_attempts']+=1;raise AssertionError('OFFLINE_TEST_NETWORK_BLOCKED')
    socket.socket.connect=guarded;socket.socket.connect_ex=blocked;socket.socketpair=socketpair;socket.create_connection=blocked
    suite=unittest.defaultTestLoader.discover('tests',pattern=a.pattern)
    with a.log.open('w',encoding='utf-8') as log:result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    print(json.dumps(dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),network=counts)))
    sys.exit(not result.wasSuccessful())
if __name__=='__main__':main()
