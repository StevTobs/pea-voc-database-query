"""ตรวจว่าเครื่องอยู่ในเครือข่าย Wi-Fi ที่อนุญาต (macOS)

macOS รุ่นใหม่ซ่อนชื่อ Wi-Fi (SSID) จากโปรแกรมที่ไม่มีสิทธิ์ Location จึงใช้ 2 วิธี:
1. SSID — ถ้าระบบยอมแสดง
2. MAC address ของ router (gateway) บน Wi-Fi — อ่านได้เสมอ ไม่ต้องขอสิทธิ์

เป็นการป้องกันการใช้งานผิดเครือข่ายโดยไม่ตั้งใจ ไม่ใช่กลไกความปลอดภัยหลัก
(การยืนยันตัวตนจริงยังเป็น VPN / SSH / รหัสผ่านฐานข้อมูล)
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Set

CMD_TIMEOUT_S = 3


@dataclass(frozen=True)
class NetworkStatus:
    allowed: bool
    reason: str
    interface: Optional[str] = None
    ssid: Optional[str] = None
    gateway_ip: Optional[str] = None
    gateway_mac: Optional[str] = None


def _run(args: List[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def normalize_mac(mac: str) -> Optional[str]:
    parts = re.split(r"[:-]", mac.strip().lower())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9a-f]{1,2}", p) for p in parts):
        return None
    return ":".join(p.zfill(2) for p in parts)


def parse_list(raw: str) -> List[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def wifi_interface() -> Optional[str]:
    lines = _run(["networksetup", "-listallhardwareports"]).splitlines()
    for i, line in enumerate(lines):
        if line.strip() in ("Hardware Port: Wi-Fi", "Hardware Port: AirPort") and i + 1 < len(lines):
            m = re.match(r"\s*Device:\s*(\S+)", lines[i + 1])
            if m:
                return m.group(1)
    return None


def current_ssid(interface: str) -> Optional[str]:
    for line in _run(["ipconfig", "getsummary", interface]).splitlines():
        m = re.match(r"\s*SSID\s*:\s*(.+)$", line)
        if m:
            ssid = m.group(1).strip()
            return None if ssid == "<redacted>" else ssid
    return None


def gateway_ip(interface: str) -> Optional[str]:
    ip = _run(["ipconfig", "getoption", interface, "router"]).strip()
    return ip if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) else None


def gateway_mac(ip: str) -> Optional[str]:
    for attempt in range(2):
        m = re.search(r"\bat\s+([0-9a-fA-F:]+)\s", _run(["arp", "-n", ip]))
        if m:
            return normalize_mac(m.group(1))
        if attempt == 0:  # ยังไม่มีใน ARP cache — ping หนึ่งครั้งแล้วลองใหม่
            _run(["ping", "-c", "1", "-t", "1", ip])
    return None


def check_network(allowed_ssids: Set[str], allowed_macs: Set[str]) -> NetworkStatus:
    if platform.system() != "Darwin":
        return NetworkStatus(False, "การล็อก Wi-Fi รองรับเฉพาะ macOS")

    iface = wifi_interface()
    if not iface:
        return NetworkStatus(False, "ไม่พบอุปกรณ์ Wi-Fi บนเครื่องนี้")
    ssid = current_ssid(iface)
    gw_ip = gateway_ip(iface)
    if not gw_ip:
        return NetworkStatus(False, "ยังไม่ได้เชื่อมต่อ Wi-Fi", iface, ssid)
    gw_mac = gateway_mac(gw_ip)
    status = dict(interface=iface, ssid=ssid, gateway_ip=gw_ip, gateway_mac=gw_mac)

    if not allowed_ssids and not allowed_macs:
        return NetworkStatus(False, "ยังไม่ได้กำหนดเครือข่ายที่อนุญาตในไฟล์ .env", **status)
    if ssid and ssid in allowed_ssids:
        return NetworkStatus(True, f"Wi-Fi '{ssid}' อยู่ในรายการที่อนุญาต", **status)
    if gw_mac and gw_mac in allowed_macs:
        return NetworkStatus(True, "router ของเครือข่ายนี้อยู่ในรายการที่อนุญาต", **status)
    return NetworkStatus(False, "เครือข่ายนี้ไม่อยู่ในรายการที่อนุญาต", **status)


def check_network_from_env() -> NetworkStatus:
    ssids = set(parse_list(os.getenv("ALLOWED_WIFI_SSIDS", "")))
    macs = {m for m in (normalize_mac(x) for x in parse_list(os.getenv("ALLOWED_GATEWAY_MACS", ""))) if m}
    return check_network(ssids, macs)


if __name__ == "__main__":
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).resolve().parent / ".env")
    st = check_network_from_env()
    print(f"อนุญาต: {'ใช่' if st.allowed else 'ไม่'} — {st.reason}")
    print(f"Wi-Fi: {st.interface}  SSID: {st.ssid or '(ระบบซ่อนไว้)'}")
    print(f"Router: {st.gateway_ip}  MAC: {st.gateway_mac}")
    if st.gateway_mac and not st.allowed:
        print(f"\nถ้าเครือข่ายนี้ได้รับอนุญาต ให้เพิ่มใน .env:\nALLOWED_GATEWAY_MACS={st.gateway_mac}")
