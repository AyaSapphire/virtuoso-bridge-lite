from __future__ import annotations

from pathlib import Path
import re
import socket
import threading
from types import SimpleNamespace

import pytest

from virtuoso_bridge.transport.paramiko_password import (
    ParamikoPasswordTransport,
    _relay,
    host_key_sha256,
)
from virtuoso_bridge.transport.tunnel import SSHClient


def _runner(password: str = "test-only-password") -> ParamikoPasswordTransport:
    return ParamikoPasswordTransport(
        host="legacy.example.test",
        user="designer",
        password=password,
        ssh_port=22,
    )


def test_from_env_selects_paramiko_without_exposing_password(monkeypatch) -> None:
    monkeypatch.setattr(
        "virtuoso_bridge.transport.tunnel.load_vb_env",
        lambda: None,
    )
    monkeypatch.setenv("VB_REMOTE_HOST", "legacy.example.test")
    monkeypatch.setenv("VB_REMOTE_USER", "designer")
    monkeypatch.setenv("VB_REMOTE_PORT", "65361")
    monkeypatch.setenv("VB_LOCAL_PORT", "65362")
    monkeypatch.setenv("VB_SSH_TRANSPORT", "paramiko")
    monkeypatch.setenv("VB_REMOTE_PASSWORD", "not-for-repr")
    monkeypatch.setenv("VB_SSH_PORT", "2222")
    monkeypatch.setenv(
        "VB_SSH_HOST_KEY_SHA256",
        "SHA256:test-fingerprint",
    )

    client = SSHClient.from_env()

    assert isinstance(client.ssh_runner, ParamikoPasswordTransport)
    assert client.ssh_runner._ssh_port == 2222
    assert "not-for-repr" not in repr(client.ssh_runner)
    client.close()


def test_paramiko_selection_requires_password(monkeypatch) -> None:
    monkeypatch.setattr(
        "virtuoso_bridge.transport.tunnel.load_vb_env",
        lambda: None,
    )
    monkeypatch.setenv("VB_REMOTE_HOST", "legacy.example.test")
    monkeypatch.setenv("VB_REMOTE_USER", "designer")
    monkeypatch.setenv("VB_SSH_TRANSPORT", "paramiko")
    monkeypatch.delenv("VB_REMOTE_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="VB_REMOTE_PASSWORD"):
        SSHClient.from_env()


def test_paramiko_selection_rejects_jump_host(monkeypatch) -> None:
    monkeypatch.setattr(
        "virtuoso_bridge.transport.tunnel.load_vb_env",
        lambda: None,
    )
    monkeypatch.setenv("VB_REMOTE_HOST", "legacy.example.test")
    monkeypatch.setenv("VB_REMOTE_USER", "designer")
    monkeypatch.setenv("VB_JUMP_HOST", "jump.example.test")
    monkeypatch.setenv("VB_SSH_TRANSPORT", "paramiko")
    monkeypatch.setenv("VB_REMOTE_PASSWORD", "test-only-password")

    with pytest.raises(ValueError, match="does not currently support"):
        SSHClient.from_env()


class _FakeKey:
    def asbytes(self) -> bytes:
        return b"fake-host-key"

    def get_name(self) -> str:
        return "ssh-rsa"


class _FakeHostKeys:
    def add(self, hostname, key_name, key) -> None:
        self.added = (hostname, key_name, key)


class _FakeConnectedTransport:
    def __init__(self, key: _FakeKey) -> None:
        self.key = key
        self.keepalive = None

    def is_active(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    def get_remote_server_key(self):
        return self.key

    def set_keepalive(self, value: int) -> None:
        self.keepalive = value


class _FakeSSHClient:
    def __init__(self, key: _FakeKey) -> None:
        self.transport = _FakeConnectedTransport(key)
        self.host_keys = _FakeHostKeys()
        self.connect_kwargs = None
        self.policy = None

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def load_system_host_keys(self) -> None:
        self.loaded_system_keys = True

    def get_host_keys(self):
        return self.host_keys

    def connect(self, **kwargs) -> None:
        self.connect_kwargs = kwargs

    def get_transport(self):
        return self.transport

    def close(self) -> None:
        pass


def test_new_client_uses_password_only_and_checks_pinned_host_key(
    monkeypatch,
) -> None:
    from virtuoso_bridge.transport import paramiko_password as module

    key = _FakeKey()
    fake_client = _FakeSSHClient(key)
    monkeypatch.setattr(module.paramiko, "SSHClient", lambda: fake_client)
    fingerprint = host_key_sha256(key)
    runner = ParamikoPasswordTransport(
        host="legacy.example.test",
        user="designer",
        password="secret-value",
        ssh_port=2222,
        host_key_sha256_fingerprint=fingerprint,
    )

    assert runner._new_client() is fake_client
    assert fake_client.connect_kwargs == {
        "hostname": "legacy.example.test",
        "port": 2222,
        "username": "designer",
        "password": "secret-value",
        "timeout": 30,
        "banner_timeout": 30,
        "auth_timeout": 30,
        "allow_agent": False,
        "look_for_keys": False,
    }
    assert fake_client.transport.keepalive == 30


class _FakeCommandChannel:
    def __init__(self) -> None:
        self.command = None
        self.payload = b""
        self.stdout = [b"command-output\n"]
        self.stderr = [b"warning\n"]
        self.closed = False

    def exec_command(self, command: str) -> None:
        self.command = command

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, payload: bytes) -> None:
        self.payload += payload
        marker = re.search(rb"__VB_COMMAND_[0-9a-f]+__", payload)
        if marker is not None:
            self.stdout = [
                b"legacy login banner\n\n"
                + marker.group(0)
                + b"\ncommand-output\n"
            ]

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv(self, _size: int) -> bytes:
        return self.stdout.pop(0)

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, _size: int) -> bytes:
        return self.stderr.pop(0)

    def exit_status_ready(self) -> bool:
        return not self.stdout and not self.stderr

    def recv_exit_status(self) -> int:
        return 7

    def close(self) -> None:
        self.closed = True


