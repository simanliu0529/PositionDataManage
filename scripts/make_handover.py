"""Package the current source tree, including untracked code, without local data.

No git history, credentials, virtual environments, snapshots or runtime evidence
are copied. Run from the project root; --verify checks an extracted delivery.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
DIRECTORIES=('backend','frontend','cleaning','db','scripts','tests','sample','docs')
ROOT_FILES=('README.md','requirements.txt','requirements-tested.txt','.gitignore','.gitattributes',
            '.dockerignore','.env.example','Dockerfile','compose.yaml','compose.fullstack.yaml')


def digest(data): return hashlib.sha256(data).hexdigest()


def sources():
    files=[ROOT/name for name in ROOT_FILES]
    files += [p for name in DIRECTORIES for p in (ROOT/name).rglob('*')
              if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo')]
    for path in sorted(files):
        if path.is_symlink() or not path.is_file(): raise ValueError('Missing or linked source: '+str(path))
        name=path.relative_to(ROOT).as_posix()
        if (path.stat().st_size>5*1024*1024 or path.suffix.lower() in ('.sqlite','.log','.zip')
                or path.name=='.env' or path.name=='local-accounts.json'):
            raise ValueError('Unexpected large/runtime file: '+name)
        yield name,path.read_bytes()


def verify(root):
    root=Path(root).resolve()
    manifest=json.loads((root/'HANDOVER-MANIFEST.json').read_text(encoding='utf-8'))
    bad=[]
    for name,expected in manifest['files'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest(path.read_bytes())!=expected:
            bad.append(name)
    print(json.dumps({'files':len(manifest['files']),'mismatches':bad,'passed':not bad},indent=2))
    return 1 if bad else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data/handover-delivery/PositionDataManage-20261124.zip')
    parser.add_argument('--verify',type=Path)
    args=parser.parse_args()
    if args.verify: return verify(args.verify)
    subprocess.run([sys.executable,str(ROOT/'scripts/check_team_boundary.py')],check=True,cwd=ROOT)
    files=dict(sources())
    manifest={'version':'2026-11-24','kind':'working_tree_source_snapshot','git_history_included':False,
              'runtime_data_included':False,'files':{name:digest(data) for name,data in files.items()}}
    output=args.output.resolve();output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items(): archive.writestr('PositionDataManage/'+name,data)
        archive.writestr('PositionDataManage/HANDOVER-MANIFEST.json',json.dumps(manifest,indent=2))
    checksum=digest(output.read_bytes());output.with_suffix('.zip.sha256').write_text(checksum+'  '+output.name+'\n',encoding='ascii')
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for name,expected in manifest['files'].items(): assert digest(archive.read('PositionDataManage/'+name))==expected
    print(json.dumps({'zip':str(output),'files':len(files),'bytes':output.stat().st_size,'sha256':checksum},ensure_ascii=True,indent=2))
    return 0


if __name__=='__main__': raise SystemExit(main())
