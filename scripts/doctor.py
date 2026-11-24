"""Diagnose an existing local stack without printing credentials or changing data."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.diagnostics import local_checks,service_checks


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=Path(os.environ.get('CHECKIN_DATA',ROOT/'data')))
    parser.add_argument('--base',default='http://127.0.0.1:8002')
    parser.add_argument('--node-port',type=int,default=8200)
    parser.add_argument('--offline',action='store_true',help='Only inspect local configuration/files')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args();report=local_checks(args.data)
    if not args.offline:
        nodes=[os.environ.get(f'CHECKIN_NODE_{i}',f'http://127.0.0.1:{args.node_port+i}') for i in range(2)]
        report['checks']+=service_checks(report,args.base,nodes)
    report.pop('expected_counts',None);report['ok']=all(row['ok'] for row in report['checks'])
    if args.json:print(json.dumps(report,ensure_ascii=True,indent=2))
    else:
        for row in report['checks']: print(('OK   ' if row['ok'] else 'FAIL ')+row['check']+' - '+row['message'])
        print('Active batch: '+(report['active_batch_id'] or 'none'))
        print('Local checks passed.' if report['ok'] else 'Resolve the failed checks above; no data was modified.')
    return 0 if report['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
