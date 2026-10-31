"""How a node can be reached: its addresses on the internet and on its local network, and whether another node's
address answers. Standard library only, so it runs wherever legion does, sandboxes included.

- The addresses this machine goes out from: the source address the system picks for a route to the internet (a UDP
  socket "connected" to a public address; nothing is sent), with the prefix of its interface where the system says.
  A global one (a server, or IPv6) is reachable from the internet, unless a firewall says otherwise; a private IPv4
  one is reachable from the same network, unless the network isolates its clients.
- A port mapped on the router: UPnP IGD, or NAT-PMP, for the node's port; only worth it if the router's own address
  is global (not behind another NAT).
- Whether an address answers: a TCP connection with a short timeout. Radicle's handshake then checks it is the node
  it should be, so a device at the same address on another network is only a failed attempt.
"""

import ipaddress
import json
import re
import socket
import struct
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

from .radicle import IS_WINDOWS

PROBE_TIMEOUT = 1.5
LEASE = 3600  # seconds a router keeps a mapping; renewed well before


def _source(family: int, target: str) -> str | None:
    """The address this machine would send from to `target`. No packet leaves: connecting a UDP socket only
    picks the route."""
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.connect((target, 53))
            return s.getsockname()[0]
    except OSError:
        return None


def _windows_prefixes() -> dict[str, int]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-NetIPAddress | Select IPAddress,PrefixLength | ConvertTo-Json -Compress"],
                         capture_output=True, text=True, timeout=30).stdout
    rows = json.loads(out or "[]")
    return {r["IPAddress"].split("%")[0]: int(r["PrefixLength"]) for r in ([rows] if isinstance(rows, dict) else rows)}


def _unix_prefixes() -> dict[str, int]:
    p = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True, text=True, timeout=10)
    if p.returncode == 0:
        return {a["local"]: int(a["prefixlen"]) for link in json.loads(p.stdout or "[]")
                for a in link.get("addr_info", []) if "local" in a}
    out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=10).stdout  # macOS, BSD
    found = {}
    for ip, mask in re.findall(r"inet (\S+) netmask (0x[0-9a-f]+)", out):
        found[ip] = bin(int(mask, 16)).count("1")
    for ip, plen in re.findall(r"inet6 (\S+?)(?:%\S+)? prefixlen (\d+)", out):
        found[ip] = int(plen)
    return found


def prefixes() -> dict[str, int]:
    """Every address of this machine's interfaces and its prefix length, where the system tells."""
    try:
        return _windows_prefixes() if IS_WINDOWS else _unix_prefixes()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {}


def local_addresses() -> dict:
    """{"public": [ip...], "lan": [(ip, net)...]}: the addresses this machine goes out from."""
    found = {"public": [], "lan": []}
    known = None
    for family, target in ((socket.AF_INET, "8.8.8.8"), (socket.AF_INET6, "2001:4860:4860::8888")):
        ip = _source(family, target)
        if not ip:
            continue
        addr = ipaddress.ip_address(ip.split("%")[0])
        if addr.is_global:
            found["public"].append(str(addr))
        elif addr.version == 4 and addr.is_private and not addr.is_loopback and not addr.is_link_local:
            if known is None:
                known = prefixes()
            plen = known.get(str(addr), 24)
            found["lan"].append((str(addr), str(ipaddress.ip_network(f"{addr}/{plen}", strict=False))))
    return found


def gateway() -> str | None:
    """The default gateway's IPv4 address, for NAT-PMP."""
    try:
        if IS_WINDOWS:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort RouteMetric | Select -First 1)"
                                  ".NextHop"], capture_output=True, text=True, timeout=30).stdout.strip()
            return out or None
        try:
            with open("/proc/net/route") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if parts[1] == "00000000" and int(parts[3], 16) & 2:
                        return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
        except OSError:
            pass
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"gateway: (\S+)", out)
        return m.group(1) if m else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


