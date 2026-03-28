#!/usr/bin/env python3
"""claude-oauth-proxy — Use Claude Max/Pro OAuth tokens with the Anthropic Messages API.

A lightweight proxy that injects the 3 undocumented requirements for OAuth token
authentication: beta headers, identity system prompt, and browser access header.

Pure Python stdlib — zero dependencies.
"""

import socket
import ssl
import json
import os
import sys
import datetime
import threading
import argparse

# ── Constants ──────────────────────────────────────────────────────────────────

ANTHROPIC_HOST = "api.anthropic.com"
ANTHROPIC_PORT = 443

# The 3 hidden requirements for OAuth token auth:
#
# 1. Beta headers — must include claude-code and oauth betas
OAUTH_BETAS = (
    "claude-code-20250219,"
    "oauth-2025-04-20,"
    "fine-grained-tool-streaming-2025-05-14,"
    "interleaved-thinking-2025-05-14"
)
# 2. Identity system prompt — must be present in the request body
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
# 3. Browser access header (see build_forwarded_headers below)

DEFAULT_TOKEN_FILE = os.path.expanduser("~/.claude/oauthToken")
DEFAULT_PORT = 8080

# ── Logging ────────────────────────────────────────────────────────────────────

_log_file = None

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _log_file:
        with open(_log_file, "a") as f:
            f.write(line + "\n")

# ── Token loading ──────────────────────────────────────────────────────────────

def load_token(token_file):
    """Load OAuth token from file.

    Supports two formats:
    - Plain text file containing just the token
    - JSON file with nested structure (e.g. {"claudeAiOauth":{"accessToken":"..."}})
    """
    try:
        with open(token_file) as f:
            raw = f.read().strip()

        # Try JSON first
        try:
            data = json.loads(raw)
            # Support nested format: {"claudeAiOauth":{"accessToken":"sk-ant-..."}}
            if isinstance(data, dict):
                if "claudeAiOauth" in data:
                    return data["claudeAiOauth"].get("accessToken", "")
                if "accessToken" in data:
                    return data["accessToken"]
                # Try first string value that looks like a token
                for v in data.values():
                    if isinstance(v, str) and v.startswith("sk-ant-"):
                        return v
        except (json.JSONDecodeError, ValueError):
            pass

        # Plain text token
        if raw.startswith("sk-ant-"):
            return raw

        return raw  # Return whatever we got
    except FileNotFoundError:
        log(f"Token file not found: {token_file}")
        return ""
    except Exception as e:
        log(f"Error reading token: {e}")
        return ""

# ── Request patching ───────────────────────────────────────────────────────────

def patch_body(body):
    """Inject Claude Code identity system prompt into the request body.

    This is requirement #2: the API checks for a specific system prompt string
    to validate that the request comes from Claude Code.
    """
    try:
        payload = json.loads(body)
        existing = payload.get("system", [])

        if isinstance(existing, str):
            existing = [{"type": "text", "text": existing}]
        elif not isinstance(existing, list):
            existing = []

        # Don't duplicate if already present
        has_identity = any(
            isinstance(s, dict) and CLAUDE_CODE_SYSTEM in s.get("text", "")
            for s in existing
        )
        if not has_identity:
            payload["system"] = [
                {"type": "text", "text": CLAUDE_CODE_SYSTEM}
            ] + existing

        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (json.JSONDecodeError, Exception):
        return body

def build_forwarded_headers(token, body_length):
    """Build the complete set of headers for the forwarded request.

    Includes all 3 hidden requirements:
    1. anthropic-beta with OAuth betas
    2. (body is patched separately by patch_body)
    3. anthropic-dangerous-direct-browser-access: true
    """
    return {
        "Host": ANTHROPIC_HOST,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": OAUTH_BETAS,                           # Requirement 1
        "anthropic-dangerous-direct-browser-access": "true",     # Requirement 3
        "accept": "application/json",
        "user-agent": "claude-cli/1.0.0",
        "x-app": "cli",
        "Content-Length": str(body_length),
        "Connection": "close",
    }

# ── Connection handler ─────────────────────────────────────────────────────────

