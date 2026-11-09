"""A relay for the tests: what a public one does with legion's events (NIP-01), on 127.0.0.1."""

import base64
import hashlib
import json
import socket
import threading
import time

from legion import nostr

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _Server(nostr.WS):
    """The server's end of a WebSocket, read and written as the client's is."""

    def __init__(self, sock: socket.socket):
        sock.settimeout(10)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise nostr.Closed("gone")
            head += chunk
        key = next(line.split(b":", 1)[1].strip() for line in head.split(b"\r\n")
                   if line.lower().startswith(b"sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest()).decode()
        sock.sendall((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        self.sock, self.buf, self.parts, self.lock = sock, bytearray(head.partition(b"\r\n\r\n")[2]), [], threading.Lock()
        self.seen = time.time()


def matches(f: dict, ev: dict) -> bool:
    if "ids" in f and ev["id"] not in f["ids"]:
        return False
    if "kinds" in f and ev["kind"] not in f["kinds"]:
        return False
    if "authors" in f and ev["pubkey"] not in f["authors"]:
        return False
    if "since" in f and ev["created_at"] < f["since"]:
        return False
    if "until" in f and ev["created_at"] > f["until"]:
        return False
    for key, wanted in f.items():
        if key.startswith("#") and not any(t[0] == key[1:] and t[1] in wanted for t in ev["tags"] if len(t) > 1):
            return False
    return True


class Relay:
    """`url` is where it listens. `events` is what it keeps; `refuse` (a reason) makes it refuse everything,
    `max_event` is the size it takes, `limit` the most it returns for a request."""

    def __init__(self, max_event: int = 65536, limit: int = 500):
        self.events, self.subs, self.lock = [], {}, threading.Lock()
        self.max_event, self.limit, self.refuse, self.received = max_event, limit, None, 0
        self.server = socket.create_server(("127.0.0.1", 0))
        self.url = f"ws://127.0.0.1:{self.server.getsockname()[1]}"
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while self.running:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock) -> None:
        try:
            ws = _Server(sock)
        except Exception:  # noqa: BLE001
            sock.close()
            return
        try:
            while self.running:
                raw = ws.recv(0.5)
                if raw is None:
                    continue
                msg = json.loads(raw)
                if msg[0] == "EVENT":
                    self._event(ws, msg[1])
                elif msg[0] == "REQ":
                    sub, filters = msg[1], msg[2:]
                    with self.lock:
                        found = sorted((e for e in self.events if any(matches(f, e) for f in filters)),
                                       key=lambda e: -e["created_at"])
                        limit = min([self.limit] + [f["limit"] for f in filters if "limit" in f])
                        self.subs[(ws, sub)] = filters
                    for e in found[:limit]:
                        ws.send(json.dumps(["EVENT", sub, e]))
                    ws.send(json.dumps(["EOSE", sub]))
                elif msg[0] == "CLOSE":
                    with self.lock:
                        self.subs.pop((ws, msg[1]), None)
        except (nostr.Closed, OSError, ValueError):
            pass
        finally:
            with self.lock:
                for key in [k for k in self.subs if k[0] is ws]:
                    del self.subs[key]
            ws.close()

    def _event(self, ws, ev: dict) -> None:
        self.received += 1
        if self.refuse:
            return ws.send(json.dumps(["OK", ev.get("id"), False, self.refuse]))
        if len(json.dumps(ev)) > self.max_event:
            return ws.send(json.dumps(["OK", ev.get("id"), False, "invalid: event too large"]))
        if not nostr.valid(ev):
            return ws.send(json.dumps(["OK", ev.get("id"), False, "invalid: bad signature"]))
        with self.lock:
            if any(e["id"] == ev["id"] for e in self.events):
                return ws.send(json.dumps(["OK", ev["id"], True, "duplicate: already have this event"]))
            if not 20000 <= ev["kind"] < 30000:  # the ephemeral ones are passed on, not kept
                self.events.append(ev)
            subs = [(w, sub) for (w, sub), filters in self.subs.items() if any(matches(f, ev) for f in filters)]
        ws.send(json.dumps(["OK", ev["id"], True, ""]))
        for w, sub in subs:
            try:
                w.send(json.dumps(["EVENT", sub, ev]))
            except (nostr.Closed, OSError):
                pass

    def close(self) -> None:
        self.running = False
        try:
            self.server.close()
        except OSError:
            pass
