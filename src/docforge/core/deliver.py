import base64
import ipaddress
import os
import posixpath
import smtplib
import socket
from email.message import EmailMessage
from io import BytesIO

import paramiko


class DeliveryError(Exception):
    """Delivery target rejected or unreachable. The message is safe to show to the customer."""


def email(to: list[str], subject: str, body: str, filename: str, pdf: bytes) -> None:
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "reports@docforge.local")
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)
    host, port = os.environ.get("SMTP_HOST", "localhost"), int(os.environ.get("SMTP_PORT", "25"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if os.environ.get("SMTP_STARTTLS") == "1":
                smtp.starttls()
            if os.environ.get("SMTP_USER"):
                smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(msg)
    except smtplib.SMTPRecipientsRefused as e:
        raise DeliveryError("email recipients were refused") from e


def check_public_host(host: str) -> None:
    """Blocks SSRF: customers must not make us connect into our own network."""
    if os.environ.get("DELIVERY_ALLOW_PRIVATE") == "1":
        return
    try:
        addrs = {str(info[4][0]) for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as e:
        raise DeliveryError("SFTP host does not resolve") from e
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            raise DeliveryError("SFTP host must be a public address")


def sftp(host: str, port: int, username: str, password: str, host_key: str,
         directory: str, filename: str, data: bytes) -> str:
    """Upload with a pinned host key. host_key is the server's public key line: '<type> <base64>'."""
    check_public_host(host)
    try:
        key_type, key_b64 = host_key.split()[:2]
        expected = paramiko.PKey.from_type_string(key_type, base64.b64decode(key_b64))
    except (ValueError, paramiko.SSHException) as e:
        raise DeliveryError("host_key must be an SSH public key like 'ssh-ed25519 AAAA...'") from e

    remote = posixpath.join(directory or ".", filename)
    try:
        with socket.create_connection((host, port), timeout=30) as sock, paramiko.Transport(sock) as transport:
            transport.start_client(timeout=30)
            # Check the pinned key before sending any credentials.
            if transport.get_remote_server_key() != expected:
                raise DeliveryError("SFTP host key does not match")
            transport.auth_password(username, password)
            client = paramiko.SFTPClient.from_transport(transport)
            if client is None:
                raise paramiko.SSHException("could not open SFTP channel")
            with client:
                client.putfo(BytesIO(data), remote)
    except paramiko.AuthenticationException as e:
        raise DeliveryError("SFTP login failed") from e
    except (OSError, paramiko.SSHException) as e:
        raise ConnectionError(f"sftp {host}:{port} failed: {e}") from e  # transient: retried
    return remote
