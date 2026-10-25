"""Can this machine reach Radicle's seeds, directly or through its HTTP proxy? (`legion radicle doctor`)"""

import os
import socket
import ssl
import tempfile

from .bridge import Proxy, proxy_from_env

SEEDS = [("rosa.radicle.network", 8776), ("iris.radicle.network", 8776),
         ("rosa.radicle.network", 58776), ("iris.radicle.network", 58776), ("defelo.de", 58776)]
TIMEOUT = 10


def direct(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return "ok"
    except OSError as e:
        return f"no ({e.__class__.__name__}: {e})"


def tunnel(proxy: Proxy, host: str, port: int) -> socket.socket:
    """A socket connected to host:port through the proxy's CONNECT, or an exception saying why not."""
    s = socket.create_connection((proxy.host, proxy.port), timeout=TIMEOUT)
    if proxy.tls:
        s = ssl.create_default_context().wrap_socket(s, server_hostname=proxy.host)
    head = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if proxy.auth:
        head += f"Proxy-Authorization: {proxy.auth}\r\n"
    s.sendall((head + "\r\n").encode())
    answer = b""
    while b"\r\n\r\n" not in answer:
        chunk = s.recv(4096)
        if not chunk:
            break
        answer += chunk
    status = answer.split(b"\r\n", 1)[0].decode("latin-1")
    if " 200" not in status:
        s.close()
        raise ConnectionRefusedError(f"refused: {status or 'no answer'}")
    return s


def through(proxy: Proxy, host: str, port: int) -> str:
    try:
        s = tunnel(proxy, host, port)
    except OSError as e:
        return f"no ({e})"
    s.settimeout(2)  # a Radicle seed waits for us to speak first: a tunnel that stays open and quiet is right
    try:
        result = "opened, then closed by the proxy" if s.recv(1) == b"" else "ok"
    except socket.timeout:
        result = "ok"
    except OSError as e:
        result = f"opened, then failed ({e})"
    s.close()
    return result


def issuer(proxy: Proxy) -> str:
    """Who signed github.com's certificate, seen through the proxy: a company CA means it inspects TLS."""
    try:
        s = tunnel(proxy, "github.com", 443)
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with ctx.wrap_socket(s, server_hostname="github.com") as tls:
            der = tls.getpeercert(binary_form=True)
    except OSError as e:
        return f"couldn't check ({e})"
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(ssl.DER_cert_to_PEM_cert(der))
    try:
        cert = ssl._ssl._test_decode_cert(f.name)  # noqa: SLF001 (the only stdlib way to read an unverified one)
    finally:
        os.unlink(f.name)
    return ", ".join(f"{k}={v}" for part in cert.get("issuer", ()) for k, v in part) or "unknown"


def doctor() -> list[str]:
    url = proxy_from_env()
    lines = [f"HTTP proxy in the environment: {url.split('@')[-1] if url else 'none'}",
             "Direct connections (what Radicle does by default):"]
    ok_direct = ok_proxy = False
    for host, port in SEEDS:
        res = direct(host, port)
        ok_direct |= res == "ok"
        lines.append(f"  {host}:{port}  {res}")
    if url:
        proxy = Proxy(url)
        lines.append("Through the HTTP proxy (CONNECT):")
        for host, port in SEEDS:
            res = through(proxy, host, port)
            ok_proxy |= res == "ok"
            lines.append(f"  {host}:{port}  {res}")
        lines.append(f"github.com's certificate through the proxy is issued by: {issuer(proxy)}")
    lines.append("")
    if ok_direct:
        lines.append("Verdict: the seeds are reachable directly; Radicle works as is.")
    elif ok_proxy:
        lines.append("Verdict: reachable only through the proxy. `legion radicle setup` routes Radicle through it "
                     "with a local bridge.")
    else:
        lines.append("Verdict: no route to any seed, not even through the proxy, so Radicle can't work from here. "
                     "Send this report to whoever runs the project.")
    return lines