def handle_client(client_sock, addr, token_file):
    try:
        # Read the full HTTP request from the client
        data = b""
        client_sock.settimeout(30)
        while True:
            chunk = client_sock.recv(131072)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data:
                header_end = data.index(b"\r\n\r\n")
                header_section = data[:header_end].decode("utf-8", "replace")
                content_length = 0
                for line in header_section.split("\r\n")[1:]:
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                        break
                body_received = len(data) - header_end - 4
                if body_received >= content_length:
                    break

        if not data or b"\r\n\r\n" not in data:
            client_sock.close()
            return

        # Parse HTTP request
        header_end = data.index(b"\r\n\r\n")
        header_section = data[:header_end].decode("utf-8", "replace")
        body = data[header_end + 4:]

        lines = header_section.split("\r\n")
        request_line = lines[0]
        parts = request_line.split(" ", 2)
        method = parts[0]
        path = parts[1] if len(parts) > 1 else "/"

        # Health check endpoint
        if method == "GET":
            resp = (
                b'HTTP/1.1 200 OK\r\n'
                b'Content-Type: application/json\r\n'
                b'Connection: close\r\n\r\n'
                b'{"status":"ok","service":"claude-oauth-proxy"}'
            )
            client_sock.sendall(resp)
            client_sock.close()
            return

        # Load token fresh for each request (handles token refresh)
        token = load_token(token_file)
        if not token:
            resp = (
                b'HTTP/1.1 500 Internal Server Error\r\n'
                b'Content-Type: application/json\r\n'
                b'Connection: close\r\n\r\n'
                b'{"error":"No OAuth token found. Check your --token-file path."}'
            )
            client_sock.sendall(resp)
            client_sock.close()
            return

        # Patch request body with identity system prompt
        patched_body = patch_body(body) if b"/messages" in path.encode() else body

        log(f"POST {path} body={len(patched_body)}b")

        # Build raw HTTPS request to Anthropic
        # (Raw TCP is used instead of urllib/requests to avoid buffering issues with SSE streaming)
        headers = build_forwarded_headers(token, len(patched_body))
        req_line = f"{method} {path} HTTP/1.1\r\n"
        header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        raw_request = (req_line + header_lines + "\r\n").encode() + patched_body

        # Connect to Anthropic via TLS
        ctx = ssl.create_default_context()
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(600)
        tls_sock = ctx.wrap_socket(raw_sock, server_hostname=ANTHROPIC_HOST)
        tls_sock.connect((ANTHROPIC_HOST, ANTHROPIC_PORT))
        tls_sock.sendall(raw_request)

        # Stream response back to client
        total_bytes = 0
        status_logged = False
        while True:
            try:
                chunk = tls_sock.recv(8192)
                if not chunk:
                    break
                if not status_logged:
                    try:
                        first_line = chunk.split(b"\r\n")[0].decode()
                        status = first_line.split(" ")[1]
                        log(f"  -> {status}")
                    except Exception:
                        pass
                    status_logged = True
                total_bytes += len(chunk)
                client_sock.sendall(chunk)
            except (socket.timeout, ConnectionError, ssl.SSLError):
                break

        log(f"  done: {total_bytes}b")
        tls_sock.close()
    except Exception as e:
        log(f"  error: {e}")
    finally:
        try:
            client_sock.close()
        except Exception:
            pass

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Proxy that enables Claude Max/Pro OAuth tokens to work with the Anthropic Messages API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use defaults (port 8080, token from ~/.claude/oauthToken)
  python3 proxy.py

  # Custom port and token file
  python3 proxy.py --port 18791 --token-file ~/.openclaw/.token-cache.json

  # With logging to file
  python3 proxy.py --log-file proxy.log

Environment variables:
  ANTHROPIC_OAUTH_TOKEN_FILE  Path to token file (overridden by --token-file)
  ANTHROPIC_PROXY_PORT        Listen port (overridden by --port)
""",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=int(os.environ.get("ANTHROPIC_PROXY_PORT", DEFAULT_PORT)),
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--token-file", "-t",
        default=os.environ.get("ANTHROPIC_OAUTH_TOKEN_FILE", DEFAULT_TOKEN_FILE),
        help=f"Path to OAuth token file (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--log-file", "-l",
        default=None,
        help="Optional log file path (logs always go to stdout)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    args = parser.parse_args()

    global _log_file
    _log_file = args.log_file

    token_file = os.path.expanduser(args.token_file)

    log("claude-oauth-proxy starting")
    log(f"Token file: {token_file}")

    token = load_token(token_file)
    if token:
        log(f"Token loaded: {token[:20]}...{token[-4:]}")
    else:
        log("WARNING: No token found! Requests will fail until a valid token is available.")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(16)
    log(f"Listening on {args.host}:{args.port}")
    log(f"Test: curl http://{args.host}:{args.port}/")

    try:
        while True:
            client_sock, addr = srv.accept()
            t = threading.Thread(
                target=handle_client,
                args=(client_sock, addr, token_file),
                daemon=True,
            )
            t.start()
    except KeyboardInterrupt:
        log("Shutting down")
        srv.close()

if __name__ == "__main__":
    main()
