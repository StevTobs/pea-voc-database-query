"""SSH local port forwarding ภายในโปรแกรม (ใช้ paramiko)

- เปิดพอร์ตฝั่ง local ที่ 127.0.0.1 เท่านั้น (พอร์ตสุ่ม) เครื่องอื่นในเครือข่ายเข้าไม่ได้
- ตรวจ host key กับ ~/.ssh/known_hosts เสมอ ไม่รับ host key ใหม่อัตโนมัติ
- รหัสผ่านใช้ตอนเชื่อมต่อเท่านั้น ไม่เก็บไว้ใน object
"""
from __future__ import annotations

import select
import socket
import threading
from typing import Optional, Tuple

import paramiko

BUFFER_SIZE = 32768


class TunnelError(Exception):
    pass


class SSHTunnel:
    def __init__(
        self,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        remote_host: str,
        remote_port: int,
        timeout: int = 10,
    ) -> None:
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.timeout = timeout
        self.local_port: Optional[int] = None
        self.last_error: Optional[str] = None
        self._client: Optional[paramiko.SSHClient] = None
        self._server: Optional[socket.socket] = None
        self._stop = threading.Event()

    @property
    def is_active(self) -> bool:
        transport = self._client.get_transport() if self._client else None
        return bool(transport and transport.is_active() and not self._stop.is_set())

    def start(self, password: Optional[str]) -> int:
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        use_password = bool(password)
        try:
            client.connect(
                self.ssh_host,
                port=self.ssh_port,
                username=self.ssh_user,
                password=password or None,
                allow_agent=not use_password,
                look_for_keys=not use_password,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
            )
        except paramiko.AuthenticationException:
            client.close()
            raise TunnelError(f"SSH: ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง ({self.ssh_user}@{self.ssh_host})")
        except paramiko.BadHostKeyException:
            client.close()
            raise TunnelError(
                f"SSH: host key ของ {self.ssh_host} ไม่ตรงกับที่บันทึกไว้ใน ~/.ssh/known_hosts — "
                "อาจถูกดักกลางทาง หรือเซิร์ฟเวอร์เปลี่ยน key ให้ติดต่อผู้ดูแลระบบก่อนใช้งาน"
            )
        except paramiko.SSHException as exc:
            client.close()
            if "known_hosts" in str(exc):
                raise TunnelError(
                    f"SSH: ยังไม่เคยยืนยัน host key ของ {self.ssh_host} — เปิด Terminal แล้วรัน "
                    f"`ssh {self.ssh_user}@{self.ssh_host}` หนึ่งครั้ง ตรวจ fingerprint กับผู้ดูแลระบบ แล้วตอบ yes"
                )
            raise TunnelError(f"SSH: {exc}")
        except (socket.timeout, TimeoutError):
            client.close()
            raise TunnelError(f"SSH: หมดเวลาเชื่อมต่อ {self.ssh_host}:{self.ssh_port} — ตรวจ VPN / เครือข่าย")
        except OSError as exc:
            client.close()
            raise TunnelError(f"SSH: เชื่อมต่อ {self.ssh_host}:{self.ssh_port} ไม่ได้ ({exc}) — ตรวจ VPN / เครือข่าย")

        transport = client.get_transport()
        assert transport is not None
        transport.set_keepalive(30)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        server.settimeout(0.5)

        self._client = client
        self._server = server
        self._stop.clear()
        self.local_port = server.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="ssh-tunnel-accept", daemon=True).start()
        return self.local_port

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        if self._client:
            self._client.close()
        self._server = None
        self._client = None
        self.local_port = None

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                sock, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._forward, args=(sock, addr), name="ssh-tunnel-forward", daemon=True
            ).start()

    def _forward(self, sock: socket.socket, addr: Tuple[str, int]) -> None:
        transport = self._client.get_transport() if self._client else None
        if transport is None or not transport.is_active():
            self.last_error = "SSH session หลุด"
            sock.close()
            return
        try:
            chan = transport.open_channel(
                "direct-tcpip", (self.remote_host, self.remote_port), addr, timeout=self.timeout
            )
        except Exception as exc:  # paramiko.ChannelException และอื่น ๆ
            self.last_error = (
                f"SSH server ส่งต่อไปที่ {self.remote_host}:{self.remote_port} ไม่ได้ ({exc})"
            )
            sock.close()
            return
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select([sock, chan], [], [], 1.0)
                if sock in readable:
                    data = sock.recv(BUFFER_SIZE)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in readable:
                    data = chan.recv(BUFFER_SIZE)
                    if not data:
                        break
                    sock.sendall(data)
        except OSError:
            pass
        finally:
            chan.close()
            sock.close()
