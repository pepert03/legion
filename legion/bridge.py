"""A local SOCKS5 server that opens every connection through an HTTP proxy (CONNECT).

Radicle's node can send all its connections through a SOCKS5 proxy (`node.proxy`), but sandboxes and
corporate networks usually only offer an HTTP proxy (HTTPS_PROXY). This bridges the two, so a node in
such a network reaches the seeds as long as the HTTP proxy allows CONNECT to their port.

    python -m legion.bridge --proxy http://user:pass@proxy:3128 --listen 127.0.0.1:1080
"""

import argparse
import asyncio
import base64
import ipaddress
import os
import struct
import sys
from urllib.parse import unquote, urlparse

TIMEOUT = 30


def proxy_from_env() -> str | None:
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(key)
        if value and value.startswith(("http://", "https://")) or (value and "://" not in value):
            return value if "://" in value else "http://" + value
    return None


class Proxy:
    def __init__(self, url: str):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"not an HTTP proxy URL: {url}")
        self.tls = u.scheme == "https"
        self.host, self.port = u.hostname, u.port or (443 if self.tls else 80)
        self.auth = None
        if u.username:
            creds = f"{unquote(u.username)}:{unquote(u.password or '')}"
            self.auth = "Basic " + base64.b64encode(creds.encode()).decode()

    async def connect(self, host: str, port: int):
        """A raw TCP stream to host:port through the proxy, or an exception saying why not."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=self.tls or None), TIMEOUT)
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if self.auth:
            head += f"Proxy-Authorization: {self.auth}\r\n"
        writer.write((head + "\r\n").encode())
        await writer.drain()
        answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        status = answer.split(b"\r\n", 1)[0].decode("latin-1")
        if " 200" not in status:
            writer.close()
            raise ConnectionRefusedError(f"the proxy refused CONNECT {target}: {status}")
        return reader, writer


async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def socks(proxy: Proxy, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, log) -> None:
    def reply(code: int) -> bytes:  # SOCKS5 reply with an empty IPv4 bind address
        return bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0])

    try:
        ver, n = await reader.readexactly(2)
        await reader.readexactly(n)
        if ver != 5:
            return writer.close()
        writer.write(b"\x05\x00")  # no authentication
        ver, cmd, _, atyp = await reader.readexactly(4)
        if atyp == 1:
            host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
        elif atyp == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        elif atyp == 4:
            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        else:
            writer.write(reply(8))
            return writer.close()
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        if cmd != 1:  # only CONNECT
            writer.write(reply(7))
            return writer.close()
        try:
            up_r, up_w = await proxy.connect(host, port)
        except Exception as e:
            log(f"{host}:{port}: {e}")
            writer.write(reply(5))
            return writer.close()
        log(f"{host}:{port}: connected")
        writer.write(reply(0))
        await writer.drain()
        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))
    except (asyncio.IncompleteReadError, ConnectionError):
        writer.close()


async def serve(proxy_url: str, host: str, port: int, quiet: bool = False) -> None:
    proxy = Proxy(proxy_url)
    log = (lambda m: None) if quiet else (lambda m: print(f"bridge: {m}", file=sys.stderr, flush=True))
    server = await asyncio.start_server(lambda r, w: socks(proxy, r, w, log), host, port)
    log(f"SOCKS5 on {host}:{port}, through {proxy.host}:{proxy.port}")
    async with server:
        await server.serve_forever()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m legion.bridge", description=__doc__.split("\n\n")[0])
    ap.add_argument("--proxy", default=proxy_from_env(), help="HTTP proxy URL (default: HTTPS_PROXY and friends)")
    ap.add_argument("--listen", default="127.0.0.1:1080")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if not a.proxy:
        ap.error("no HTTP proxy: pass --proxy or set HTTPS_PROXY")
    host, port = a.listen.rsplit(":", 1)
    try:
        asyncio.run(serve(a.proxy, host, int(port), a.quiet))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
