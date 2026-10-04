"""PEA VOC Data Viewer — เว็บแอปบนเครื่อง สำหรับดูและ export ข้อมูลตัวอย่าง (read-only)

รัน: streamlit run app.py   (เปิดได้เฉพาะ http://localhost:8501 บนเครื่องนี้)

- เปิด SSH tunnel เองภายในแอป และตรวจ host key กับ ~/.ssh/known_hosts
- ทำงานเฉพาะเมื่ออยู่ในเครือข่าย Wi-Fi ที่อนุญาต (ตั้งใน .env)
- ใช้ SELECT เท่านั้น session เป็น read-only ไม่เกิน 100 แถวต่อตาราง
- รหัสผ่านใช้ตอนเชื่อมต่อแล้วล้างทิ้ง ไม่บันทึกและไม่แสดงใน log
"""
from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import pandas as pd
import psycopg
import streamlit as st

import netguard
import pii
import sample_export as se
from ssh_tunnel import SSHTunnel, TunnelError

st.set_page_config(page_title="PEA VOC Data Viewer", page_icon="📊", layout="wide")

MASK = "•••"
KEEP_UNMASKED = ("id", "created_at", "updated_at", "status")
S = st.session_state


# ---------------------------------------------------------------------------
# การตั้งค่า
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SSHConfig:
    host: str
    port: int
    user: str
    remote_host: str
    remote_port: int


def load_ssh_config() -> Optional[SSHConfig]:
    host = os.getenv("SSH_HOST", "").strip()
    if not host:
        return None
    missing = [k for k in ("SSH_USER", "SSH_REMOTE_DB_HOST", "SSH_REMOTE_DB_PORT") if not os.getenv(k, "").strip()]
    if missing:
        raise se.ConfigError(f"ตั้ง SSH_HOST แล้ว แต่ยังไม่ได้กรอก {', '.join(missing)} ในไฟล์ .env")
    return SSHConfig(
        host=host,
        port=se._int_env("SSH_PORT", 22, 1, 65535),
        user=os.getenv("SSH_USER", "").strip(),
        remote_host=os.getenv("SSH_REMOTE_DB_HOST", "").strip(),
        remote_port=se._int_env("SSH_REMOTE_DB_PORT", 5432, 1, 65535),
    )


def load_settings() -> Tuple[se.Config, Optional[SSHConfig]]:
    se.load_env()
    ssh = load_ssh_config()
    return se.load_config(require_host=ssh is None), ssh


# ---------------------------------------------------------------------------
# สถานะการเชื่อมต่อ
# ---------------------------------------------------------------------------

for key, default in {
    "tunnel": None, "conn": None, "db_cfg": None, "tables": [], "denied": 0,
    "preview": None, "related": None, "flash": [],
}.items():
    S.setdefault(key, default)


def flash(kind: str, message: str) -> None:
    S.flash.append((kind, message))


def is_connected() -> bool:
    conn = S.conn
    tunnel = S.tunnel
    return conn is not None and not conn.closed and (tunnel is None or tunnel.is_active)


def disconnect(message: Optional[str] = None) -> None:
    if S.conn is not None:
        with contextlib.suppress(Exception):
            S.conn.close()
    if S.tunnel is not None:
        S.tunnel.stop()
    S.conn = S.tunnel = S.db_cfg = S.preview = S.related = None
    S.tables, S.denied = [], 0
    if message:
        flash("warning", message)


def connection_help(cfg: se.Config, ssh: Optional[SSHConfig], exc: Exception, tunnel_error: Optional[str]) -> str:
    msg = str(exc).strip()
    lower = msg.lower()
    lines = [f"เชื่อมต่อฐานข้อมูลไม่สำเร็จ: {msg}"]
    if tunnel_error:
        lines.append(f"SSH tunnel: {tunnel_error}")
    if "password authentication failed" in lower:
        lines.append("→ รหัสผ่านฐานข้อมูลไม่ถูกต้อง")
    if "ssl" in lower or "certificate" in lower:
        lines.append(f"→ ปัญหา SSL (DB_SSLMODE={cfg.sslmode}) ให้ขอ sslmode / ไฟล์ CA ที่ถูกต้องจากผู้ดูแลระบบ "
                     "แอปนี้จะไม่ปิดการตรวจสอบ SSL ให้อัตโนมัติ")
    if "no pg_hba.conf entry" in lower:
        lines.append("→ เซิร์ฟเวอร์ไม่อนุญาตการเชื่อมต่อแบบนี้ (pg_hba.conf) ติดต่อผู้ดูแลระบบ")
    if ssh:
        lines.append(f"ตรวจ: SSH_REMOTE_DB_HOST/PORT = {ssh.remote_host}:{ssh.remote_port} ถูกต้องไหม")
    else:
        lines.append(f"ตรวจ: VPN, DB_HOST/DB_PORT = {cfg.host}:{cfg.port}")
    return "\n\n".join(lines)


