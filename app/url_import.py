"""Fetch media over HTTPS from configured CDN origins, pinning each TLS connection."""
import http.client
import ipaddress
import os
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, ServiceUnavailable


DEFAULT_HOSTS = "cdn.discordapp.com,media.discordapp.net,cdn.steamusercontent.com"


def open_origin(url, remaining):
    allowed = {h.strip().lower() for h in os.getenv("URL_IMPORT_HOSTS", DEFAULT_HOSTS).split(",")}
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme != "https" or not host or host not in allowed
                or parts.username is not None or parts.password is not None or parts.port not in (None, 443)
                or "\\" in url or any(ord(c) < 32 for c in url)):
            raise ValueError()
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(entry[4][0]) for entry in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
            raise ValueError()
    except (ValueError, UnicodeError, OSError):
        raise BadRequest("URL imports require an approved public HTTPS media host.")

    connection = http.client.HTTPSConnection(host, timeout=min(10, remaining))
    try:
        with socket.create_connection((str(ips[0]), 443), timeout=min(10, remaining)) as raw:
            connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        connection.sock.settimeout(min(15, remaining))
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        connection.request("GET", path, headers={"Accept-Encoding": "identity"})
        return connection, connection.getresponse()
    except BaseException:
        connection.close()
        raise


def download_url(url, destination, max_bytes):
    deadline = time.monotonic() + 180
    try:
        for _ in range(5):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            conn, response = open_origin(url, remaining)
            try:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise BadRequest("The import host returned an incomplete redirect.")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise BadRequest(f"The import host returned HTTP {response.status}.")
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise RequestEntityTooLarge("Remote video exceeds the server upload limit.")
                received = 0
                with open(destination, "wb") as output:
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError()
                        chunk = response.read1(128 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > max_bytes:
                            raise RequestEntityTooLarge("Remote video exceeds the server upload limit.")
                        output.write(chunk)
                if not received or (length is not None and received != int(length)):
                    raise BadRequest("The import host returned an incomplete video.")
                return
            finally:
                conn.close()
        raise BadRequest("The import host redirected too many times.")
    except (OSError, http.client.HTTPException, UnicodeError, ValueError) as exc:
        raise ServiceUnavailable("The import host could not deliver the file securely. Upload the file directly.") from exc