def test_run_command_uses_login_shell_and_lf_payload(monkeypatch) -> None:
    runner = _runner()
    channel = _FakeCommandChannel()
    transport = SimpleNamespace(
        open_session=lambda timeout: channel,
    )
    client = SimpleNamespace(get_transport=lambda: transport)
    monkeypatch.setattr(runner, "_get_client", lambda: client)

    result = runner.run_command("printf ok", timeout=1)

    assert channel.command == "sh -l"
    assert channel.payload.endswith(b"printf ok\n")
    assert b"__VB_COMMAND_" in channel.payload
    assert channel.closed is True
    assert result.returncode == 7
    assert result.stdout == "command-output\n"
    assert result.stderr == "warning\n"


def test_upload_text_uses_exec_channel_without_sftp(
    monkeypatch,
) -> None:
    runner = _runner()
    calls = {}

    def fake_exec_bytes(command, payload, timeout):
        calls["command"] = command
        calls["payload"] = payload
        calls["timeout"] = timeout
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner, "_exec_bytes", fake_exec_bytes)

    result = runner.upload_text("payload", "/tmp/bridge/input.txt")

    assert result.returncode == 0
    assert calls["payload"] == b"payload"
    assert "mkdir -p" in calls["command"]
    assert "/tmp/bridge/input.txt" in calls["command"]


def test_detached_tunnel_helper_keeps_password_out_of_argv(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from virtuoso_bridge.transport import paramiko_password as module

    runner = _runner(password="child-env-secret")
    monkeypatch.setattr(module, "log_dir", lambda: tmp_path)
    reachability = iter([False, True])
    monkeypatch.setattr(
        runner,
        "can_reach_port",
        lambda _port: next(reachability),
    )
    captured = {}

    class _FakeProcess:
        pid = 4321
        stderr = None

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        Path(
            kwargs["env"]["_VB_PARAMIKO_TUNNEL_PID_FILE"]
        ).write_text("8765", encoding="ascii")
        return _FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    proc = runner.start_port_forward(65362, remote_port=65361)

    assert proc.pid == 4321
    assert "child-env-secret" not in " ".join(captured["cmd"])
    assert (
        captured["env"]["_VB_PARAMIKO_TUNNEL_PASSWORD"]
        == "child-env-secret"
    )
    assert runner.tunnel_pid == 8765


def test_relay_preserves_response_after_client_half_close() -> None:
    client, relay_socket = socket.socketpair()

    class _HalfCloseChannel:
        def __init__(self) -> None:
            self.request = b""
            self.write_closed = False
            self.response = b"daemon-response"
            self.closed = False

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, data: bytes) -> None:
            self.request += data

        def shutdown_write(self) -> None:
            self.write_closed = True

        def recv(self, _size: int) -> bytes:
            if not self.write_closed:
                raise socket.timeout
            if self.response:
                data, self.response = self.response, b""
                return data
            return b""

        def close(self) -> None:
            self.closed = True

    channel = _HalfCloseChannel()
    transport = SimpleNamespace(
        open_channel=lambda *_args: channel,
    )
    thread = threading.Thread(
        target=_relay,
        args=(relay_socket, transport, 65361),
    )
    thread.start()
    try:
        client.sendall(b"request")
        client.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
    finally:
        client.close()
        thread.join(timeout=2)

    assert thread.is_alive() is False
    assert channel.request == b"request"
    assert channel.write_closed is True
    assert channel.closed is True
    assert response == b"daemon-response"