def do_connect(cfg: se.Config, ssh: Optional[SSHConfig], ssh_password: str, db_password: str) -> None:
    tunnel: Optional[SSHTunnel] = None
    try:
        if ssh:
            tunnel = SSHTunnel(ssh.host, ssh.port, ssh.user, ssh.remote_host, ssh.remote_port,
                               timeout=cfg.connect_timeout)
            local_port = tunnel.start(ssh_password or None)
            db_cfg = replace(cfg, host="127.0.0.1", port=local_port)
        else:
            db_cfg = cfg
        conn = se.connect(db_cfg, db_password)
    except TunnelError as exc:
        flash("error", str(exc))
        return
    except psycopg.OperationalError as exc:
        tunnel_error = tunnel.last_error if tunnel else None
        if tunnel:
            tunnel.stop()
        flash("error", connection_help(cfg, ssh, exc, tunnel_error))
        return
    except Exception as exc:
        if tunnel:
            tunnel.stop()
        flash("error", f"เชื่อมต่อไม่สำเร็จ: {exc}")
        return

    try:
        tables, denied = se.list_tables(conn)
    except psycopg.Error as exc:
        conn.close()
        if tunnel:
            tunnel.stop()
        flash("error", f"อ่านรายชื่อตารางไม่สำเร็จ: {exc}")
        return
    S.conn, S.tunnel, S.db_cfg = conn, tunnel, db_cfg
    S.tables, S.denied = tables, denied
    flash("success", f"เชื่อมต่อสำเร็จ — session read-only, statement_timeout={cfg.statement_timeout_ms} ms")


def db_call(cfg: se.Config, label: str, fn):
    """เรียกฐานข้อมูลหลังตรวจเครือข่ายซ้ำ และแปลง error เป็นข้อความ"""
    status = netguard.check_network_from_env()
    if not status.allowed:
        disconnect(f"ตัดการเชื่อมต่อแล้ว เพราะเครือข่ายไม่อยู่ในรายการที่อนุญาต ({status.reason})")
        st.rerun()
    try:
        return fn()
    except psycopg.OperationalError as exc:
        disconnect(f"การเชื่อมต่อขาด: {str(exc).strip()} — ตรวจ VPN / SSH แล้วเชื่อมต่อใหม่")
        st.rerun()
    except psycopg.Error as exc:
        st.error(se.describe_db_error(exc, cfg, label))
    except se.RelatedError as exc:
        st.error(str(exc))
    return None


# ---------------------------------------------------------------------------
# ข้อมูลและการปกปิด
# ---------------------------------------------------------------------------

def fetch_preview(conn: psycopg.Connection, table: se.TableInfo, limit: int) -> se.SampleResult:
    try:
        cols = se.get_columns(conn, table)
        pk = se.get_primary_key(conn, table)
        headers, rows = se.fetch_rows(conn, table, cols, pk, limit)
    finally:
        if not conn.closed:
            conn.rollback()
    order_note = (f"เรียงตาม primary key: {', '.join(pk)}" if pk
                  else "ไม่มี primary key — ลำดับแถวขึ้นกับฐานข้อมูล")
    return se.SampleResult(table, cols, headers, rows, order_note,
                           f"ข้อมูลตัวอย่าง {len(rows)} แถวแรก (สูงสุด {limit}) ไม่ใช่การสุ่ม")


def sensitive_indexes(table: se.TableInfo, headers: Sequence[str]) -> List[int]:
    whole_table = pii.is_sensitive_table(table.schema, table.name)
    result = []
    for i, h in enumerate(headers):
        if pii.is_sensitive_column(table.name, h):
            result.append(i)
        elif whole_table and not (h in KEEP_UNMASKED or h.endswith("_id") or h.startswith("is_")):
            result.append(i)
    return result


def apply_mask(result: se.SampleResult, mask: bool) -> Tuple[List[Sequence[Any]], List[str]]:
    if not mask:
        return list(result.rows), []
    idx = set(sensitive_indexes(result.table, result.headers))
    rows = [[MASK if i in idx and v is not None else v for i, v in enumerate(r)] for r in result.rows]
    return rows, [result.headers[i] for i in sorted(idx)]


