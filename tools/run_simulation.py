"""Start a loopback-only development UI. Machine addresses are never contacted."""
import argparse
import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "haas_monitor"))
from app.main import create_app
import uvicorn

parser = argparse.ArgumentParser()
parser.add_argument("--data", type=Path, required=True)
parser.add_argument("--broker-port", type=int, default=18883)
args = parser.parse_args()
options = {"mode": "simulation", "mqtt_host": "127.0.0.1", "mqtt_port": args.broker_port}
uvicorn.run(create_app(args.data, options, development=True), host="127.0.0.1", port=8099, proxy_headers=False)
