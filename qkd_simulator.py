import base64
import json
import logging
import uuid
from hashlib import sha3_256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from sys import argv

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.DEBUG,
    datefmt="%Y-%m-%d %H:%M:%S",
)


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_version = "quPSKd/1.0"

    def log_message(self, format: str, *args) -> None:
        logger.debug(format % args)

    def log_error(self, format: str, *args) -> None:
        logger.warning(format % args)

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

    def handle_404(self):
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"404 Not Found")

    def do_GET(self):
        if self.path.startswith("/api/v1/keys/"):
            self.handle_keys()
        else:
            self.handle_404()


if __name__ == "__main__":
    bind, port = argv[1], int(argv[2])
    httpd = ThreadingHTTPServer((bind, port), SimpleHTTPRequestHandler)
    logger.info(f"Serving at http://{bind}:{port}")
    httpd.serve_forever()
