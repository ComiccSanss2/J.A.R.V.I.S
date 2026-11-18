"""Self-signed HTTPS certificate for LAN use.

Browsers only allow the microphone and speech recognition in a "secure context"
(https:// or http://localhost). To use J.A.R.V.I.S. from a second device
(e.g. the Mac connecting to the PC) you therefore need HTTPS: this module generates it.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packet is sent: only used to discover the LAN IP
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


def ensure_cert(cert_dir: Path) -> tuple[Path, Path]:
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = cert_dir / "jarvis-cert.pem", cert_dir / "jarvis-key.pem"
    if cert_file.exists() and key_file.exists():
        return cert_file, key_file

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host = socket.gethostname()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "J.A.R.V.I.S. OS")])
    san = [x509.DNSName("localhost"), x509.DNSName(host), x509.DNSName(f"{host}.local")]
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in local_ips()]
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file
