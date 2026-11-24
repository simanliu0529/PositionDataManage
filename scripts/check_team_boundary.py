"""Read-only source/config boundary check against an explicitly chosen team ref."""
import argparse
import json
import hashlib
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
BASELINE='753ec0f06be8ba083db0f219d2838a3cfc4c9531'

def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT)

def check(baseline):
    if not (ROOT/'.git').exists():
        manifest=json.loads((ROOT/'docs'/'team-boundary-manifest.json').read_text(encoding='utf-8'))
        if manifest['baseline']!=baseline:
            raise ValueError('Snapshot baseline differs; use the documented --baseline value')
        names=manifest['files']
        differences=[name for name,digest in names.items()
                     if not (ROOT/name).is_file() or hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest]
        extras=[p.relative_to(ROOT).as_posix() for d in ('cleaning','db') for p in (ROOT/d).rglob('*')
                if p.is_file() and '__pycache__' not in p.parts and p.relative_to(ROOT).as_posix() not in names]
        return {'baseline':baseline,'method':'snapshot_sha256','checked_files':len(names),
                'modified_or_missing':differences,'unexpected_source_files':extras,'passed':not differences and not extras}
    names=git('ls-tree','-r','--name-only',baseline,'--','cleaning','db').decode().splitlines()+['compose.yaml']
    differences=[]
    for name in names:
        path=ROOT/name
        if not path.is_file() or path.read_bytes()!=git('show',baseline+':'+name):
            differences.append(name)
    extras=[str(path.relative_to(ROOT)).replace('\\','/')
            for directory in ('cleaning','db') for path in (ROOT/directory).rglob('*')
            if path.is_file() and '__pycache__' not in path.parts
            and str(path.relative_to(ROOT)).replace('\\','/') not in names]
    return {'baseline':baseline,'checked_files':len(names),'modified_or_missing':differences,
            'unexpected_source_files':extras,'passed':not differences and not extras}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',default=BASELINE)
    args=parser.parse_args()
    result=check(args.baseline)
    print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__': raise SystemExit(main())
