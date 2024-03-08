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

import tomllib

peer_data = {}

config_file = getenv("UKMS_CONFIG_FILE", "/etc/ukms.toml")

config = tomllib.loads(Path(config_file).read_text())


key_folder = Path(config["key_folder"])
key_folder.mkdir(parents=True, exist_ok=True)


def fetch_json(url):
    print(f"Fetching data from {url}")

    with urllib.request.urlopen(url) as response:
        if response.status == 200:
            return json.loads(response.read().decode())
        else:
            print(f"Failed to fetch data, status code: {response.status}")
            return None


def fetch_qkd_key(peer):
    url = f"{peer['etsi_url']}/api/v1/keys/{peer['slave_SAE_ID']}/enc_keys"
    data = fetch_json(url)
    peer["key_ID"] = data["keys"][0]["key_ID"]
    peer["key"] = data["keys"][0]["key"]


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
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

    def handle_rotate(self):
        peer = peer_data.get(self.path.split("/")[-2])
        if not peer:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return

        if self.path.endswith("/new"):
            peer["rotate_in_seconds"] = 0
            peer["remote_key"] = None

        fetch_qkd_key(peer)

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        response = json.dumps({"key_ID": peer["key_ID"]})
        self.wfile.write(response.encode())
        peer["key_update"] = True

    def do_GET(self):
        if self.path.startswith("/api/v1/peer/") and self.path.endswith(
            ("/rotate", "/new")
        ):
            self.handle_rotate()
        elif self.path.startswith("/api/v1/keys/") and config["enable_fake_qkd_api"]:
            self.handle_keys()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")


def run_server():
    httpd = ThreadingHTTPServer(
        (config["ukms_bind"], config["ukms_port"]), SimpleHTTPRequestHandler
    )
    print(f"Serving at http://{config['ukms_bind']}:{config['ukms_port']}")
    httpd.serve_forever()


async def request_key_rotation(peer):
    while True:
        url = f"{peer['ukms_url']}/api/v1/peer/{peer['source_KME_ID']}/rotate"
        data = fetch_json(url)
        print(data)
        if data.get("status") == "ok":
            print("Peer online")
            break

        print("Peer offline")
        await asyncio.sleep(2)


async def update_psk(peer):
    while True:
        if peer["key_update"] and peer["key"] and peer["remote_key"]:
            psk = ""
            for key in sorted([peer["remote_key"], peer["key"]]):
                psk += key

            psk = sha3_256(psk.encode()).digest()
            (key_folder / f"wg2-{peer['alias']}.key").write_bytes(
                base64.b64encode(psk)
            )

            print(f"new PSK: {peer['source_KME_ID']} <-> {peer['target_KME_ID']}")
            peer["key_update"] = False
        await asyncio.sleep(1)


async def fetch_peer_data(peer):
    while True:
        if peer["rotate_in_seconds"] > 0:
            peer["rotate_in_seconds"] -= 1
            await asyncio.sleep(1)
            continue

        try:
            if peer["rotate_in_seconds"] == -1:
                url = f"{peer['ukms_url']}/api/v1/peer/{peer['source_KME_ID']}/new"
            else:
                url = f"{peer['ukms_url']}/api/v1/peer/{peer['source_KME_ID']}/rotate"
            data = fetch_json(url)
            key_ID = data.get("key_ID")

            url = f"{peer['etsi_url']}/api/v1/keys/{peer['slave_SAE_ID']}/dec_keys?key_ID={key_ID}"
            data = fetch_json(url)

            peer["remote_key"] = data.get("keys")[0].get("key")
            peer["key_update"] = True
            peer["rotate_in_seconds"] = config["key_rotation_seconds"]

        except Exception as e:
            print(f"Error fetching data: {e}")
            peer["rotate_in_seconds"] = 2


async def main():
    # Run the server in a separate thread because it's not async
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Run the fetch task

    runs = []
    for target_KME_ID in config["peers"]:
        print(f"Peer: {target_KME_ID}")

        peer_data[target_KME_ID] = {
            **config["peers"][target_KME_ID],
            "target_KME_ID": target_KME_ID,
            "remote_key": None,
            "key": None,
            "key_update": False,
            "rotate_in_seconds": -1,
        }

        runs.append(update_psk(peer_data[target_KME_ID]))
        runs.append(fetch_peer_data(peer_data[target_KME_ID]))

    asyncio.gather(*runs)

    while True:
        # print(json.dumps(peer_data, indent=4))
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
