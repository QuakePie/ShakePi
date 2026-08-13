"""SSH/SFTP session with the recorder Pi.

One :class:`ShakePieSession` owns a paramiko client, an SFTP channel, and helpers
for running commands (including passwordless-then-prompted sudo) and transferring
files with progress callbacks. Passwords live only in memory for the lifetime of
the session; host keys are taken from the system known_hosts when available and new
hosts are accepted with a visible warning.
"""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import paramiko
from rich.console import Console

from shakepie_terminal.names import RemoteFile


class SessionError(RuntimeError):
    """Raised when the connection or a remote operation fails."""


@dataclass(slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    def ok(self) -> bool:
        return self.exit_code == 0


class TofuHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use: accept unseen keys with a loud warning and remember them.

    Plain AutoAddPolicy is silent; this policy prints the key so a surprising change
    is at least visible, and stores it in a tool-local known_hosts file.
    """

    def __init__(self, console: Console, known_hosts: Path) -> None:
        self._console = console
        self._known_hosts = known_hosts

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        fingerprint = key.get_fingerprint().hex(":")
        self._console.print(
            f"[yellow]Warning: unknown host key for {hostname} "
            f"({key.get_name()} {fingerprint}). Trusting on first use.[/yellow]"
        )
        try:
            keys = paramiko.hostkeys.HostKeys()
            if self._known_hosts.is_file():
                keys.load(str(self._known_hosts))
            keys.add(hostname, key.get_name(), key)
            keys.save(str(self._known_hosts))
        except OSError:
            self._console.print("[dim]Could not persist the host key locally.[/dim]")
        client.get_host_keys().add(hostname, key.get_name(), key)


class ShakePieSession:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str | None,
        *,
        console: Console | None = None,
        known_hosts: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self._console = console or Console()
        self._known_hosts = known_hosts
        self._client = paramiko.SSHClient()
        self._sftp: paramiko.SFTPClient | None = None

    # -- lifecycle -------------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> None:
        try:
            # Trust-on-first-use: system known_hosts are honoured, and a new appliance
            # key is accepted once, loudly, and remembered for future verification.
            self._client.load_system_host_keys()
            default_known_hosts = Path.home() / ".shakepie-terminal-known_hosts"
            policy = TofuHostKeyPolicy(self._console, self._known_hosts or default_known_hosts)
            self._client.set_missing_host_key_policy(policy)
            self._client.connect(
                self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                look_for_keys=True,
                allow_agent=True,
                timeout=timeout,
                banner_timeout=30.0,
                auth_timeout=timeout,
            )
        except paramiko.AuthenticationException as error:
            message = f"authentication failed for {self.user}@{self.host}: {error}"
            raise SessionError(message) from error
        except (OSError, paramiko.SSHException) as error:
            message = f"cannot reach {self.host}:{self.port}: {error}"
            raise SessionError(message) from error
        transport = self._client.get_transport()
        if transport is None:
            raise SessionError("SSH transport did not start")
        transport.set_keepalive(30)

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        self._client.close()

    def __enter__(self) -> ShakePieSession:
        self.connect()
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    # -- commands --------------------------------------------------------------
    def run(self, command: str, timeout: float = 120.0) -> CommandResult:
        try:
            _stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            code = stdout.channel.recv_exit_status()
        except (OSError, paramiko.SSHException) as error:
            raise SessionError(f"remote command failed: {error}") from error
        return CommandResult(code, stdout.read().decode("utf-8", "replace"),
                             stderr.read().decode("utf-8", "replace"))

    def run_sudo(
        self, command: str, timeout: float = 120.0, stdin_text: str | None = None
    ) -> CommandResult:
        """Run a command as root: passwordless sudo first, then sudo -S with the password.

        ``stdin_text``, when given, is written after the sudo password line so commands
        that read their payload from stdin (``tee``) work in a single channel.
        """
        probe = self.run("sudo -n true", timeout=15.0)
        if probe.ok():
            fed = f"sudo -n {command}"
            password = b""
        else:
            if not self.password:
                raise SessionError("sudo needs a password; none was supplied")
            fed = f"sudo -S -p '' {command}"
            password = (self.password + "\n").encode()
        try:
            stdin, stdout, stderr = self._client.exec_command(fed, timeout=timeout)
            stdin.write(password)
            if stdin_text is not None:
                stdin.write(stdin_text)
            stdin.flush()
            stdin.channel.shutdown_write()
            code = stdout.channel.recv_exit_status()
        except (OSError, paramiko.SSHException) as error:
            raise SessionError(f"remote sudo command failed: {error}") from error
        result = CommandResult(code, stdout.read().decode("utf-8", "replace"),
                               stderr.read().decode("utf-8", "replace"))
        if not result.ok() and "incorrect password" in result.stderr.lower():
            raise SessionError("sudo rejected the password")
        return result

    # -- files -----------------------------------------------------------------
    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            try:
                self._sftp = self._client.open_sftp()
            except paramiko.SSHException as error:
                raise SessionError(f"SFTP channel failed: {error}") from error
        return self._sftp

    def list_files(self, directory: str, predicate: Callable[[str], bool]) -> list[RemoteFile]:
        try:
            entries = self.sftp().listdir_attr(directory)
        except (OSError, paramiko.SSHException) as error:
            raise SessionError(f"cannot list {directory}: {error}") from error
        return sorted(
            (
                RemoteFile(entry.filename, int(entry.st_size or 0))
                for entry in entries
                if not stat.S_ISDIR(entry.st_mode or 0) and predicate(entry.filename)
            ),
            key=lambda entry: entry.name,
        )

    def download(self, remote_path: str, local_path: Path,
                 progress: Callable[[int, int], None] | None = None) -> Path:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.sftp().get(remote_path, str(local_path), callback=progress)
        except (OSError, paramiko.SSHException) as error:
            raise SessionError(f"download failed for {remote_path}: {error}") from error
        return local_path

    def remote_sha256(self, remote_path: str) -> str | None:
        result = self.run(f"sha256sum '{remote_path}'", timeout=600.0)
        if not result.ok():
            return None
        return result.stdout.split(" ", 1)[0] or None

    @staticmethod
    def local_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def remove_files(self, directory: str, names: list[str]) -> int:
        sftp = self.sftp()
        removed = 0
        for name in names:
            try:
                sftp.remove(f"{directory}/{name}")
                removed += 1
            except (OSError, paramiko.SSHException):
                continue
        return removed
