#!/usr/bin/env python3
"""
Chrome Control Native Messaging Host

Bridges Chrome extension <-> Unix socket for local automation.
"""
from __future__ import annotations

import sys
import json
import struct
import os
import socket
import select
import base64
import tempfile
import time
import uuid
import fcntl

SOCKET_DIR = "/tmp"
REGISTRY_PATH = "/tmp/chrome_control_registry.json"
LOG_FILE = "/tmp/chrome_control.log"

_read_buffer = bytearray()
_expected_length = None


def log(msg: str) -> None:
    line = f"[NativeHost] {msg}\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(line)


def read_message() -> dict | str | None:
    """Read message from Chrome using native messaging protocol."""
    global _read_buffer, _expected_length

    try:
        try:
            chunk = sys.stdin.buffer.read(65536)
            if chunk:
                _read_buffer.extend(chunk)
            else:
                # EOF: Chrome closed the stdio pipe (typically because the
                # MV3 service worker was evicted). Discard any half-frame
                # in the buffer — the rest is never coming. Without this
                # reset, EOF + non-empty buffer falls through to the length
                # parser, which returns "incomplete" forever; select() keeps
                # reporting stdin readable on EOF, so the main loop spins
                # at 100% CPU until the host is killed.
                _read_buffer.clear()
                _expected_length = None
                return None
        except BlockingIOError:
            if len(_read_buffer) == 0:
                return "wouldblock"

        if len(_read_buffer) < 4:
            return "incomplete"

        if _expected_length is None:
            _expected_length = struct.unpack('<I', _read_buffer[:4])[0]

        total_needed = 4 + _expected_length
        if len(_read_buffer) < total_needed:
            return "incomplete"

        message_bytes = bytes(_read_buffer[4:total_needed])
        _read_buffer = _read_buffer[total_needed:]
        _expected_length = None

        return json.loads(message_bytes.decode('utf-8'))

    except Exception as e:
        log(f"read_message error: {e}")
        _read_buffer.clear()
        _expected_length = None
        return None


def send_message(message: dict) -> None:
    """Send message to Chrome using native messaging protocol."""
    message_bytes = json.dumps(message).encode('utf-8')
    length = len(message_bytes)
    sys.stdout.buffer.write(struct.pack('<I', length))
    sys.stdout.buffer.write(message_bytes)
    sys.stdout.buffer.flush()


