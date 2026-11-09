"""Relays, as a client: signed events pushed to and read from several public relays at once, over a WebSocket
on port 443 (the Nostr protocol, NIP-01). Nothing here knows legion: live.py wraps its events in these.

Why relays: an event sent to one reaches whoever is subscribed in tens of milliseconds, the relay keeps it for
whoever is away, and all it takes is an outbound HTTPS connection and a key made here, no account. Over Radicle an
event waited for a seed to come and fetch it: minutes to hours, measured (docs/diagnostico-2026-11-09.md).

In plain Python on purpose: the signature relays ask for (BIP-340 Schnorr on secp256k1) is not in `cryptography`,
and the sandboxes agents run in can't always build a C extension. A signature takes some 20 ms.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlsplit

from .bridge import Proxy, proxy_from_env

# --- BIP-340 signatures (from the BIP's reference implementation, public domain) -----------------------------

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and a[1] != b[1]:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(pt, n: int):
    r = None
    while n:
        if n & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        n >>= 1
    return r


def _tagged(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


_points: dict[bytes, tuple] = {}


def _point(sec: bytes):
    if sec not in _points:
        d = int.from_bytes(sec, "big")
        if not 1 <= d < N:
            raise ValueError("not a secp256k1 secret key")
        _points[sec] = _mul(G, d)
    return _points[sec]


def pubkey(sec: bytes) -> bytes:
    """The x-only public key of a 32-byte secret key."""
    return _point(sec)[0].to_bytes(32, "big")


def sign(msg: bytes, sec: bytes, aux: bytes | None = None) -> bytes:
    pt = _point(sec)
    d0 = int.from_bytes(sec, "big")
    d = d0 if pt[1] % 2 == 0 else N - d0
    px = pt[0].to_bytes(32, "big")
    t = (d ^ int.from_bytes(_tagged("BIP0340/aux", aux if aux is not None else os.urandom(32)), "big"))
    k0 = int.from_bytes(_tagged("BIP0340/nonce", t.to_bytes(32, "big") + px + msg), "big") % N
    r = _mul(G, k0)
    k = k0 if r[1] % 2 == 0 else N - k0
    rx = r[0].to_bytes(32, "big")
    e = int.from_bytes(_tagged("BIP0340/challenge", rx + px + msg), "big") % N
    return rx + ((k + e * d) % N).to_bytes(32, "big")


def verify(msg: bytes, pub: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    x, r, s = int.from_bytes(pub, "big"), int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if x >= P or r >= P or s >= N:
        return False
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2:
        return False
    e = int.from_bytes(_tagged("BIP0340/challenge", sig[:32] + pub + msg), "big") % N
    pt = _add(_mul(G, s), _mul((x, y if y % 2 == 0 else P - y), N - e))
    return pt is not None and pt[1] % 2 == 0 and pt[0] == r


# --- events ----------------------------------------------------------------------------------------------


def event(sec: bytes, kind: int, tags: list[list[str]], content: str, created_at: float | None = None) -> dict:
    """A signed event, as relays take it."""
    pub = pubkey(sec).hex()
    at = int(created_at if created_at is not None else time.time())
    body = json.dumps([0, pub, at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    eid = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {"id": eid, "pubkey": pub, "created_at": at, "kind": kind, "tags": tags, "content": content,
            "sig": sign(bytes.fromhex(eid), sec).hex()}


def valid(ev: dict) -> bool:
    """Whether an event is what its id and signature say (relays check it; the tests' relay does too)."""
    try:
        body = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                          separators=(",", ":"), ensure_ascii=False)
        return (hashlib.sha256(body.encode("utf-8")).hexdigest() == ev["id"]
                and verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]), bytes.fromhex(ev["sig"])))
    except (KeyError, TypeError, ValueError):
        return False


# --- a WebSocket, client side ------------------------------------------------------------------------------


class Closed(ConnectionError):
    pass


def _mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


PROXY: str | None = None  # an HTTP proxy to use when the environment names none (live.py: git's http.proxy)
CA_FILES: list[str] = []  # more certificate authorities to trust (live.py: git's http.sslCAInfo)


