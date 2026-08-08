#!/usr/bin/env python3
"""Local dashboard server: serves dashboard/public and proxies
/api/state to the delivery store, holding the monitor token server-side
so it never reaches a browser. The static assets are host-agnostic —
anything that serves dashboard/public plus this /api/state contract
(store /state passthrough + optional repo field) can host them.

Usage: make dashboard PROJECT=<name>   (default port 8790)
"""

import argparse
import http.server
import json
import os
import sys
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "dashboard" / "public"


def _store_base() -> str:
    url = os.environ.get("DELIVERY_STORE_URL")
    if url:
        return url.removesuffix("/mcp").rstrip("/")
    port = os.environ.get("DELIVERY_STORE_PORT", "8787")
    return f"http://127.0.0.1:{port}"


def _repo(project: str) -> str:
    """PR links need the GitHub repo; it lives in the project bundle."""
    try:
        text = (ROOT / "projects-config" / project /
                "project.yaml").read_text()
        for line in text.splitlines():
            if line.startswith("repo:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, repo: str = "", **kwargs):
        self.repo = repo
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path.rstrip("/") != "/api/state":
            return super().do_GET()
        token = os.environ.get("MCP_TOKEN_MONITOR", "")
        request = urllib.request.Request(
            f"{_store_base()}/state",
            headers={"X-Store-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                state = json.load(resp)
        except (urllib.error.URLError, TimeoutError) as exc:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"error": f"store unreachable: {exc}"}).encode())
            return
        if self.repo:
            state["repo"] = self.repo
        body = json.dumps(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the delivery dashboard locally.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        load_dotenv(ROOT / "projects-config" / args.project / ".env")
    except ImportError:
        pass  # env may already be exported (make loads .env)

    handler = partial(Handler, repo=_repo(args.project))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"[dashboard] {args.project}: http://127.0.0.1:{args.port} "
          f"(store: {_store_base()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
