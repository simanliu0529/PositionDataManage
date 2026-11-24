"""Start the complete local stack and stop only its own child processes on Ctrl+C."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.diagnostics import health,local_checks

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8000);parser.add_argument('--node-port',type=int,default=8100);args=parser.parse_args()
    if args.node_port==args.port or args.node_port+1==args.port: parser.error('Web and node ports must differ')
    env=os.environ.copy();data=Path(env.get('CHECKIN_DATA',ROOT/'data')).resolve();data.mkdir(parents=True,exist_ok=True)
    env['CHECKIN_DATA']=str(data)
    for i in range(2):
        env[f'CHECKIN_SHARD_{i}']=str(Path(env.get(f'CHECKIN_SHARD_{i}',data/'shards'/f'node-{i}')).resolve());Path(env[f'CHECKIN_SHARD_{i}']).mkdir(parents=True,exist_ok=True)
        env[f'CHECKIN_NODE_{i}']=f'http://127.0.0.1:{args.node_port+i}'
    checks=local_checks(data,[args.port,args.node_port,args.node_port+1],[env[f'CHECKIN_SHARD_{i}'] for i in range(2)])
    if not checks['ok']:
        for item in checks['checks']:
            if not item['ok']:print(f"Startup check failed: {item['check']} - {item['message']}",file=sys.stderr)
        raise SystemExit(1)
    commands=[[sys.executable,'-m','backend.batch_node',env[f'CHECKIN_SHARD_{i}'],str(args.node_port+i)] for i in range(2)]
    commands += [[sys.executable,'-m','backend.app','--port',str(args.port),'--no-worker']]
    children=[]
    try:
        for command in commands:
            children.append(subprocess.Popen(command,cwd=ROOT,env=env,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0))
        pending=[(env[f'CHECKIN_NODE_{i}'],'checkin-node') for i in range(2)]+[(f'http://127.0.0.1:{args.port}','checkin-web')]
        deadline=time.monotonic()+20
        while pending:
            if any(child.poll() is not None for child in children):raise SystemExit('A service exited before readiness; check ports and logs.')
            for endpoint in list(pending):
                try:health(*endpoint,timeout=.5);pending.remove(endpoint)
                except (OSError,ValueError):pass
            if time.monotonic()>deadline:raise SystemExit('Services did not become ready within 20 seconds; check their logs. No import worker was started.')
            if pending:time.sleep(.1)
        children.append(subprocess.Popen([sys.executable,'-m','backend.worker'],cwd=ROOT,env=env,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0))
        print(f'Complete stack: http://127.0.0.1:{args.port} (Ctrl+C stops this stack)',flush=True)
        while all(child.poll() is None for child in children): time.sleep(.5)
        failed=[child.returncode for child in children if child.poll() is not None]
        if failed: raise SystemExit('A service exited; check ports, worker ownership and logs.')
    except KeyboardInterrupt: pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                else:child.terminate()
        for child in children:
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: child.kill();child.wait()

if __name__=='__main__': main()