def proxy() -> str | None:
    return proxy_from_env() or PROXY


def tls() -> ssl.SSLContext:
    """What a TLS connection is checked against: the system's authorities, and the ones the other tools here were
    told to trust. A proxy that inspects TLS signs with an authority of its own, which the sandbox gives git,
    curl or node in a variable of theirs: Python, not told, refused every relay."""
    ctx = ssl.create_default_context()
    for path in [os.environ.get(k) for k in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
                                             "NODE_EXTRA_CA_CERTS")] + CA_FILES:
        if path and os.path.isfile(path):
            try:
                ctx.load_verify_locations(cafile=path)
            except (ssl.SSLError, OSError):
                pass
    return ctx


def _open(host: str, port: int, timeout: float) -> socket.socket:
    """A TCP connection to host:port: straight, or through the HTTP proxy the environment names (CONNECT), which
    is all some sandboxes have."""
    url = proxy()
    bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    local = host in ("localhost", "127.0.0.1", "::1") or any(
        h.strip() and (h.strip() == "*" or host.endswith(h.strip().lstrip("."))) for h in bypass.split(","))
    if not url or local:
        return socket.create_connection((host, port), timeout=timeout)
    via = Proxy(url)
    s = socket.create_connection((via.host, via.port), timeout=timeout)
    if via.tls:
        s = tls().wrap_socket(s, server_hostname=via.host)
    head = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if via.auth:
        head += f"Proxy-Authorization: {via.auth}\r\n"
    s.sendall((head + "\r\n").encode())
    answer = b""
    while b"\r\n\r\n" not in answer:
        chunk = s.recv(4096)
        if not chunk or len(answer) > 65536:
            break
        answer += chunk
    status = answer.split(b"\r\n", 1)[0].decode("latin-1")
    if " 200" not in status:
        s.close()
        raise Closed(f"the proxy refused CONNECT {host}:{port}: {status or 'no answer'}")
    return s