# --- port mappings on the router ------------------------------------------------------------------------------

SSDP = ("239.255.255.250", 1900)
IGD = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
       "urn:schemas-upnp-org:service:WANPPPConnection:1")


def _ssdp(lan_ip: str, wait: float = 2) -> list[str]:
    """The description URLs of the internet gateways that answer on this network."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.bind((lan_ip, 0))
        s.settimeout(0.5)
        s.sendto(msg, SSDP)
        end = time.time() + wait
        while time.time() < end:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m and m.group(1).decode() not in found:
                found.append(m.group(1).decode())
    return found


def _control(location: str) -> tuple[str, str] | None:
    """(control URL, service type) of a gateway's WAN connection service."""
    with urllib.request.urlopen(location, timeout=5) as r:
        root = ElementTree.fromstring(r.read())
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    base = location.split("/", 3)
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        kind = svc.findtext("d:serviceType", namespaces=ns)
        url = svc.findtext("d:controlURL", namespaces=ns)
        if kind in IGD and url:
            return (url if url.startswith("http") else f"{base[0]}//{base[2]}/{url.lstrip('/')}"), kind
    return None


def _soap(url: str, kind: str, action: str, args: dict) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{kind}">{body}</u:{action}></s:Body></s:Envelope>')
    req = urllib.request.Request(url, data=envelope.encode(), headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{kind}#{action}"'})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def upnp_map(lan_ip: str, port: int) -> str | None:
    """Map `port` on the router to this machine's, through UPnP IGD; returns the public "ip:port", or None."""
    for location in _ssdp(lan_ip):
        try:
            found = _control(location)
            if not found:
                continue
            url, kind = found
            ext = re.search(r"<NewExternalIPAddress>([^<]+)<", _soap(url, kind, "GetExternalIPAddress", {}))
            if not ext or not ipaddress.ip_address(ext.group(1)).is_global:
                continue  # behind another NAT: a mapping here doesn't make it reachable
            _soap(url, kind, "AddPortMapping", {
                "NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "TCP", "NewInternalPort": port,
                "NewInternalClient": lan_ip, "NewEnabled": 1, "NewPortMappingDescription": "legion radicle",
                "NewLeaseDuration": LEASE})
            return f"{ext.group(1)}:{port}"
        except (OSError, ValueError, ElementTree.ParseError):
            continue
    return None


def natpmp_map(gw: str, port: int) -> str | None:
    """Map `port` through NAT-PMP (Apple's routers, and others); returns the public "ip:port", or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.sendto(b"\x00\x00", (gw, 5351))
            data = s.recv(16)
            if len(data) < 12 or data[1] != 128 or struct.unpack("!H", data[2:4])[0] != 0:
                return None
            ext = socket.inet_ntoa(data[8:12])
            if not ipaddress.ip_address(ext).is_global:
                return None
            s.sendto(struct.pack("!BBHHHI", 0, 2, 0, port, port, LEASE), (gw, 5351))
            data = s.recv(16)
            if len(data) < 16 or data[1] != 130 or struct.unpack("!H", data[2:4])[0] != 0:
                return None
            return f"{ext}:{struct.unpack('!H', data[10:12])[0]}"
    except OSError:
        return None


def mapped(lan_ip: str, port: int) -> str | None:
    gw = gateway()
    return (natpmp_map(gw, port) if gw else None) or upnp_map(lan_ip, port)


# --- does it answer? ------------------------------------------------------------------------------------------


def split(addr: str) -> tuple[str, int]:
    host, port = addr.rsplit(":", 1)
    return host.strip("[]"), int(port)


def answers(addr: str, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection(split(addr), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def probe(addrs: list[str]) -> dict[str, bool]:
    """Which of these addresses answer, tried at once."""
    if not addrs:
        return {}
    with ThreadPoolExecutor(max_workers=min(16, len(addrs))) as pool:
        return dict(zip(addrs, pool.map(answers, addrs)))


def joined(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
