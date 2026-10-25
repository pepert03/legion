"""The SOCKS5-to-HTTP-CONNECT bridge, against a local echo server and a local CONNECT proxy."""

import asyncio
import socket
import struct

from legion import bridge


async def echo(r, w):
    while data := await r.read(1024):
        w.write(data)
        await w.drain()
    w.close()


async def connect_proxy(r, w, seen):
    head = await r.readuntil(b"\r\n\r\n")
    method, target, _ = head.split(b"\r\n")[0].decode().split(" ")
    seen.append(target)
    host, port = target.rsplit(":", 1)
    ur, uw = await asyncio.open_connection(host, int(port))
    w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await w.drain()
    await asyncio.gather(bridge.pipe(r, uw), bridge.pipe(ur, w))


def test_a_socks_connection_goes_through_the_http_proxy():
    async def scenario():
        seen = []
        echo_srv = await asyncio.start_server(echo, "127.0.0.1", 0)
        proxy_srv = await asyncio.start_server(lambda r, w: connect_proxy(r, w, seen), "127.0.0.1", 0)
        proxy = bridge.Proxy(f"http://127.0.0.1:{proxy_srv.sockets[0].getsockname()[1]}")
        socks_srv = await asyncio.start_server(lambda r, w: bridge.socks(proxy, r, w, lambda m: None), "127.0.0.1", 0)
        echo_port, socks_port = echo_srv.sockets[0].getsockname()[1], socks_srv.sockets[0].getsockname()[1]

        def client():  # what Radicle's node does with its SOCKS5 proxy
            with socket.create_connection(("127.0.0.1", socks_port), timeout=5) as s:
                s.sendall(b"\x05\x01\x00")
                assert s.recv(2) == b"\x05\x00"
                name = b"localhost"
                s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack("!H", echo_port))
                assert s.recv(10)[1] == 0  # success
                s.sendall(b"hello")
                return s.recv(5)

        assert await asyncio.to_thread(client) == b"hello"
        assert seen == [f"localhost:{echo_port}"]
        for srv in (echo_srv, proxy_srv, socks_srv):
            srv.close()

    asyncio.run(scenario())


def test_proxy_credentials_become_basic_auth():
    p = bridge.Proxy("http://ana:s%40cret@proxy.example:3128")
    assert (p.host, p.port) == ("proxy.example", 3128)
    assert p.auth == "Basic YW5hOnNAY3JldA=="
