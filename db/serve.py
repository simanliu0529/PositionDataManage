"""Start the two existing node processes and the web backend together."""

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from urllib.request import urlopen

from db.load import FILENAMES


def serve(directory, *, port=8000, node_ports=(8100, 8101)):
    directory = Path(directory).resolve()
    if (directory / ".import.lock").exists():
        raise ValueError("Import is still running")
    for name in FILENAMES:
        if not (directory / name).is_file():
            raise ValueError(f"Database missing: {directory / name}; run db.load first")
    ports = (port, *node_ports)
    if len(set(ports)) != 3 or any(not 1 <= p <= 65535 for p in ports):
        raise ValueError("Choose three different ports between 1 and 65535")
    for chosen in ports:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", chosen))
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for index, chosen in enumerate(node_ports):
        env[f"CHECKIN_NODE_{index}"] = f"http://127.0.0.1:{chosen}"
    commands = [
        [sys.executable, "-m", "db.node", str(directory / name), str(chosen)]
        for name, chosen in zip(FILENAMES, node_ports)
    ]
    commands.append([sys.executable, "-m", "backend.app", "--port", str(port)])
    children = []
    try:
        for command in commands:
            children.append(subprocess.Popen(command, cwd=root, env=env))
        url = f"http://127.0.0.1:{port}"
        deadline = monotonic() + 20
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A service exited during startup")
            try:
                with urlopen(url + "/api/nodes", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                if monotonic() >= deadline:
                    raise RuntimeError("Services did not become ready within 20 seconds") from None
                sleep(0.2)
        print(f"Ready: {url}  (Ctrl+C stops all three services)", flush=True)
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A service exited; stopping the remaining services")
            sleep(0.5)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("data/full"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--node-0-port", type=int, default=8100)
    parser.add_argument("--node-1-port", type=int, default=8101)
    args = parser.parse_args()
    try:
        serve(args.directory, port=args.port, node_ports=(args.node_0_port, args.node_1_port))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Startup failed: {error}\n")
