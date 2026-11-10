"""Local TLS certificate for the opt-in LAN UI.

The key stays in the state directory, mode 0600. A certificate already present
at ui/tls/cert.pem and ui/tls/key.pem is used as-is, which is how mkcert is
installed. Otherwise OpenSSL generates a self-signed certificate. The
fingerprint is a SHA-256 of the certificate, not a secret, and is printed so
the user can compare it in the browser.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import assert_private, secure_mkdir


def fingerprint(pem: bytes) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem.decode("utf-8"))
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def ensure_certificate(state_dir: Path) -> tuple[Path, Path, str]:
    directory = state_dir / "ui" / "tls"
    secure_mkdir(directory)
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    if cert.is_file() and key.is_file():
        assert_private(key)
        os.chmod(cert, 0o600)
        digest = fingerprint(cert.read_bytes())
        _write_fingerprint(directory, digest)
        return cert, key, digest
    if shutil.which("openssl") is None:
        raise SsiosError(
            "tls_unavailable",
            "openssl is missing. Place a mkcert certificate and key at ui/tls/cert.pem and ui/tls/key.pem in the state directory.",
            500,
        )
    host = socket.gethostname().split(".")[0].strip() or "ssios"
    safe_host = "".join(char for char in host if char.isalnum() or char in "-") or "ssios"
    config_path = directory / "openssl.cnf"
    config_path.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name=req_distinguished_name",
                "x509_extensions=v3_req",
                "prompt=no",
                "[req_distinguished_name]",
                "CN=ssios.local",
                "[v3_req]",
                f"subjectAltName=DNS:localhost,DNS:{safe_host},DNS:{safe_host}.local,IP:127.0.0.1",
                "keyUsage=digitalSignature,keyEncipherment",
                "extendedKeyUsage=serverAuth",
                "basicConstraints=CA:FALSE",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    completed = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "825",
            "-nodes",
            "-config",
            str(config_path),
        ],
        check=False,
        capture_output=True,
    )
    config_path.unlink(missing_ok=True)
    if completed.returncode != 0 or not cert.is_file() or not key.is_file():
        cert.unlink(missing_ok=True)
        key.unlink(missing_ok=True)
        raise SsiosError("tls_unavailable", "openssl could not write a local certificate", 500)
    os.chmod(key, 0o600)
    os.chmod(cert, 0o600)
    digest = fingerprint(cert.read_bytes())
    _write_fingerprint(directory, digest)
    return cert, key, digest


def _write_fingerprint(directory: Path, digest: str) -> None:
    path = directory / "fingerprint.txt"
    path.write_text(digest + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
