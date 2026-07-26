"""Password-authenticated SSH transport backed by Paramiko.

This module is intentionally imported only when ``VB_SSH_TRANSPORT=paramiko``
is selected.  The existing OpenSSH transport remains the default and does not
gain a Paramiko dependency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from virtuoso_bridge.runtime_paths import log_dir
from virtuoso_bridge.transport.ssh import (
    CommandResult,
    SSHRunner,
    _setup_command_log,
    _windows_no_window_kwargs,
)

try:
    import paramiko
except ModuleNotFoundError:  # pragma: no cover - exercised through the error path
    paramiko = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

_HELPER_FLAG = "--paramiko-tunnel-helper"
_HELPER_ENV_PREFIX = "_VB_PARAMIKO_TUNNEL_"


def _require_paramiko() -> Any:
    if paramiko is None:
        raise RuntimeError(
            "Paramiko password transport requires the optional dependency. "
            'Install it with: uv pip install -e ".[paramiko]"'
        )
    return paramiko


def host_key_sha256(key: Any) -> str:
    """Return an OpenSSH-style SHA256 fingerprint for a Paramiko key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _normalize_fingerprint(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if not text.startswith("SHA256:"):
        text = "SHA256:" + text
    return text.rstrip("=")


class _PinnedHostKeyPolicy(
    (_require_paramiko().MissingHostKeyPolicy if paramiko is not None else object)
):
    """Accept exactly one explicitly pinned host-key fingerprint."""

    def __init__(self, expected: str) -> None:
        self.expected = _normalize_fingerprint(expected)

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        actual = host_key_sha256(key)
        if not hmac.compare_digest(actual, self.expected):
            raise _require_paramiko().SSHException(
                "SSH host key fingerprint mismatch; verify "
                "VB_SSH_HOST_KEY_SHA256 before connecting"
            )
        client.get_host_keys().add(hostname, key.get_name(), key)


def _remote_path(value: str) -> str:
    return str(PurePosixPath(value.replace("\\", "/")))


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text or exc.__class__.__name__


class _ChannelWriter:
    """Minimal streaming file object that writes a tar archive to a channel."""

    def __init__(self, channel: Any) -> None:
        self.channel = channel
        self.position = 0

    def write(self, data: bytes) -> int:
        self.channel.sendall(data)
        self.position += len(data)
        return len(data)

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        return None


class ParamikoPasswordTransport(SSHRunner):
    """Drop-in ``SSHRunner`` alternative using password authentication.

    One Paramiko connection is reused for command and exec channels.  Port
    forwarding is hosted by a detached helper process so a tunnel started by
    the CLI survives after the parent command exits, matching OpenSSH mode.
    """

    def __init__(
        self,
        host: str,
        user: str | None,
        password: str,
        *,
        ssh_port: int = 22,
        host_key_sha256_fingerprint: str | None = None,
        timeout: int = 600,
        connect_timeout: int = 30,
        verbose: bool = False,
        profile: str | None = None,
    ) -> None:
        _require_paramiko()
        if not password:
            raise ValueError("password must be non-empty")
        if not 1 <= int(ssh_port) <= 65535:
            raise ValueError("ssh_port must be between 1 and 65535")

        _setup_command_log()
        self._host = host
        self._user = user
        self._password = password
        self._ssh_port = int(ssh_port)
        self._host_key_sha256 = _normalize_fingerprint(
            host_key_sha256_fingerprint or ""
        )
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._verbose = verbose
        self._profile = profile
        self._persistent_shell_enabled = False
        self._use_control_master = False
        self._client: Any = None
        self._connect_lock = threading.RLock()

        # Keep the same tunnel state attributes/properties as SSHRunner.
        self._tunnel_proc: subprocess.Popen[Any] | None = None
        self._tunnel_pid: int | None = None
        self._tunnel_using_external = False

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(host={self._host!r}, "
            f"user={self._user!r}, ssh_port={self._ssh_port!r})"
        )

    def _new_client(self) -> Any:
        pm = _require_paramiko()
        client = pm.SSHClient()
        if self._host_key_sha256:
            client.set_missing_host_key_policy(
                _PinnedHostKeyPolicy(self._host_key_sha256)
            )
        else:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(pm.RejectPolicy())

        client.connect(
            hostname=self._host,
            port=self._ssh_port,
            username=self._user,
            password=self._password,
            timeout=self._connect_timeout,
            banner_timeout=self._connect_timeout,
            auth_timeout=self._connect_timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        transport = client.get_transport()
        if transport is None or not transport.is_authenticated():
            client.close()
            raise pm.SSHException("SSH transport did not authenticate")
        if self._host_key_sha256:
            actual = host_key_sha256(transport.get_remote_server_key())
            if not hmac.compare_digest(actual, self._host_key_sha256):
                client.close()
                raise pm.SSHException(
                    "SSH host key fingerprint mismatch; verify "
                    "VB_SSH_HOST_KEY_SHA256 before connecting"
                )
        transport.set_keepalive(30)
        return client

    def _get_client(self) -> Any:
        with self._connect_lock:
            if self._client is not None:
                transport = self._client.get_transport()
                if (
                    transport is not None
                    and transport.is_active()
                    and transport.is_authenticated()
                ):
                    return self._client
                self._client.close()
                self._client = None
            self._client = self._new_client()
            logger.info(
                "Paramiko password connection established to %s:%d",
                self._host,
                self._ssh_port,
            )
            return self._client

    def _drop_client(self) -> None:
        with self._connect_lock:
            if self._client is not None:
                try:
                    self._client.close()
                finally:
                    self._client = None

    def close(self) -> None:
        """Close command channels without stopping the detached tunnel."""
        self._drop_client()

    def ensure_persistent_shell(
        self,
        timeout: float | None = None,
        *,
        _budget: Any = None,
    ) -> None:
        """No-op: Paramiko already multiplexes channels on one connection."""

    def test_connection(self, timeout: float | None = None) -> bool:
        try:
            result = self.run_command(":", timeout=timeout or self._connect_timeout)
        except Exception as exc:
            logger.warning(
                "Paramiko SSH connection to %s failed: %s",
                self._host,
                _safe_error(exc),
            )
            return False
        return result.returncode == 0

    def run_command(
        self,
        command: str,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run *command* through a POSIX login shell over a Paramiko channel."""
        effective_timeout = float(timeout if timeout is not None else self._timeout)
        logger.info("[server] %s", command)
        if self._verbose:
            print(
                f"[paramiko] {self._user or ''}@{self._host}:{self._ssh_port} sh -l",
                flush=True,
            )

        client = self._get_client()
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("Paramiko SSH transport is unavailable")

        channel = transport.open_session(timeout=effective_timeout)
        channel.settimeout(effective_timeout)
        marker = f"__VB_COMMAND_{uuid.uuid4().hex}__"
        try:
            channel.exec_command("sh -l")
            payload = (
                f"printf '\\n%s\\n' {shlex.quote(marker)}\n{command}"
            ).encode("utf-8")
            if not payload.endswith(b"\n"):
                payload += b"\n"
            channel.sendall(payload)
            channel.shutdown_write()
            returncode, stdout_bytes, stderr_bytes = self._collect_channel(
                channel,
                effective_timeout,
                ["paramiko", self._host, "sh", "-l"],
            )
        except (EOFError, OSError):
            self._drop_client()
            raise
        finally:
            channel.close()

        marker_bytes = ("\n" + marker + "\n").encode("utf-8")
        marker_index = stdout_bytes.find(marker_bytes)
        if marker_index >= 0:
            stdout_bytes = stdout_bytes[marker_index + len(marker_bytes) :]
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        logger.debug(
            "Remote Paramiko command returned %d "
            "(stdout=%d bytes, stderr=%d bytes)",
            returncode,
            len(stdout),
            len(stderr),
        )
        return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    @staticmethod
    def _collect_channel(
        channel: Any,
        timeout: float,
        command: object,
    ) -> tuple[int, bytes, bytes]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        started = time.monotonic()
        while True:
            while channel.recv_ready():
                stdout_chunks.append(channel.recv(65536))
            while channel.recv_stderr_ready():
                stderr_chunks.append(channel.recv_stderr(65536))
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            if time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.01)
        return (
            channel.recv_exit_status(),
            b"".join(stdout_chunks),
            b"".join(stderr_chunks),
        )

    def _open_exec_channel(self, command: str, timeout: float) -> Any:
        client = self._get_client()
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("Paramiko SSH transport is unavailable")
        channel = transport.open_session(timeout=timeout)
        channel.settimeout(timeout)
        channel.exec_command(command)
        return channel

    def _exec_bytes(
        self,
        command: str,
        payload: bytes,
        timeout: float,
    ) -> CommandResult:
        channel = self._open_exec_channel(command, timeout)
        try:
            channel.sendall(payload)
            channel.shutdown_write()
            rc, stdout, stderr = self._collect_channel(
                channel,
                timeout,
                command,
            )
        finally:
            channel.close()
        return CommandResult(
            rc,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _tar_basename(remote_path: str) -> str:
        basename = PurePosixPath(_remote_path(remote_path).rstrip("/")).name
        if not basename or basename in (".", ".."):
            raise ValueError(f"Invalid remote path: {remote_path}")
        return basename

    def _upload_tar_entries(
        self,
        entries: list[tuple[Path, str]],
        remote_dir: str,
        timeout: float,
    ) -> CommandResult:
        remote_dir = _remote_path(remote_dir)
        command = (
            "sh -c "
            + shlex.quote(
                f"mkdir -p {shlex.quote(remote_dir)} && "
                f"chmod 755 {shlex.quote(remote_dir)} && "
                f"tar xf - -C {shlex.quote(remote_dir)}"
            )
        )
        channel = self._open_exec_channel(command, timeout)
        try:
            writer = _ChannelWriter(channel)
            with tarfile.open(fileobj=writer, mode="w|") as archive:
                for local_path, remote_name in entries:
                    archive.add(
                        local_path,
                        arcname=remote_name,
                        recursive=True,
                    )
            channel.shutdown_write()
            rc, stdout, stderr = self._collect_channel(
                channel,
                timeout,
                command,
            )
        finally:
            channel.close()
        return CommandResult(
            rc,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        recursive: bool = False,
        timeout: float | None = None,
    ) -> CommandResult:
        if not local_path.exists():
            raise FileNotFoundError(f"Local path not found: {local_path}")
        effective_timeout = float(timeout if timeout is not None else self._timeout)
        remote_path = _remote_path(remote_path)
        try:
            result = self._upload_tar_entries(
                [(local_path, self._tar_basename(remote_path))],
                str(PurePosixPath(remote_path).parent),
                effective_timeout,
            )
        except socket.timeout as exc:
            raise subprocess.TimeoutExpired(
                ["paramiko-tar", "upload", str(local_path)],
                effective_timeout,
            ) from exc
        except Exception as exc:
            return CommandResult(
                1,
                "",
                f"Paramiko upload failed: {_safe_error(exc)}",
            )
        return result

    def upload_batch(
        self,
        files: list[tuple[Path, str]],
        timeout: float | None = None,
    ) -> CommandResult:
        for local_path, _ in files:
            if not local_path.exists():
                raise FileNotFoundError(f"Local path not found: {local_path}")
        if not files:
            return CommandResult(0, "", "")
        effective_timeout = float(timeout if timeout is not None else self._timeout)
        try:
            by_remote_dir: dict[str, list[tuple[Path, str]]] = {}
            for local_path, remote_path in files:
                normalized = _remote_path(remote_path)
                remote_dir = str(PurePosixPath(normalized).parent)
                by_remote_dir.setdefault(remote_dir, []).append(
                    (local_path, self._tar_basename(normalized))
                )
            for remote_dir, entries in by_remote_dir.items():
                result = self._upload_tar_entries(
                    entries,
                    remote_dir,
                    effective_timeout,
                )
                if result.returncode != 0:
                    return result
        except socket.timeout as exc:
            raise subprocess.TimeoutExpired(
                ["paramiko-tar", "upload-batch"],
                effective_timeout,
            ) from exc
        except Exception as exc:
            return CommandResult(
                1,
                "",
                f"Paramiko batch upload failed: {_safe_error(exc)}",
            )
        return CommandResult(0, "", "")

    def upload_text(
        self,
        text: str,
        remote_path: str,
        timeout: float | None = None,
    ) -> CommandResult:
        remote_path = _remote_path(remote_path)
        effective_timeout = float(timeout if timeout is not None else self._timeout)
        remote_dir = str(PurePosixPath(remote_path).parent)
        command = (
            "sh -c "
            + shlex.quote(
                f"mkdir -p {shlex.quote(remote_dir)} && "
                f"chmod 755 {shlex.quote(remote_dir)} && "
                f"cat > {shlex.quote(remote_path)}"
            )
        )
        try:
            return self._exec_bytes(
                command,
                text.encode("utf-8"),
                effective_timeout,
            )
        except socket.timeout as exc:
            raise subprocess.TimeoutExpired(
                ["paramiko", "upload-text", remote_path],
                effective_timeout,
            ) from exc
        except Exception as exc:
            return CommandResult(
                1,
                "",
                f"Paramiko text upload failed: {_safe_error(exc)}",
            )

    def _receive_framed_payload(
        self,
        command: str,
        marker: str,
        destination: BinaryIO,
        timeout: float,
    ) -> tuple[int, str]:
        channel = self._open_exec_channel(command, timeout)
        marker_bytes = ("\n" + marker + "\n").encode("utf-8")
        pending = b""
        marker_found = False
        stderr_chunks: list[bytes] = []
        started = time.monotonic()
        try:
            channel.shutdown_write()
            while True:
                while channel.recv_ready():
                    chunk = channel.recv(65536)
                    if marker_found:
                        destination.write(chunk)
                    else:
                        pending += chunk
                        index = pending.find(marker_bytes)
                        if index >= 0:
                            marker_found = True
                            destination.write(
                                pending[index + len(marker_bytes) :]
                            )
                            pending = b""
                        elif len(pending) > len(marker_bytes) * 4:
                            pending = pending[-len(marker_bytes) * 2 :]
                while channel.recv_stderr_ready():
                    stderr_chunks.append(channel.recv_stderr(65536))
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                if time.monotonic() - started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.01)
            returncode = channel.recv_exit_status()
        finally:
            channel.close()
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        if not marker_found:
            detail = stderr or pending.decode("utf-8", errors="replace")
            raise RuntimeError(
                "Remote download stream did not contain its payload marker"
                + (f": {detail.strip()}" if detail.strip() else "")
            )
        return returncode, stderr

    @staticmethod
    def _safe_extract_archive(archive_path: Path, destination: Path) -> None:
        root = destination.resolve()
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                member_path = (destination / member.name).resolve()
                if os.path.commonpath([root, member_path]) != str(root):
                    raise RuntimeError(
                        f"Unsafe path in downloaded archive: {member.name}"
                    )
                if member.issym():
                    link_path = (member_path.parent / member.linkname).resolve()
                    if os.path.commonpath([root, link_path]) != str(root):
                        raise RuntimeError(
                            f"Unsafe symlink in downloaded archive: {member.name}"
                        )
                elif member.islnk():
                    link_path = (destination / member.linkname).resolve()
                    if os.path.commonpath([root, link_path]) != str(root):
                        raise RuntimeError(
                            f"Unsafe hard link in downloaded archive: {member.name}"
                        )
            archive.extractall(destination)

    @staticmethod
    def _replace_download(staged: Path, target: Path) -> None:
        backup: Path | None = None
        try:
            if target.exists() or target.is_symlink():
                backup = target.parent / f".vbbak-{uuid.uuid4().hex}"
                target.rename(backup)
            staged.rename(target)
        except Exception:
            if (
                backup is not None
                and not (target.exists() or target.is_symlink())
                and (backup.exists() or backup.is_symlink())
            ):
                backup.rename(target)
            raise
        else:
            if backup is not None:
                if backup.is_dir() and not backup.is_symlink():
                    shutil.rmtree(backup)
                else:
                    backup.unlink(missing_ok=True)

    def download(
        self,
        remote_path: str,
        local_path: Path,
        recursive: bool = False,
        timeout: float | None = None,
    ) -> CommandResult:
        remote_path = _remote_path(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        effective_timeout = float(timeout if timeout is not None else self._timeout)
        token = uuid.uuid4().hex
        marker = f"__VB_PAYLOAD_{token}__"
        staged = local_path.parent / f".vbtmp-{token}"
        archive_path = local_path.parent / f".vbtmp-{token}.tar.gz"
        try:
            marker_command = f"printf '\\n%s\\n' {shlex.quote(marker)}"
            if recursive:
                remote_basename = self._tar_basename(remote_path)
                remote_parent = str(PurePosixPath(remote_path).parent)
                payload_command = (
                    f"cd {shlex.quote(remote_parent)} && "
                    f"tar czf - {shlex.quote(remote_basename)}"
                )
                command = "sh -c " + shlex.quote(
                    f"{marker_command}; {payload_command}"
                )
                with archive_path.open("wb") as destination:
                    rc, stderr = self._receive_framed_payload(
                        command,
                        marker,
                        destination,
                        effective_timeout,
                    )
                if rc != 0:
                    archive_path.unlink(missing_ok=True)
                    return CommandResult(rc, "", stderr)
                staged.mkdir(parents=True)
                self._safe_extract_archive(archive_path, staged)
                extracted = staged / remote_basename
                if not (extracted.exists() or extracted.is_symlink()):
                    raise RuntimeError(
                        "Downloaded archive did not contain expected directory: "
                        f"{remote_basename}"
                    )
                self._replace_download(extracted, local_path)
                shutil.rmtree(staged, ignore_errors=True)
            else:
                command = "sh -c " + shlex.quote(
                    f"{marker_command}; cat {shlex.quote(remote_path)}"
                )
                with staged.open("wb") as destination:
                    rc, stderr = self._receive_framed_payload(
                        command,
                        marker,
                        destination,
                        effective_timeout,
                    )
                if rc != 0:
                    staged.unlink(missing_ok=True)
                    return CommandResult(rc, "", stderr)
                self._replace_download(staged, local_path)
        except socket.timeout as exc:
            if staged.is_dir():
                shutil.rmtree(staged, ignore_errors=True)
            else:
                staged.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)
            raise subprocess.TimeoutExpired(
                ["paramiko", "download", remote_path],
                effective_timeout,
            ) from exc
        except Exception as exc:
            if staged.is_dir():
                shutil.rmtree(staged, ignore_errors=True)
            else:
                staged.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)
            return CommandResult(
                1,
                "",
                f"Paramiko download failed: {_safe_error(exc)}",
            )
        archive_path.unlink(missing_ok=True)
        return CommandResult(0, "", "")

    def start_port_forward(
        self,
        port: int,
        settle: float = 1.5,
        *,
        remote_port: int | None = None,
    ) -> subprocess.Popen[Any] | None:
        """Start a detached Paramiko helper forwarding localhost TCP."""
        if remote_port is None:
            remote_port = port
        if self.can_reach_port(port):
            self._tunnel_using_external = True
            return None

        logs = log_dir()
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / f"paramiko-tunnel-{port}.log"
        pid_path = logs / f"paramiko-tunnel-{port}-{uuid.uuid4().hex}.pid"
        helper_env = os.environ.copy()
        values = {
            "HOST": self._host,
            "USER": self._user or "",
            "PASSWORD": self._password,
            "SSH_PORT": str(self._ssh_port),
            "HOST_KEY_SHA256": self._host_key_sha256,
            "CONNECT_TIMEOUT": str(self._connect_timeout),
            "LOCAL_PORT": str(port),
            "REMOTE_PORT": str(remote_port),
            "PID_FILE": str(pid_path),
        }
        for key, value in values.items():
            helper_env[_HELPER_ENV_PREFIX + key] = value

        cmd = [
            sys.executable,
            "-m",
            "virtuoso_bridge.transport.paramiko_password",
            _HELPER_FLAG,
        ]
        logger.info(
            "Starting Paramiko tunnel helper: localhost:%d -> %s:%d",
            port,
            self._host,
            remote_port,
        )
        log_handle = log_path.open("ab")
        try:
            popen_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "env": helper_env,
            }
            if os.name == "nt":
                popen_kwargs.update(
                    _windows_no_window_kwargs(
                        detached=True,
                        new_process_group=True,
                    )
                )
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)
        finally:
            log_handle.close()

        deadline = time.monotonic() + max(
            settle,
            min(float(self._connect_timeout) + 2.0, 60.0),
        )
        while time.monotonic() < deadline:
            if self.can_reach_port(port):
                try:
                    helper_pid = int(
                        pid_path.read_text(encoding="ascii").strip()
                    )
                except (OSError, ValueError):
                    # The listener may become reachable just before the helper
                    # publishes its PID.  Keep waiting instead of recording the
                    # Windows venv launcher PID.
                    time.sleep(0.05)
                    continue
                pid_path.unlink(missing_ok=True)
                self._tunnel_pid = helper_pid
                if helper_pid == proc.pid:
                    self._tunnel_proc = proc
                    self._tunnel_using_external = False
                else:
                    # On Windows, a venv python.exe launcher may remain as the
                    # parent of the real interpreter.  State and stop() must
                    # target the child that owns the listening socket.
                    self._tunnel_proc = None
                    self._tunnel_using_external = True
                return proc
            if proc.poll() is not None:
                break
            time.sleep(0.1)

        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        pid_path.unlink(missing_ok=True)
        detail = ""
        try:
            lines = log_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
            if lines:
                detail = ": " + lines[-1]
        except OSError:
            pass
        raise RuntimeError(f"Paramiko tunnel helper failed to start{detail}")


def _helper_setting(name: str, *, required: bool = True) -> str:
    key = _HELPER_ENV_PREFIX + name
    value = os.environ.pop(key, "")
    if required and not value:
        raise RuntimeError(f"Missing internal tunnel helper setting: {name}")
    return value


def _relay(
    client_socket: socket.socket,
    ssh_transport: Any,
    remote_port: int,
) -> None:
    channel: Any = None
    try:
        origin = client_socket.getpeername()
        channel = ssh_transport.open_channel(
            "direct-tcpip",
            ("127.0.0.1", remote_port),
            origin,
        )
        if channel is None:
            return
        client_socket.settimeout(0.5)
        channel.settimeout(0.5)
        client_read_open = True
        channel_read_open = True
        while client_read_open or channel_read_open:
            if client_read_open:
                try:
                    data = client_socket.recv(65536)
                except socket.timeout:
                    pass
                else:
                    if data:
                        channel.sendall(data)
                    else:
                        # VirtuosoClient half-closes its write side after the
                        # JSON request.  Propagate EOF to the remote daemon but
                        # keep relaying the daemon's response back to the client.
                        client_read_open = False
                        channel.shutdown_write()

            if channel_read_open:
                try:
                    data = channel.recv(65536)
                except socket.timeout:
                    pass
                else:
                    if data:
                        client_socket.sendall(data)
                    else:
                        channel_read_open = False
                        try:
                            client_socket.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
    except (EOFError, OSError):
        logger.debug("Paramiko tunnel relay closed", exc_info=True)
    finally:
        if channel is not None:
            channel.close()
        client_socket.close()


def _serve_tunnel_helper() -> int:
    host = _helper_setting("HOST")
    user = _helper_setting("USER", required=False) or None
    password = _helper_setting("PASSWORD")
    ssh_port = int(_helper_setting("SSH_PORT"))
    fingerprint = _helper_setting("HOST_KEY_SHA256", required=False) or None
    connect_timeout = int(_helper_setting("CONNECT_TIMEOUT"))
    local_port = int(_helper_setting("LOCAL_PORT"))
    remote_port = int(_helper_setting("REMOTE_PORT"))
    pid_file = Path(_helper_setting("PID_FILE"))

    runner = ParamikoPasswordTransport(
        host=host,
        user=user,
        password=password,
        ssh_port=ssh_port,
        host_key_sha256_fingerprint=fingerprint,
        connect_timeout=connect_timeout,
    )
    # Drop the final local reference as soon as the runner has copied it.
    password = ""
    client = runner._get_client()
    runner._password = ""
    ssh_transport = client.get_transport()
    if ssh_transport is None:
        raise RuntimeError("Paramiko tunnel transport is unavailable")

    stop = threading.Event()

    def _stop(_signum: int, _frame: Any) -> None:
        stop.set()

    if os.name != "nt":
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", local_port))
    listener.listen(32)
    listener.settimeout(1.0)
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    logger.info(
        "Paramiko tunnel listening on localhost:%d -> %s:localhost:%d",
        local_port,
        host,
        remote_port,
    )
    try:
        while not stop.is_set() and ssh_transport.is_active():
            try:
                incoming, _ = listener.accept()
            except socket.timeout:
                continue
            thread = threading.Thread(
                target=_relay,
                args=(incoming, ssh_transport, remote_port),
                daemon=True,
            )
            thread.start()
    finally:
        listener.close()
        runner.close()
    return 0


def _main() -> int:
    if sys.argv[1:] != [_HELPER_FLAG]:
        print("This module is an internal Paramiko tunnel helper.", file=sys.stderr)
        return 2
    try:
        return _serve_tunnel_helper()
    except Exception as exc:
        logger.exception("Paramiko tunnel helper failed")
        print(f"Paramiko tunnel helper failed: {_safe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by integration tests
    raise SystemExit(_main())