class WS:
    """One WebSocket connection: text messages out and in. One thread reads; any thread may send."""

    def __init__(self, url: str, timeout: float = 10):
        u = urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise ValueError(f"not a relay address: {url}")
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        s = _open(u.hostname, port, timeout)
        try:
            if secure:
                s = tls().wrap_socket(s, server_hostname=u.hostname)
            s.settimeout(timeout)
            key = base64.b64encode(os.urandom(16)).decode()
            host = u.hostname if port in (80, 443) else f"{u.hostname}:{port}"
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: legion\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = s.recv(4096)
                if not chunk or len(head) > 65536:
                    raise Closed(f"{u.hostname} closed during the handshake")
                head += chunk
            head, _, rest = head.partition(b"\r\n\r\n")
            status = head.split(b"\r\n", 1)[0].decode("latin-1")
            if " 101" not in status:
                raise Closed(f"{u.hostname} is not a relay here: {status}")
        except BaseException:
            s.close()
            raise
        self.sock, self.buf, self.parts, self.lock = s, bytearray(rest), [], threading.Lock()
        self.seen = time.time()  # when the other side last sent anything, a pong included

    def send(self, data, opcode: int = 1) -> None:
        data = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        n, head = len(data), bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        try:
            with self.lock:
                self.sock.settimeout(30)
                self.sock.sendall(head + mask + _mask(data, mask))
        except OSError as e:
            raise Closed(str(e))

    def recv(self, timeout: float) -> str | None:
        """The next message; None when none came within `timeout`. Closed when the other side is gone."""
        end = time.time() + timeout
        while True:
            msg = self._message()
            if msg is not None:
                return msg
            left = end - time.time()
            if left <= 0:
                return None
            try:
                self.sock.settimeout(left)
                chunk = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                return None
            except OSError as e:
                raise Closed(str(e))
            if not chunk:
                raise Closed("the relay closed the connection")
            self.buf += chunk

    def _message(self) -> str | None:
        while True:
            b = self.buf
            if len(b) < 2:
                return None
            n, pos = b[1] & 0x7F, 2
            if n == 126:
                if len(b) < 4:
                    return None
                n, pos = struct.unpack(">H", bytes(b[2:4]))[0], 4
            elif n == 127:
                if len(b) < 10:
                    return None
                n, pos = struct.unpack(">Q", bytes(b[2:10]))[0], 10
            masked = b[1] & 0x80
            if masked:
                pos += 4
            if len(b) < pos + n:
                return None
            payload = bytes(b[pos:pos + n])
            if masked:
                payload = _mask(payload, bytes(b[pos - 4:pos]))
            fin, op = b[0] & 0x80, b[0] & 0x0F
            del b[:pos + n]
            self.seen = time.time()
            if op == 8:
                raise Closed("the relay closed the connection")
            if op == 9:
                self.send(payload, 10)
            elif op in (0, 1, 2):
                self.parts.append(payload)
                if fin:
                    data, self.parts = b"".join(self.parts), []
                    return data.decode("utf-8", "replace")

    def ping(self) -> None:
        self.send(b"", 9)

    def close(self) -> None:
        try:
            self.send(b"", 8)
        except (Closed, OSError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def why(url: str, timeout: float = 8) -> str | None:
    """Why this relay can't be reached from here, in a line, or None when it can: what `legion relay` says."""
    try:
        WS(url, timeout).close()
        return None
    except ssl.SSLCertVerificationError as e:
        return (f"its certificate isn't trusted here ({e.verify_message}): a proxy that inspects TLS signs with its "
                "own authority; point SSL_CERT_FILE at the bundle the other tools here use")
    except Exception as e:  # noqa: BLE001
        return " ".join(str(e).split())[:200] or type(e).__name__


# --- relays ---------------------------------------------------------------------------------------------

CONNECT = 6  # seconds to reach a relay; one that doesn't answer is left out of the round
GRACE = 0.3  # seconds the others get once a relay has answered: a slow or dead one doesn't hold a command
_pool: dict[str, WS] = {}  # the connections this process keeps, one per relay: a command reads, then writes
_pool_lock = threading.Lock()
# relay -> when it last failed or was too slow to wait for, so a command doesn't wait for it twice (live.py keeps
# it between commands)
_down: dict[str, float] = {}


class Slow(Exception):
    """The relay answers, but not in the time given."""


def _with(url: str, fn):
    """fn(connection) on this process's connection to the relay, or on a new one when that one has gone (a
    relay closes the ones left idle)."""
    with _pool_lock:
        ws = _pool.pop(url, None)
    if ws is not None:
        try:
            result = fn(ws)
        except (Closed, OSError):
            ws.close()
        except BaseException:
            ws.close()
            raise
        else:
            _keep(url, ws)
            return result
    ws = WS(url, CONNECT)
    try:
        result = fn(ws)
    except BaseException:
        ws.close()
        raise
    _keep(url, ws)
    return result


def _keep(url: str, ws: WS) -> None:
    with _pool_lock:
        old = _pool.pop(url, None)
        _pool[url] = ws
    if old:
        old.close()


def close_all() -> None:
    with _pool_lock:
        conns = list(_pool.values())
        _pool.clear()
    for ws in conns:
        ws.close()


def _each(urls: list[str], fn, timeout: float, grace: float) -> dict:
    """fn(url) for every relay at once. {url: its result} for those done within `timeout`, or within `grace`
    seconds of the first one that worked: a slow relay doesn't hold a command."""
    out, done = {}, threading.Event()

    def run(u):
        try:
            out[u] = fn(u)
            _down.pop(u, None)
        except Exception as e:  # noqa: BLE001 (a relay's trouble is its own)
            out[u] = e
            _down[u] = time.time()
        done.set()

    threads = [threading.Thread(target=run, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    end, first = time.time() + timeout, None
    while time.time() < end and len(out) < len(urls):
        done.wait(0.02)
        done.clear()
        if first is None and any(not isinstance(v, Exception) for v in list(out.values())):
            first = time.time()
        if first is not None and time.time() - first >= grace:
            break
    got = dict(out)
    for u in urls:
        if u not in got:
            _down[u] = time.time()  # not in time: the next rounds go on without it for a while
    return got


def publish(urls: list[str], events: list[dict], timeout: float = 8, grace: float = GRACE) -> dict[str, dict]:
    """Send the events to every relay. {relay: {event id: why it was refused, or None}} for the relays that
    answered; an event is out when one relay took it."""
    def send(ws):
        for ev in events:
            ws.send(json.dumps(["EVENT", ev], separators=(",", ":"), ensure_ascii=False))
        answers, end = {}, time.time() + timeout
        while len(answers) < len(events) and time.time() < end:
            raw = ws.recv(end - time.time())
            if raw is None:
                break
            msg = _parse(raw)
            if msg and msg[0] == "OK" and len(msg) >= 3:
                dup = len(msg) > 3 and str(msg[3]).startswith("duplicate")
                answers[msg[1]] = None if msg[2] or dup else str(msg[3] if len(msg) > 3 else "refused")
        return answers

    done = _each(urls, lambda url: _with(url, send), timeout, grace)
    return {u: r for u, r in done.items() if not isinstance(r, Exception)}


def query(urls: list[str], filters: list[dict], timeout: float = 6, grace: float = GRACE) -> tuple[list[dict], list[str]]:
    """What the relays hold that matches: (events, each once; the relays that answered in full)."""
    def ask(ws):
        sub = os.urandom(6).hex()
        ws.send(json.dumps(["REQ", sub, *filters], separators=(",", ":")))
        got, end = [], time.time() + timeout
        while True:
            raw = ws.recv(max(0.0, end - time.time()))
            if raw is None:
                raise Slow("the relay didn't finish answering")
            msg = _parse(raw)
            if not msg or len(msg) < 2 or msg[1] != sub:
                continue
            if msg[0] == "EVENT" and len(msg) > 2 and isinstance(msg[2], dict):
                got.append(msg[2])
            elif msg[0] == "EOSE":
                break
            elif msg[0] == "CLOSED":
                raise Slow(f"the relay refused the request: {msg[2] if len(msg) > 2 else ''}")
        ws.send(json.dumps(["CLOSE", sub]))
        return got

    seen, events, answered = set(), [], []
    for url, result in _each(urls, lambda url: _with(url, ask), timeout, grace).items():
        if isinstance(result, Exception):
            continue
        answered.append(url)
        for ev in result:
            if ev.get("id") not in seen:
                seen.add(ev.get("id"))
                events.append(ev)
    return events, answered


def _parse(raw: str):
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, list) and msg else None


PING = 40  # seconds between pings on a connection that stays open; three unanswered and it's opened again
RETRY = (2, 5, 15, 30, 60)  # seconds before connecting again to a relay that went


def listen(urls: list[str], filters, on_event, stop: threading.Event, on_state=None) -> list[threading.Thread]:
    """Stay subscribed to every relay: on_event(event, relay) for each one as it's published, from a thread per
    relay, until `stop` is set. `filters` is called at every connection (so it can ask only for what's new since
    the last). on_state(relay, connected) says when one comes or goes."""
    def run(url):
        tries = 0
        while not stop.is_set():
            ws = None
            try:
                ws = WS(url, CONNECT)
                sub = os.urandom(6).hex()
                ws.send(json.dumps(["REQ", sub, *filters()], separators=(",", ":")))
                if on_state:
                    on_state(url, True)
                tries, pinged = 0, time.time()
                while not stop.is_set():
                    raw = ws.recv(1.0)
                    if raw is None:
                        if time.time() - ws.seen > 3 * PING:
                            raise Closed("no answer to the pings")
                        if time.time() - pinged >= PING:
                            pinged = time.time()
                            ws.ping()
                        continue
                    msg = _parse(raw)
                    if msg and msg[0] == "EVENT" and len(msg) > 2 and msg[1] == sub and isinstance(msg[2], dict):
                        on_event(msg[2], url)
                    elif msg and msg[0] == "CLOSED":
                        raise Closed("the relay closed the subscription")
            except Exception:  # noqa: BLE001 (the relay went, or isn't one: again later)
                pass
            finally:
                if ws:
                    ws.close()
                if on_state:
                    on_state(url, False)
            stop.wait(RETRY[min(tries, len(RETRY) - 1)])
            tries += 1

    threads = [threading.Thread(target=run, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    return threads
