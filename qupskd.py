#!/usr/bin/env python3

import asyncio
import base64
import json
import threading
import urllib.request
import uuid
from hashlib import sha3_256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import getenv
from pathlib import Path
from subprocess import run

import tomllib

IDENTITY_STRING = "quPSKd Version 1"
INITIATOR_WAIT_SECONDS = 120
RESPONDER_WAIT_SECONDS = 130

config_file = getenv("QUPSKD_CONFIG_FILE", "/etc/qupskd.toml")

config = tomllib.loads(Path(config_file).read_text())


if "key_folder" in config:
    key_folder = Path(config["key_folder"])
    key_folder.mkdir(parents=True, exist_ok=True)


def sha3_base64(input):
    return base64.b64encode(sha3_256("".join(sorted(input)).encode()).digest())


state = {
    "key": None,
    "key_ID": None,
    "last_rotate": -1,
    "initiator": False,
    "psk": sha3_base64([config.get("psk", ""), IDENTITY_STRING]),
}


def fetch_json(url):
    print(f"Fetching data from {url}")

    with urllib.request.urlopen(url) as response:
        if response.status == 200:
            return json.loads(response.read().decode())
        else:
            print(f"Failed to fetch data, status code: {response.status}")
            return None


def fetch_qkd(url):
    data = fetch_json(url)
    state["key_ID"] = data["keys"][0]["key_ID"]
    state["key"] = data["keys"][0]["key"]


def fetch_qkd_key_id():
    fetch_qkd(
        f"{config['etsi_url']}/api/v1/keys/{config['remote_SAE_ID']}/dec_keys?key_ID={state['key_ID']}"
    )


def fetch_qkd_key():
    fetch_qkd(
        f"{config['etsi_url']}/api/v1/keys/{config['remote_SAE_ID']}/enc_keys?number=1"
    )


def psk_update():
    parts = [
        state["key"],
        state["key_ID"],
        state["psk"].decode(),
    ]

    state["psk"] = base64.b64encode(sha3_256("".join(sorted(parts)).encode()).digest())
    if "wireguard_public_key" in config:
        run(
            args=[
                "wg-set-psk",
                f"wg0_{config['alias']}",
                config["wireguard_public_key"],
            ],
            input=state["psk"],
        )
    else:
        (key_folder / f"{config['alias']}.key").write_bytes(state["psk"])

    print(
        f"new PSK: {config['source_KME_ID']} <-> {config['target_KME_ID']}"
    )


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_version = "quPSKd/1.0"

    def handle_keys(self):
        if "dec_keys" in self.path and "key_ID=" not in self.path:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"key_ID parameter is required")
            return

        if "dec_keys" in self.path:
            key_ID = self.path.split("=")[-1]
        else:
            key_ID = str(uuid.uuid4())
        key = base64.b64encode(sha3_256(key_ID.encode()).digest()).decode()

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        response = json.dumps(
            {
                "keys": [
                    {
                        "key": key,
                        "key_ID": key_ID,
                    }
                ]
            }
        )
        self.wfile.write(response.encode())

    def check_peer(self) -> dict:
        if not self.path.split("/")[-2] == config["target_KME_ID"]:
            self.handle_404()

    def handle_rotate(self, new=False):
        self.check_peer()

        if new:
            state["psk"] = sha3_base64([config.get("psk", ""), IDENTITY_STRING])
            print("Initiating key rotation")

        fetch_qkd_key()

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        response = json.dumps(
            {
                "status": "ok",
                "key_ID": state["key_ID"],
            }
        )
        self.wfile.write(response.encode())
        state["initiator"] = False

    def handle_ack(self):
        self.check_peer()

        psk_update()
        state["last_rotate"] = 0

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        response = json.dumps(
            {
                "status": "ok",
            }
        )
        self.wfile.write(response.encode())

    def handle_404(self):
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"404 Not Found")

    def do_GET(self):
        if self.path.startswith("/api/v1/peer/"):
            if self.path.endswith("/new"):
                self.handle_rotate(new=True)
            elif self.path.endswith("/rotate"):
                self.handle_rotate()
            elif self.path.endswith("/ack"):
                self.handle_ack()
            else:
                self.handle_404()

        elif self.path.startswith("/api/v1/keys/") and config["enable_fake_qkd_api"]:
            self.handle_keys()
        else:
            self.handle_404()


def run_server():
    httpd = ThreadingHTTPServer(
        (config["qupskd_bind"], config["qupskd_port"]), SimpleHTTPRequestHandler
    )
    print(f"Serving at http://{config['qupskd_bind']}:{config['qupskd_port']}")
    httpd.serve_forever()


async def fetch_peer_data():
    while True:
        try:
            if state["initiator"]:
                if -1 < state["last_rotate"] < INITIATOR_WAIT_SECONDS:
                    state["last_rotate"] += 1
                    await asyncio.sleep(1)
                    continue
            else:
                if -1 < state["last_rotate"] < RESPONDER_WAIT_SECONDS:
                    state["last_rotate"] += 1
                    await asyncio.sleep(1)
                    continue
                else:
                    state["initiator"] = True

            if not state["key_ID"]:
                url = f"{config['remote_qupskd_url']}/api/v1/peer/{config['source_KME_ID']}/new"
            else:
                url = f"{config['remote_qupskd_url']}/api/v1/peer/{config['source_KME_ID']}/rotate"

            data = fetch_json(url)
            state["key_ID"] = data.get("key_ID")

            fetch_qkd_key_id()

            state["last_rotate"] = 0

            fetch_json(
                f"{config['remote_qupskd_url']}/api/v1/peer/{config['source_KME_ID']}/ack"
            )

            psk_update()
        except Exception as e:
            print(e)
            await asyncio.sleep(1)


async def main():
    # Run the server in a separate thread because it's not async
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    await fetch_peer_data()


if __name__ == "__main__":
    asyncio.run(main())