def to_dataframe(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
    return pd.DataFrame([[None if v is None else se.to_text(v) for v in r] for r in rows], columns=list(headers))


def save_result(result: se.SampleResult, mask: bool) -> Tuple[Path, List[str]]:
    rows, masked = apply_mask(result, mask)
    path = se.output_path(result.table)
    se.write_csv(path, result.headers, rows)
    return path, masked


def show_result(result: se.SampleResult, mask: bool, height: int = 380) -> None:
    rows, masked = apply_mask(result, mask)
    st.caption(f"{result.sample_note} · {result.order_note}")
    if masked:
        st.caption(f"🔒 ปกปิด {len(masked)} คอลัมน์: {', '.join(masked)}")
    binary = [c.name for c in result.columns if c.binary_kind]
    if binary:
        st.caption(f"คอลัมน์ binary แสดงเป็นขนาดข้อมูล: {', '.join(binary)}")
    if result.rows:
        st.dataframe(to_dataframe(result.headers, rows), height=height)
    else:
        st.info("ไม่มีข้อมูล")


# ---------------------------------------------------------------------------
# หน้าจอ
# ---------------------------------------------------------------------------

@st.cache_data(ttl=10, show_spinner=False)
def cached_network_status() -> netguard.NetworkStatus:
    return netguard.check_network_from_env()


try:
    cfg, ssh = load_settings()
except se.ConfigError as exc:
    st.error(f"ตั้งค่าไม่ครบ: {exc}")
    st.stop()

with st.sidebar:
    st.title("📊 PEA VOC")
    st.caption("ดูและ export ข้อมูลตัวอย่าง (read-only)")

    st.subheader("เครือข่าย")
    net = cached_network_status()
    if net.allowed:
        st.success(f"✅ {net.reason}")
    else:
        st.error(f"⛔ {net.reason}")
        st.caption(f"Wi-Fi: {net.ssid or '(ระบบซ่อนชื่อ)'} · router: {net.gateway_ip or '-'}")
        if net.gateway_mac:
            st.caption("ถ้าเครือข่ายนี้ได้รับอนุญาตจากองค์กร ให้เพิ่มบรรทัดนี้ใน .env แล้วกดตรวจอีกครั้ง")
            st.code(f"ALLOWED_GATEWAY_MACS={net.gateway_mac}", language=None)
    if st.button("ตรวจเครือข่ายอีกครั้ง", width="stretch"):
        cached_network_status.clear()
        st.rerun()

    if not net.allowed and is_connected():
        disconnect("ตัดการเชื่อมต่อแล้ว เพราะเครือข่ายไม่อยู่ในรายการที่อนุญาต")
    if S.conn is not None and not is_connected():
        disconnect("การเชื่อมต่อหลุด กรุณาเชื่อมต่อใหม่")

    st.subheader("การเชื่อมต่อ")
    if ssh:
        st.caption(f"SSH: {ssh.user}@{ssh.host}:{ssh.port}\n\nDB: {cfg.user}@{ssh.remote_host}:{ssh.remote_port}/{cfg.dbname}")
    else:
        st.caption(f"DB: {cfg.user}@{cfg.host}:{cfg.port}/{cfg.dbname} (ไม่ใช้ SSH)")

    if is_connected():
        st.success("🟢 เชื่อมต่ออยู่")
        if st.button("ตัดการเชื่อมต่อ", width="stretch"):
            disconnect()
            flash("info", "ตัดการเชื่อมต่อแล้ว")
            st.rerun()
    elif net.allowed:
        with st.form("connect", clear_on_submit=True):
            ssh_pw = ""
            if ssh:
                ssh_pw = st.text_input(f"รหัสผ่าน SSH ({ssh.user})", type="password",
                                       help="เว้นว่างถ้าใช้ SSH key")
            db_pw = st.text_input(f"รหัสผ่านฐานข้อมูล ({cfg.user})", type="password")
            submitted = st.form_submit_button("เชื่อมต่อ", type="primary", width="stretch")
        if submitted:
            with st.spinner("กำลังเชื่อมต่อ..."):
                do_connect(cfg, ssh, ssh_pw, db_pw)
            ssh_pw = db_pw = ""
            st.rerun()

    st.divider()
    mask = st.toggle("🔒 ปกปิดข้อมูลส่วนบุคคล", value=True,
                     help="ปกปิดทั้งบนหน้าจอและในไฟล์ CSV เช่น ชื่อ เลขบัตร เบอร์ อีเมล ที่อยู่ ข้อความอิสระ")
    st.caption("ไฟล์ CSV อาจมีข้อมูลจริงขององค์กร เก็บเฉพาะในพื้นที่ที่องค์กรอนุญาต")

for kind, message in S.flash:
    getattr(st, kind)(message)
S.flash = []

if not net.allowed:
    st.warning("แอปทำงานเฉพาะในเครือข่าย Wi-Fi ที่อนุญาต — ดูรายละเอียดที่แถบซ้าย")
    st.stop()
if not is_connected():
    st.info("👈 กรอกรหัสผ่านที่แถบซ้ายแล้วกด **เชื่อมต่อ**")
    st.stop()

conn: psycopg.Connection = S.conn
tables: List[se.TableInfo] = S.tables
tab_view, tab_related = st.tabs(["🔎 ดูข้อมูลตาราง", "🔗 ชุดตัวอย่าง VOC ที่สัมพันธ์กัน"])

with tab_view:
    st.caption(f"ตารางที่บัญชีนี้มีสิทธิ์ SELECT: {len(tables)} รายการ"
               + (f" (ไม่มีสิทธิ์อีก {S.denied} รายการ)" if S.denied else ""))
    c1, c2 = st.columns([3, 1])
    table = c1.selectbox(
        "ตาราง", tables,
        format_func=lambda t: t.label + ("" if t.kind == "table" else f"  [{t.kind}]"),
    )
    limit = c2.number_input("จำนวนแถว", min_value=1, max_value=se.MAX_ROWS, value=cfg.sample_rows)
    if table and pii.is_sensitive_table(table.schema, table.name):
        st.warning("ตารางนี้อาจมีข้อมูลบุคคลหรือ log — โปรดใช้เท่าที่จำเป็น")

    if st.button("โหลดตัวอย่าง", type="primary"):
        with st.spinner(f"กำลังอ่าน {table.label} ..."):
            result = db_call(cfg, table.label, lambda: fetch_preview(conn, table, int(limit)))
        if result is not None:
            S.preview = result

    preview: Optional[se.SampleResult] = S.preview
    if preview is not None and table is not None and preview.table.label == table.label:
        show_result(preview, mask)
        with st.expander(f"โครงสร้างคอลัมน์ ({len(preview.columns)})"):
            st.dataframe(pd.DataFrame([{
                "คอลัมน์": c.name,
                "ชนิด": c.type_name,
                "ข้อมูลส่วนบุคคล": "ใช่" if pii.is_sensitive_column(preview.table.name, c.name) else "",
            } for c in preview.columns]), hide_index=True)
        if st.button("💾 บันทึก CSV ลง outputs/"):
            path, masked = save_result(preview, mask)
            st.success(f"บันทึกแล้ว: {path.relative_to(se.PROJECT_DIR)}"
                       + (f" (ปกปิด {len(masked)} คอลัมน์)" if masked else " (ไม่ได้ปกปิดข้อมูล)"))

with tab_related:
    st.write(
        "ดึง `voc_master` เรื่องล่าสุด แล้วดึง `voc_detail` และ `voc_tracking` เฉพาะของเรื่องชุดเดียวกัน "
        "เพื่อให้ join กันได้ใน dev.db (ไม่เกิน 100 แถวต่อตาราง)"
    )
    n_master = st.number_input("จำนวนเรื่อง VOC", min_value=1, max_value=se.MAX_ROWS,
                               value=cfg.related_rows,
                               help="voc_tracking มีหลายแถวต่อเรื่อง ถ้าตั้งสูงเกินไปจะถูกตัดที่ 100 แถว")
    if st.button("ดึงชุดตัวอย่าง", type="primary"):
        with st.spinner("กำลังอ่าน voc_master / voc_detail / voc_tracking ..."):
            out = db_call(cfg, "ชุดตัวอย่าง VOC",
                          lambda: se.collect_related(conn, cfg, tables, int(n_master)))
        if out is not None:
            S.related = out

    if S.related is not None:
        results, warnings = S.related
        for w in warnings:
            st.warning(w)
        for r in results:
            with st.expander(f"{r.table.label} — {len(r.rows)} แถว", expanded=r is results[0]):
                show_result(r, mask, height=300)
        c1, c2 = st.columns(2)
        if c1.button("💾 บันทึกทั้งชุดลง outputs/", width="stretch"):
            for r in results:
                path, masked = save_result(r, mask)
                st.success(f"บันทึกแล้ว: {path.relative_to(se.PROJECT_DIR)}"
                           + (f" (ปกปิด {len(masked)} คอลัมน์)" if masked else ""))
        if c2.button("🛠 อัปเดต dev.db จากไฟล์ใน outputs/", width="stretch"):
            import build_dev_db

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                build_dev_db.main()
            st.code(buf.getvalue(), language=None)
