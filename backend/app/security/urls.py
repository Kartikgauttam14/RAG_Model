import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    pass


def validate_ingestion_url(url: str, allowed_hosts: list[str]) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURLError("Only absolute HTTP(S) URLs are accepted")
    host = parsed.hostname.lower().rstrip(".")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs containing credentials are not accepted")
    if allowed_hosts and not any(host == item or host.endswith(f".{item}") for item in allowed_hosts):
        raise UnsafeURLError("URL host is not allow-listed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise UnsafeURLError("URL host could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if any(
            (
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            )
        ):
            raise UnsafeURLError("URL resolves to a prohibited network")
    return url
