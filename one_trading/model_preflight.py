"""Non-inference executable diagnosis; no auth files, account names or paths exported."""
import argparse,hashlib,json,os,re,shutil,subprocess
from pathlib import Path
from .autonomy.store import utc,atomic_bytes,canonical

def ensure_pinned_path(runtime):
    """Repair only this Python process. Windows Start-Process can reconstruct
    PATH, discarding the parent's process-only addition. The sealed model still
    uses shutil.which('codex'); neither its config nor credentials are changed.
    """
    pinfile=Path(runtime)/'model-executable.txt'
    if not pinfile.is_file():return False
    pinned=Path(pinfile.read_text(encoding='utf-8-sig').strip())
    if not pinned.is_file():return False
    parts=os.environ.get('PATH','').split(os.pathsep)
    if str(pinned.parent).casefold() not in [p.casefold() for p in parts]:
        os.environ['PATH']=str(pinned.parent)+os.pathsep+os.environ.get('PATH','')
    return bool(shutil.which('codex') and Path(shutil.which('codex')).resolve()==pinned.resolve())

def inspect(runtime):
    pinfile=Path(runtime)/'model-executable.txt'
    pinned=Path(pinfile.read_text(encoding='utf-8-sig').strip()) if pinfile.is_file() else None
    resolved=shutil.which('codex')
    report=dict(at=utc(),scope='EXECUTABLE_PREFLIGHT_NOT_INFERENCE',invocation_context=os.environ.get('ONE_PREFLIGHT_CONTEXT','INTERACTIVE'),
        pin_file_exists=pinfile.is_file(),pinned_exists=bool(pinned and pinned.is_file()),
        bare_resolved=bool(resolved),wrapper_parent_on_path=bool(pinned and str(pinned.parent).casefold() in [x.casefold() for x in os.environ.get('PATH','').split(os.pathsep)]),
        resolved_matches_pinned=bool(pinned and resolved and Path(resolved).resolve()==pinned.resolve()),
        model_attempts=0,subprocess_started=0,connected_valid=0,provider_usage_known=0)
    for label,path in [('pinned',pinned),('resolved',Path(resolved) if resolved else None)]:
        out=dict(exists=bool(path and path.is_file()),sha256=None,version=None,exit_code=None,version_process_started=False,error=None)
        if out['exists']:
            try:
                out['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
                run=subprocess.run([str(path),'--version'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                out['version_process_started']=True;out['exit_code']=run.returncode
                match=re.fullmatch(r'codex(?:-cli)? [0-9A-Za-z.+_-]+',run.stdout.strip())
                out['version']=match.group(0) if match else None
            except Exception as exc:out['error']=type(exc).__name__
        report[label]=out
    report['status']='PREFLIGHT_PASS' if report['resolved_matches_pinned'] and report['resolved']['exit_code']==0 else 'PREFLIGHT_BLOCKED'
    return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--repair-process-path',action='store_true');a=p.parse_args()
    before=inspect(a.runtime)
    if a.repair_process_path:
        ensure_pinned_path(a.runtime);r=inspect(a.runtime);r['before_process_path_repair']=before
    else:r=before
    atomic_bytes(a.output,canonical(r));print(json.dumps(r))

if __name__=='__main__':main()
