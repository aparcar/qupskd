#!/usr/bin/env python3

import asyncio
import base64
import json
import logging
import socket
import threading
import urllib.request
import uuid
from hashlib import sha3_256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import getenv
from pathlib import Path
from subprocess import run

import tomllib

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.DEBUG,
    datefmt="%Y-%m-%d %H:%M:%S",
)

IDENTITY_STRING = "quPSKd Version 1"
INITIATOR_WAIT_SECONDS = 120
RESPONDER_WAIT_SECONDS = 130
MAX_WAIT_SECONDS = 180

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
    "last_rotate": -2,
    "initiator": False,
    "psk": sha3_base64([config.get("psk", ""), IDENTITY_STRING]),
}


def fetch_json(url):
    logger.info(f"Fetching data from {url}")

    with urllib.request.urlopen(url) as response:
        if response.status == 200:
            return json.loads(response.read().decode())
        else:
            logger.error(f"Failed to fetch data, status code: {response.status}")
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

    state["psk"] = sha3_base64(parts)
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

    logger.info(f"new PSK: {config['source_KME_ID']} <-> {config['target_KME_ID']}")

    state["last_rotate"] = 0


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_version = "quPSKd/1.0"

    def log_message(self, format: str, *args) -> None:
        logger.debug(format % args)

    def log_error(self, format: str, *args) -> None:
        logger.warning(format % args)

    def handle_rotate(self, new=False):
        if new:
            state["psk"] = sha3_base64([config.get("psk", ""), IDENTITY_STRING])
            logger.info("Initiating key rotation")

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
        psk_update()

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
        if self.path == "/new":
            self.handle_rotate(new=True)
        elif self.path == "/rotate":
            self.handle_rotate()
        elif self.path == "/ack":
            self.handle_ack()
        else:
            self.handle_404()


def run_server():
    httpd = ThreadingHTTPServer(
        (config["qupskd_bind"], config["qupskd_port"]), SimpleHTTPRequestHandler
    )
    logger.info(f"Serving at http://{config['qupskd_bind']}:{config['qupskd_port']}")
    httpd.serve_forever()


async def fetch_peer_data():
    while True:
        state["last_rotate"] += 1
        try:
            if state["last_rotate"] > MAX_WAIT_SECONDS:
                logger.warning("Key rotation failed. Setting random PSK")
                state["psk"] = sha3_base64([uuid.uuid4().hex])
                psk_update()
                continue

            if state["initiator"]:
                if -1 < state["last_rotate"] < INITIATOR_WAIT_SECONDS:
                    await asyncio.sleep(1)
                    continue
            else:
                if -1 < state["last_rotate"] < RESPONDER_WAIT_SECONDS:
                    await asyncio.sleep(1)
                    continue
                else:
                    state["initiator"] = True

            if not state["key_ID"]:
                url = f"{config['remote_qupskd_url']}/new"
            else:
                url = f"{config['remote_qupskd_url']}/rotate"

            data = fetch_json(url)
            state["key_ID"] = data.get("key_ID")

            fetch_qkd_key_id()

            fetch_json(f"{config['remote_qupskd_url']}/ack")

            psk_update()
        except Exception as e:
            logger.error(e)
            await asyncio.sleep(1)


async def main():
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((config["qupskd_bind"], config["qupskd_port"]))
                s.close()
                break
        except OSError:
            logger.warning(
                f"Waiting for {config['qupskd_bind']}:{config['qupskd_port']} to become available"
            )
            await asyncio.sleep(1)

    # Run the server in a separate thread because it's not async
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    await fetch_peer_data()


if __name__ == "__main__":
    asyncio.run(main())
