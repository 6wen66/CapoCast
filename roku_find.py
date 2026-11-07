"""Find Rokus on the local network with SSDP (the same search the Roku phone app does), so the IP needn't be typed.

`parse_reply` is pure and tested; `search` does the network part.
"""
import re
import socket
import time

SSDP_ADDR = ("239.255.255.250", 1900)
REQUEST = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: roku:ecp\r\n\r\n").encode()


def parse_reply(data):
    """The Roku's IP address from one SSDP reply, or None if it isn't a Roku's answer."""
    try:
        text = data.decode("ascii", "replace")
    except Exception:
        return None
    if "roku:ecp" not in text.lower():
        return None
    m = re.search(r"^location:\s*https?://(\d{1,3}(?:\.\d{1,3}){3})[:/]", text, re.I | re.M)
    if not m:
        return None
    ip = m.group(1)
    return ip if all(0 <= int(part) <= 255 for part in ip.split(".")) else None


def search(timeout=2.5):
    """IP addresses of the Rokus that answered within `timeout` seconds, in the order they answered."""
    found = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:                                 # no network adapter at all
        return found
    try:
        sock.settimeout(0.4)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        for _ in range(2):                          # UDP can be lost; ask twice
            sock.sendto(REQUEST, SSDP_ADDR)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            ip = parse_reply(data)
            if ip and ip not in found:
                found.append(ip)
    except OSError:
        pass
    finally:
        sock.close()
    return found