def load_registry() -> dict:
    """Load the profile registry."""
    try:
        if os.path.exists(REGISTRY_PATH):
            with open(REGISTRY_PATH, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {"profiles": {}}


def save_registry(registry: dict) -> None:
    """Save the profile registry with restricted permissions."""
    fd = os.open(REGISTRY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(registry, f, indent=2)


def cleanup_stale_profiles(registry: dict) -> dict:
    """Remove profiles whose sockets no longer exist or whose PIDs are dead."""
    active = {}
    for profile_id, info in registry.get("profiles", {}).items():
        socket_path = info.get("socket")
        pid = info.get("pid")
        # Check if socket exists and process is alive
        if socket_path and os.path.exists(socket_path):
            try:
                os.kill(pid, 0)  # Check if process exists
                active[profile_id] = info
            except (OSError, TypeError):
                # Process dead, clean up socket
                try:
                    os.remove(socket_path)
                except Exception:
                    pass
    registry["profiles"] = active
    return registry


class ChromeControlHost:
    def __init__(self):
        self.running = True
        self.pending_requests: dict[str, socket.socket] = {}
        self.request_id = 0
        self.socket_server: socket.socket | None = None
        self.clients: list[socket.socket] = []
        self.screenshot_chunks: dict[str, dict] = {}
        self.socket_path: str | None = None
        self.profile_id: str | None = None
        self.profile_name: str | None = None

    def setup_socket(self):
        # Generate unique socket path
        socket_id = uuid.uuid4().hex[:8]
        self.socket_path = f"{SOCKET_DIR}/chrome_control_{socket_id}.sock"

        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

        self.socket_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket_server.bind(self.socket_path)
        self.socket_server.listen(5)
        self.socket_server.setblocking(False)
        os.chmod(self.socket_path, 0o700)
        log(f"Listening on {self.socket_path}")

    def register_profile(self, extension_id: str, profile_name: str | None = None):
        """Register this instance in the shared registry."""
        self.profile_id = extension_id
        self.profile_name = profile_name or extension_id[:8]

        registry = load_registry()
        registry = cleanup_stale_profiles(registry)
        registry["profiles"][self.profile_id] = {
            "socket": self.socket_path,
            "pid": os.getpid(),
            "name": self.profile_name,
            "started": int(time.time())
        }
        save_registry(registry)
        log(f"Registered profile: {self.profile_name} ({self.profile_id})")

    def unregister_profile(self):
        """Remove this instance from the registry."""
        if self.profile_id:
            try:
                registry = load_registry()
                if self.profile_id in registry.get("profiles", {}):
                    del registry["profiles"][self.profile_id]
                    save_registry(registry)
                    log(f"Unregistered profile: {self.profile_name}")
            except Exception:
                pass

    def cleanup_client(self, client: socket.socket):
        """Remove client and clean up any pending requests for it."""
        if client in self.clients:
            self.clients.remove(client)
        # Remove pending requests for this client
        to_remove = [k for k, v in self.pending_requests.items() if v is client]
        for k in to_remove:
            del self.pending_requests[k]
            # Also clean up any screenshot chunks for this request
            if k in self.screenshot_chunks:
                del self.screenshot_chunks[k]
        try:
            client.close()
        except Exception:
            pass

    def is_client_valid(self, client: socket.socket) -> bool:
        """Check if client socket is still valid."""
        try:
            return client.fileno() != -1 and client in self.clients
        except Exception:
            return False

    def handle_client_message(self, client: socket.socket, data: bytes):
        try:
            message = json.loads(data.decode('utf-8'))

            # Special reload command - send directly to extension
            if message.get('command') == '_reload_extension':
                log("Sending reload signal to extension")
                send_message({'type': 'reload'})
                try:
                    client.sendall(json.dumps({'status': 'reload_sent'}).encode() + b'\n')
                except Exception:
                    pass
                return

            self.request_id += 1
            msg_id = f"req_{self.request_id}"

            self.pending_requests[msg_id] = client
            message['id'] = msg_id
            send_message(message)
            log(f"Forwarded: {message.get('command', 'unknown')}")

        except Exception as e:
            log(f"Client message error: {e}")
            try:
                client.sendall(json.dumps({'error': str(e)}).encode() + b'\n')
            except Exception:
                pass

    def handle_extension_message(self, message: dict):
        msg_type = message.get('type')

        if msg_type == 'heartbeat':
            # Respond to keepalive to confirm connection is healthy
            log("Heartbeat received, sending pong")
            send_message({'type': 'pong'})
            return

        if msg_type == 'extension_ready':
            log("Extension ready - clearing stale state")
            self.screenshot_chunks.clear()  # Clear any orphaned chunks from previous session
            # Register profile with extension ID
            ext_id = message.get('extensionId', 'unknown')
            profile_name = message.get('profileName', None)
            self.register_profile(ext_id, profile_name)
            return

        if msg_type == 'screenshot_chunk':
            self.handle_screenshot_chunk(message)
            return

        if msg_type == 'response':
            msg_id = message.get('id')
            result = message.get('result', {})

            if isinstance(result, dict) and result.get('screenshotChunked'):
                self.finalize_screenshot(msg_id)
                return

            if msg_id in self.pending_requests:
                client = self.pending_requests.pop(msg_id)
                try:
                    response = json.dumps(message).encode() + b'\n'
                    client.setblocking(True)
                    client.sendall(response)
                except Exception as e:
                    log(f"Send error: {e}")

    def handle_screenshot_chunk(self, message: dict):
        request_id = message.get('requestId')
        index = message.get('index')
        total = message.get('total')
        data = message.get('data')
        fmt = message.get('format', 'jpeg')

        # Discard chunks for requests where client already disconnected
        if request_id not in self.pending_requests:
            if request_id in self.screenshot_chunks:
                del self.screenshot_chunks[request_id]
            return

        if request_id not in self.screenshot_chunks:
            self.screenshot_chunks[request_id] = {
                'chunks': [None] * total,
                'total': total,
                'format': fmt,
                'received': 0
            }

        chunk_info = self.screenshot_chunks[request_id]
        chunk_info['chunks'][index] = data
        chunk_info['received'] += 1

        log(f"Screenshot chunk {chunk_info['received']}/{total}")

    def finalize_screenshot(self, request_id: str):
        if request_id not in self.screenshot_chunks:
            if request_id in self.pending_requests:
                client = self.pending_requests.pop(request_id)
                try:
                    client.sendall(json.dumps({
                        'type': 'response', 'id': request_id,
                        'error': 'No screenshot chunks'
                    }).encode() + b'\n')
                except Exception:
                    pass
            return

        chunk_info = self.screenshot_chunks.pop(request_id)
        chunks = chunk_info['chunks']

        if None in chunks:
            if request_id in self.pending_requests:
                client = self.pending_requests.pop(request_id)
                try:
                    client.sendall(json.dumps({
                        'type': 'response', 'id': request_id,
                        'error': 'Missing chunks'
                    }).encode() + b'\n')
                except Exception:
                    pass
            return

        try:
            full_data = ''.join(chunks)
            image_bytes = base64.b64decode(full_data)
            ext = 'jpg' if chunk_info['format'] == 'jpeg' else chunk_info['format']

            fd, filepath = tempfile.mkstemp(suffix=f'.{ext}', prefix='chrome_screenshot_')
            os.write(fd, image_bytes)
            os.close(fd)

            log(f"Screenshot saved: {filepath}")

            if request_id in self.pending_requests:
                client = self.pending_requests.pop(request_id)
                if self.is_client_valid(client):
                    try:
                        client.setblocking(True)
                        client.sendall(json.dumps({
                            'type': 'response', 'id': request_id,
                            'result': {'screenshotPath': filepath}
                        }).encode() + b'\n')
                    except Exception as e:
                        log(f"Send error: {e}")
                else:
                    log(f"Client disconnected before screenshot response (saved to {filepath})")

        except Exception as e:
            log(f"Screenshot save error: {e}")
            if request_id in self.pending_requests:
                client = self.pending_requests.pop(request_id)
                try:
                    client.sendall(json.dumps({
                        'type': 'response', 'id': request_id,
                        'error': str(e)
                    }).encode() + b'\n')
                except Exception:
                    pass

    def run(self):
        log("Native host started")
        self.setup_socket()
        send_message({'type': 'ready'})

        fl = fcntl.fcntl(sys.stdin.buffer.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(sys.stdin.buffer.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)

        stdin_fd = sys.stdin.buffer.fileno()

        while self.running:
            try:
                read_fds = [stdin_fd, self.socket_server.fileno()]
                read_fds.extend([c.fileno() for c in self.clients])

                readable, _, _ = select.select(read_fds, [], [], 1.0)

                for fd in readable:
                    if fd == stdin_fd:
                        message = read_message()
                        if message is None:
                            log("Extension disconnected")
                            self.running = False
                            break
                        if message in ("wouldblock", "incomplete"):
                            continue
                        self.handle_extension_message(message)

                    elif fd == self.socket_server.fileno():
                        client, _ = self.socket_server.accept()
                        client.setblocking(False)
                        self.clients.append(client)
                        log("Client connected")

                    else:
                        client = next((c for c in self.clients if c.fileno() == fd), None)
                        if client:
                            try:
                                data = client.recv(65536)
                                if data:
                                    self.handle_client_message(client, data)
                                else:
                                    self.cleanup_client(client)
                                    log("Client disconnected")
                            except Exception as e:
                                log(f"Client error: {e}")
                                self.cleanup_client(client)

            except Exception as e:
                log(f"Main loop error: {e}")
                import traceback
                traceback.print_exc(file=sys.stderr)

        self.unregister_profile()
        if self.socket_server:
            self.socket_server.close()
        if self.socket_path and os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        for client in self.clients:
            client.close()

        log("Native host exiting")


if __name__ == '__main__':
    host = ChromeControlHost()
    host.run()
