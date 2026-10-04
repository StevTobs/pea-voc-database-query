"""ดึงข้อมูลตัวอย่าง (ไม่เกิน 100 แถว) จาก PostgreSQL แล้วบันทึกเป็น CSV

- ใช้คำสั่ง SELECT เท่านั้น ไม่มีการเขียนหรือแก้ไขฐานข้อมูล
- เซสชันถูกตั้งเป็น read-only และกำหนด statement timeout
- รหัสผ่านรับผ่าน getpass ทุกครั้ง ไม่เก็บในไฟล์และไม่แสดงใน log
"""
from __future__ import annotations

import csv
import getpass
import json
import os
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

try:
    import psycopg
    from dotenv import load_dotenv
    from psycopg import errors, sql
    from psycopg.types.datetime import DateLoader, TimestampLoader, TimestamptzLoader
except ImportError as exc:  # pragma: no cover
    sys.exit(f"ไม่พบไลบรารี {exc.name} — โปรดรัน: pip install -r requirements.txt")

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "outputs"
SCHEMAS = ["public", "logs"]
MAX_ROWS = 100
PREVIEW_ROWS = 10
PREVIEW_CELL_WIDTH = 24
MAX_PASSWORD_ATTEMPTS = 3
SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
RELATED_ROOT = "public.voc_master"
RELATED_CHILDREN = ["public.voc_detail", "public.voc_tracking"]
RELATED_FK = "voc_master_id"
RELKIND_LABELS = {"r": "table", "p": "partitioned", "v": "view", "m": "matview", "f": "foreign"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    dbname: str
    user: str
    sslmode: str
    sslrootcert: Optional[str]
    connect_timeout: int
    statement_timeout_ms: int
    sample_rows: int
    related_rows: int


@dataclass(frozen=True)
class TableInfo:
    oid: int
    schema: str
    name: str
    kind: str

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type_name: str
    binary_kind: Optional[str]  # "bytea", "bytea[]" หรือ None


# ---------------------------------------------------------------------------
# การตั้งค่า
# ---------------------------------------------------------------------------

def _int_env(key: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} ต้องเป็นตัวเลข (ได้รับ: {raw!r})")
    if not low <= value <= high:
        raise ConfigError(f"{key} ต้องอยู่ระหว่าง {low} ถึง {high} (ได้รับ: {value})")
    return value


def load_env() -> None:
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        raise ConfigError("ไม่พบไฟล์ .env — คัดลอกจาก .env.example แล้วกรอกค่า")
    load_dotenv(env_file)


def load_config(require_host: bool = True) -> Config:
    """require_host=False ใช้กับแอปที่เปิด SSH tunnel เอง (host/port จะถูกแทนด้วยฝั่ง local ของ tunnel)"""
    load_env()

    host = os.getenv("DB_HOST", "").strip()
    if require_host and not host:
        raise ConfigError("ยังไม่ได้กรอก DB_HOST ในไฟล์ .env")
    if require_host and not os.getenv("DB_PORT", "").strip():
        raise ConfigError("ยังไม่ได้กรอก DB_PORT ในไฟล์ .env")
    port = _int_env("DB_PORT", 5432, 1, 65535)

    sslmode = os.getenv("DB_SSLMODE", "").strip() or "prefer"
    if sslmode not in SSL_MODES:
        raise ConfigError(f"DB_SSLMODE ไม่ถูกต้อง: {sslmode!r} (ใช้ได้: {', '.join(sorted(SSL_MODES))})")

    return Config(
        host=host,
        port=port,
        dbname=os.getenv("DB_NAME", "").strip() or "pea_voc",
        user=os.getenv("DB_USER", "").strip() or "voc_user",
        sslmode=sslmode,
        sslrootcert=os.getenv("DB_SSLROOTCERT", "").strip() or None,
        connect_timeout=_int_env("DB_CONNECT_TIMEOUT_S", 10, 1, 120),
        statement_timeout_ms=_int_env("DB_STATEMENT_TIMEOUT_MS", 30000, 1000, 600000),
        sample_rows=_int_env("DB_SAMPLE_ROWS", MAX_ROWS, 1, MAX_ROWS),
        related_rows=_int_env("DB_RELATED_ROWS", 20, 1, MAX_ROWS),
    )


# ---------------------------------------------------------------------------
# การเชื่อมต่อ
# ---------------------------------------------------------------------------

def _tolerant(loader_cls: type) -> type:
    """ค่าอย่าง 'infinity' หรือปี ค.ศ. นอกช่วงของ Python ให้คืนเป็นข้อความแทนการ error"""

    class Tolerant(loader_cls):  # type: ignore[valid-type, misc]
        def load(self, data: Any) -> Any:
            try:
                return super().load(data)
            except psycopg.DataError:
                return bytes(data).decode("utf-8", "replace")

    Tolerant.__name__ = f"Tolerant{loader_cls.__name__}"
    return Tolerant


def register_tolerant_loaders(conn: psycopg.Connection) -> None:
    conn.adapters.register_loader("date", _tolerant(DateLoader))
    conn.adapters.register_loader("timestamp", _tolerant(TimestampLoader))
    conn.adapters.register_loader("timestamptz", _tolerant(TimestamptzLoader))


def connect(cfg: Config, password: str) -> psycopg.Connection:
    params = {
        "host": cfg.host,
        "port": cfg.port,
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": password,
        "sslmode": cfg.sslmode,
        "connect_timeout": cfg.connect_timeout,
        "application_name": "pea_voc_sample_export",
    }
    if cfg.sslrootcert:
        params["sslrootcert"] = cfg.sslrootcert

    conn = psycopg.connect(autocommit=True, **params)
    try:
        # ตั้งค่าระดับ session: ทุก transaction เป็น read-only และจำกัดเวลาต่อคำสั่ง
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.execute(
            sql.SQL("SET statement_timeout = {}").format(sql.Literal(cfg.statement_timeout_ms))
        )
        row = conn.execute("SHOW transaction_read_only").fetchone()
        if not row or row[0] != "on":
            raise RuntimeError("ไม่สามารถยืนยันได้ว่า session เป็น read-only — ยกเลิกเพื่อความปลอดภัย")
        conn.autocommit = False
        conn.read_only = True  # psycopg จะเปิด transaction ด้วย BEGIN READ ONLY
        register_tolerant_loaders(conn)
    except Exception:
        conn.close()
        raise
    return conn


def print_connection_help(cfg: Config, exc: Exception) -> None:
    message = str(exc).strip()
    lower = message.lower()
    print("\n[ERROR] เชื่อมต่อฐานข้อมูลไม่สำเร็จ")
    print(f"รายละเอียดจากไลบรารี: {message}\n")

    hints = []
    if "password authentication failed" in lower:
        hints.append("รหัสผ่านหรือชื่อผู้ใช้ไม่ถูกต้อง")
    if "could not translate host name" in lower or "nodename nor servname" in lower:
        hints.append("ไม่รู้จักชื่อ Host — ตรวจ DB_HOST และการเชื่อมต่อ VPN")
    if "connection refused" in lower:
        hints.append("Port ปฏิเสธการเชื่อมต่อ — SSH tunnel อาจยังไม่เปิด หรือ DB_PORT ไม่ถูกต้อง")
    if "timeout" in lower or "timed out" in lower:
        hints.append("หมดเวลารอ — มักเกิดจากยังไม่ได้ต่อ VPN หรือ firewall ปิดกั้น")
    if "ssl" in lower or "certificate" in lower:
        hints.append("ปัญหาเกี่ยวกับ SSL — ดูข้อ 4 ด้านล่าง")
    if "no pg_hba.conf entry" in lower:
        hints.append("เซิร์ฟเวอร์ไม่อนุญาตการเชื่อมต่อจากเครื่อง/รูปแบบนี้ (pg_hba.conf) — ติดต่อผู้ดูแลระบบ")
    if hints:
        print("สาเหตุที่เป็นไปได้:")
        for hint in hints:
            print(f"  - {hint}")
        print()

    print("รายการตรวจสอบ:")
    print("  1. VPN: เชื่อมต่อ VPN ขององค์กรแล้วหรือยัง")
    print("  2. SSH tunnel: ถ้าใช้ tunnel ต้องเปิดคำสั่ง ssh -L ค้างไว้ในอีกหน้าต่าง Terminal")
    print("     และ DB_HOST/DB_PORT ต้องชี้ไปที่ฝั่ง local ของ tunnel (เช่น 127.0.0.1 และ port ที่ใช้ใน -L)")
    print(f"  3. Host/Port: ค่าปัจจุบันใน .env คือ {cfg.host}:{cfg.port}")
    print("     (อย่าใช้ port ชั่วคราวจาก DBeaver เพราะจะเปลี่ยนทุกครั้งที่เปิดใหม่)")
    print(f"  4. SSL: DB_SSLMODE ปัจจุบัน = {cfg.sslmode}"
          + (f", DB_SSLROOTCERT = {cfg.sslrootcert}" if cfg.sslrootcert else ""))
    print("     หากเซิร์ฟเวอร์บังคับ SSL หรือใบรับรองไม่ผ่าน ให้ขอไฟล์ CA / sslmode ที่ถูกต้องจากผู้ดูแลระบบ")
    print("     โปรแกรมนี้จะไม่ปิดการตรวจสอบ SSL ให้อัตโนมัติ")
    print(f"  5. Database/User: {cfg.dbname} / {cfg.user}")


def connect_interactive(cfg: Config) -> Optional[psycopg.Connection]:
    print(f"เชื่อมต่อ {cfg.user}@{cfg.host}:{cfg.port}/{cfg.dbname} (sslmode={cfg.sslmode})")
    for attempt in range(1, MAX_PASSWORD_ATTEMPTS + 1):
        password = getpass.getpass(f"รหัสผ่านของ {cfg.user} (ไม่แสดงบนหน้าจอ): ")
        try:
            conn = connect(cfg, password)
        except psycopg.OperationalError as exc:
            auth_failed = "password authentication failed" in str(exc).lower()
            if auth_failed and attempt < MAX_PASSWORD_ATTEMPTS:
                print(f"[ERROR] รหัสผ่านไม่ถูกต้อง ลองใหม่ ({attempt}/{MAX_PASSWORD_ATTEMPTS})")
                continue
            print_connection_help(cfg, exc)
            return None
        finally:
            password = ""
        return conn
    return None


# ---------------------------------------------------------------------------
# อ่าน catalog (SELECT อย่างเดียว)
# ---------------------------------------------------------------------------

LIST_TABLES_SQL = """
SELECT c.oid, n.nspname, c.relname, c.relkind,
       has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT') AS can_select
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s::name[])
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT c.relispartition
ORDER BY n.nspname, c.relname
"""

COLUMNS_SQL = """
SELECT a.attname,
       pg_catalog.format_type(a.atttypid, a.atttypmod),
       CASE
         WHEN COALESCE(NULLIF(t.typbasetype, 0), t.oid) = 'bytea'::regtype THEN 'bytea'
         WHEN t.typcategory = 'A' AND t.typelem = 'bytea'::regtype THEN 'bytea[]'
       END
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

PRIMARY_KEY_SQL = """
SELECT a.attname
FROM pg_catalog.pg_index i
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indrelid = %s AND i.indisprimary
ORDER BY k.ord
"""


def list_tables(conn: psycopg.Connection) -> tuple[List[TableInfo], int]:
    try:
        rows = conn.execute(LIST_TABLES_SQL, (SCHEMAS,)).fetchall()
    finally:
        conn.rollback()
    allowed = [TableInfo(r[0], r[1], r[2], RELKIND_LABELS.get(r[3], r[3])) for r in rows if r[4]]
    return allowed, len(rows) - len(allowed)


def get_columns(conn: psycopg.Connection, table: TableInfo) -> List[ColumnInfo]:
    rows = conn.execute(COLUMNS_SQL, (table.oid,)).fetchall()
    return [ColumnInfo(r[0], r[1], r[2]) for r in rows]


def get_primary_key(conn: psycopg.Connection, table: TableInfo) -> List[str]:
    return [r[0] for r in conn.execute(PRIMARY_KEY_SQL, (table.oid,)).fetchall()]


def build_sample_query(
    table: TableInfo,
    columns: Sequence[ColumnInfo],
    order_cols: Sequence[str],
    limit: int,
    where: Optional[sql.Composable] = None,
    descending: bool = False,
) -> sql.Composed:
    exprs = []
    for col in columns:
        ident = sql.Identifier(col.name)
        if col.binary_kind == "bytea":
            # ไม่ดึงเนื้อหา binary มาจริง ส่งกลับเฉพาะขนาด
            exprs.append(sql.SQL(
                "CASE WHEN src.{c} IS NULL THEN NULL"
                " ELSE '<binary ' || octet_length(src.{c}) || ' bytes>' END AS {c}"
            ).format(c=ident))
        elif col.binary_kind == "bytea[]":
            exprs.append(sql.SQL(
                "CASE WHEN src.{c} IS NULL THEN NULL"
                " ELSE '<binary array ' || cardinality(src.{c}) || ' items>' END AS {c}"
            ).format(c=ident))
        else:
            exprs.append(sql.SQL("src.{}").format(ident))

    query = sql.SQL("SELECT {cols} FROM {tbl} AS src").format(
        cols=sql.SQL(", ").join(exprs),
        tbl=sql.Identifier(table.schema, table.name),
    )
    if where is not None:
        query += sql.SQL(" WHERE ") + where
    if order_cols:
        direction = sql.SQL(" DESC" if descending else "")
        query += sql.SQL(" ORDER BY {}").format(
            sql.SQL(", ").join(
                sql.SQL("src.{}{}").format(sql.Identifier(c), direction) for c in order_cols
            )
        )
    query += sql.SQL(" LIMIT {}").format(sql.Literal(limit))
    return query


# ---------------------------------------------------------------------------
# แปลงค่าให้เขียน CSV ได้
# ---------------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    return to_text(value)


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, memoryview):
        return f"<binary {value.nbytes} bytes>"
    if isinstance(value, (bytes, bytearray)):
        return f"<binary {len(value)} bytes>"
    if isinstance(value, (dict, list, tuple)):  # json/jsonb และ array
        return json.dumps(value, ensure_ascii=False, default=_json_default)
    if isinstance(value, (str, int, float, Decimal)):
        return str(value)
    return str(value)  # UUID, inet, range, ชนิดอื่น ๆ


# ---------------------------------------------------------------------------
# ส่งออกและแสดงผล
# ---------------------------------------------------------------------------

def safe_filename_part(name: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in "_-." or unicodedata.category(ch).startswith("M") else "_"
        for ch in name
    ).strip(".")
    return cleaned or "_"


def output_path(table: TableInfo) -> Path:
    filename = f"{safe_filename_part(table.schema)}_{safe_filename_part(table.name)}_sample.csv"
    return OUTPUT_DIR / filename


def write_csv(path: Path, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:  # BOM สำหรับ Excel
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([to_text(v) for v in row])


def display_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text: str, width: int) -> str:
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if display_width(text) > width:
        out = ""
        for ch in text:
            if display_width(out + ch) > width - 1:
                break
            out += ch
        text = out + "…"
    return text + " " * (width - display_width(text))


def print_preview(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    preview = [["NULL" if v is None else to_text(v) for v in row] for row in rows[:PREVIEW_ROWS]]
    widths = []
    for i, h in enumerate(headers):
        cells = [h] + [r[i] for r in preview]
        widths.append(min(PREVIEW_CELL_WIDTH, max(display_width(c) for c in cells)))
    print(" | ".join(fit(h, w) for h, w in zip(headers, widths)))
    print("-+-".join("-" * w for w in widths))
    for row in preview:
        print(" | ".join(fit(c, w) for c, w in zip(row, widths)))


def fetch_rows(
    conn: psycopg.Connection,
    table: TableInfo,
    columns: Sequence[ColumnInfo],
    order_cols: Sequence[str],
    limit: int,
    where: Optional[sql.Composable] = None,
    params: Optional[Sequence[Any]] = None,
    descending: bool = False,
) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    query = build_sample_query(table, columns, order_cols, limit, where, descending)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [d.name for d in cur.description or []], cur.fetchall()


def describe_db_error(exc: psycopg.Error, cfg: Config, label: str) -> str:
    if isinstance(exc, errors.InsufficientPrivilege):
        return (f"บัญชีนี้ไม่มีสิทธิ์อ่าน {label} — โปรดเลือกตารางอื่น\n"
                "(โปรแกรมจะไม่ขอหรือเพิ่มสิทธิ์ใด ๆ ให้)")
    if isinstance(exc, errors.QueryCanceled):
        return (f"คำสั่งใช้เวลาเกิน {cfg.statement_timeout_ms} ms (statement timeout)\n"
                "ลองเลือกตารางอื่น หรือปรับ DB_STATEMENT_TIMEOUT_MS ใน .env")
    return f"อ่าน {label} ไม่สำเร็จ: {str(exc).strip()}"


def report_db_error(exc: psycopg.Error, cfg: Config, label: str) -> None:
    print("\n[ERROR] " + describe_db_error(exc, cfg, label).replace("\n", "\n        "))


def save_and_show(
    table: TableInfo,
    columns: Sequence[ColumnInfo],
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    order_note: str,
    sample_note: str,
) -> None:
    path = output_path(table)
    write_csv(path, headers, rows)

    print(f"\nตาราง: {table.label} ({table.kind})")
    print(order_note)
    print(f"หมายเหตุ: {sample_note}")
    binary_cols = [c.name for c in columns if c.binary_kind]
    if binary_cols:
        print(f"คอลัมน์ binary ถูกแทนด้วยขนาดข้อมูล: {', '.join(binary_cols)}")
    print(f"บันทึกแล้ว: {path.relative_to(PROJECT_DIR)}\n")

    if rows:
        print(f"ตัวอย่าง {min(len(rows), PREVIEW_ROWS)} แถวแรก:")
        print_preview(headers, rows)
    else:
        print("ไม่มีข้อมูล (CSV มีเฉพาะหัวคอลัมน์)")
    print()


def export_sample(conn: psycopg.Connection, cfg: Config, table: TableInfo) -> bool:
    """คืนค่า True เมื่อส่งออกสำเร็จ"""
    try:
        columns = get_columns(conn, table)
        pk_cols = get_primary_key(conn, table)
        headers, rows = fetch_rows(conn, table, columns, pk_cols, cfg.sample_rows)
    except psycopg.OperationalError:
        raise
    except psycopg.Error as exc:
        report_db_error(exc, cfg, table.label)
        return False
    finally:
        if not conn.closed:
            conn.rollback()

    if pk_cols:
        order_note = f"เรียงตาม primary key: {', '.join(pk_cols)}"
    else:
        order_note = "ไม่มี primary key — ลำดับแถวขึ้นกับฐานข้อมูล ไม่รับประกันว่าจะเหมือนเดิมทุกครั้ง"
    save_and_show(
        table, columns, headers, rows, order_note,
        f"นี่คือข้อมูลตัวอย่าง {len(rows)} แถวแรก (สูงสุด {cfg.sample_rows}) ไม่ใช่การสุ่ม",
    )
    return True


def _existing(columns: Sequence[ColumnInfo], wanted: Sequence[str]) -> List[str]:
    names = {c.name for c in columns}
    return [w for w in wanted if w in names]


class RelatedError(Exception):
    pass


@dataclass
class SampleResult:
    table: TableInfo
    columns: List[ColumnInfo]
    headers: List[str]
    rows: List[Tuple[Any, ...]]
    order_note: str
    sample_note: str


def collect_related(
    conn: psycopg.Connection, cfg: Config, tables: Sequence[TableInfo], n_master: int
) -> Tuple[List[SampleResult], List[str]]:
    """ดึง voc_master ล่าสุด n_master เรื่อง แล้วดึงตารางลูกเฉพาะแถวของเรื่องชุดเดียวกัน

    คืนค่า (ผลลัพธ์, คำเตือน) — ข้อผิดพลาดของ psycopg ส่งต่อให้ผู้เรียกจัดการ
    """
    warnings: List[str] = []
    by_label = {t.label: t for t in tables}
    root = by_label.get(RELATED_ROOT)
    if root is None:
        raise RelatedError(f"บัญชีนี้ไม่มีสิทธิ์อ่าน {RELATED_ROOT} จึงสร้างชุดตัวอย่างที่สัมพันธ์กันไม่ได้")
    children = []
    for label in RELATED_CHILDREN:
        if label in by_label:
            children.append(by_label[label])
        else:
            warnings.append(f"ไม่มีสิทธิ์อ่าน {label} — ข้ามตารางนี้")

    results: List[SampleResult] = []
    try:
        root_cols = get_columns(conn, root)
        if "id" not in {c.name for c in root_cols}:
            raise RelatedError(f"{root.label} ไม่มีคอลัมน์ id")
        where = None
        if "is_deleted" in {c.name for c in root_cols}:
            where = sql.SQL("src.is_deleted IS NOT TRUE")
        order = _existing(root_cols, ["created_at", "id"])
        headers, rows = fetch_rows(
            conn, root, root_cols, order, n_master, where=where, descending=True
        )
        results.append(SampleResult(
            root, root_cols, headers, rows,
            f"เรียงตาม {', '.join(order)} จากใหม่ไปเก่า (ไม่รวมเรื่องที่ถูกลบ)",
            f"เรื่อง VOC ล่าสุด {len(rows)} เรื่อง (ขอ {n_master}) ไม่ใช่การสุ่ม",
        ))
        ids = [str(r[headers.index("id")]) for r in rows]

        for child in children:
            cols = get_columns(conn, child)
            fk = next((c for c in cols if c.name == RELATED_FK), None)
            if fk is None:
                warnings.append(f"{child.label} ไม่มีคอลัมน์ {RELATED_FK} — ข้ามตารางนี้")
                continue
            # cast พารามิเตอร์ให้ตรงชนิดคอลัมน์ เพื่อให้ใช้ index ได้
            child_where = sql.SQL("src.{fk} = ANY({p}::{t}[])").format(
                fk=sql.Identifier(RELATED_FK), p=sql.Placeholder(), t=sql.SQL(fk.type_name)
            )
            order = _existing(cols, [RELATED_FK, "created_at", "id"])
            headers, rows = fetch_rows(
                conn, child, cols, order, cfg.sample_rows, where=child_where, params=(ids,)
            )
            note = f"เฉพาะแถวของ {len(ids)} เรื่องข้างต้น ได้ {len(rows)} แถว"
            if len(rows) >= cfg.sample_rows:
                note += f" — ถึงขีดจำกัด {cfg.sample_rows} แถว ข้อมูลของบางเรื่องอาจไม่ครบ (ลดจำนวนเรื่อง)"
                warnings.append(f"{child.label} ถึงขีดจำกัด {cfg.sample_rows} แถว ข้อมูลของบางเรื่องอาจไม่ครบ")
            results.append(SampleResult(child, cols, headers, rows, f"เรียงตาม {', '.join(order)}", note))
    finally:
        if not conn.closed:
            conn.rollback()
    return results, warnings


def export_related(conn: psycopg.Connection, cfg: Config, tables: Sequence[TableInfo]) -> bool:
    try:
        results, warnings = collect_related(conn, cfg, tables, cfg.related_rows)
    except RelatedError as exc:
        print(f"\n[ERROR] {exc}")
        return False
    except psycopg.OperationalError:
        raise
    except psycopg.Error as exc:
        report_db_error(exc, cfg, "ชุดตัวอย่าง VOC")
        return False
    for w in warnings:
        print(f"[คำเตือน] {w}")

    # เขียนไฟล์หลังจากดึงครบทุกตารางแล้วเท่านั้น
    for r in results:
        save_and_show(r.table, r.columns, r.headers, r.rows, r.order_note, r.sample_note)
    print("ไฟล์ข้างต้นเป็นชุดเดียวกัน join กันได้ด้วย voc_master.id = voc_master_id")
    print("รัน python build_dev_db.py เพื่ออัปเดต dev.db\n")
    return True


def print_tables(tables: Sequence[TableInfo], denied: int) -> None:
    print(f"\nตารางใน schema {', '.join(SCHEMAS)} ที่บัญชีนี้มีสิทธิ์ SELECT ({len(tables)} รายการ):")
    num_width = len(str(len(tables)))
    for i, t in enumerate(tables, 1):
        kind = "" if t.kind == "table" else f"  [{t.kind}]"
        print(f"  {i:>{num_width}}. {t.label}{kind}")
    if denied:
        print(f"  (มีอีก {denied} รายการที่ไม่มีสิทธิ์ SELECT จึงไม่แสดง)")


def choose_table(tables: Sequence[TableInfo], denied: int) -> Union[TableInfo, str, None]:
    by_label = {t.label: t for t in tables}
    while True:
        answer = input(
            "\nเลือกหมายเลข หรือพิมพ์ schema.table"
            " (v = ชุดตัวอย่าง VOC ที่สัมพันธ์กัน, l = แสดงรายการ, q = ออก): "
        ).strip()
        if answer.lower() in ("q", "quit", "exit"):
            return None
        if answer.lower() == "v":
            return "related"
        if answer.lower() == "l":
            print_tables(tables, denied)
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(tables):
            return tables[int(answer) - 1]
        if answer in by_label:
            return by_label[answer]
        print("ไม่พบตัวเลือกนี้ ลองใหม่อีกครั้ง")


def main() -> int:
    print("=== PEA VOC: ดึงข้อมูลตัวอย่างเป็น CSV (read-only) ===")
    print("คำเตือน: CSV อาจมีข้อมูลจริงขององค์กร เก็บไว้เฉพาะในพื้นที่ที่องค์กรอนุญาต\n")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[ERROR] {exc}")
        return 1

    conn = connect_interactive(cfg)
    if conn is None:
        return 1

    try:
        with conn:
            print("เชื่อมต่อสำเร็จ (session: read-only, "
                  f"statement_timeout={cfg.statement_timeout_ms} ms)")
            tables, denied = list_tables(conn)
            if not tables:
                print("ไม่พบตารางที่บัญชีนี้มีสิทธิ์ SELECT ใน schema ที่กำหนด")
                return 1
            print_tables(tables, denied)
            while True:
                table = choose_table(tables, denied)
                if table is None:
                    break
                if table == "related":
                    ok = export_related(conn, cfg, tables)
                else:
                    ok = export_sample(conn, cfg, table)
                if not ok:
                    continue  # ไม่มีสิทธิ์หรือ error ให้เลือกตารางอื่น
                again = input("ต้องการเลือกตารางอื่นต่อไหม? [y/N]: ").strip().lower()
                if again not in ("y", "yes"):
                    break
    except psycopg.OperationalError as exc:
        print(f"\n[ERROR] การเชื่อมต่อขาดระหว่างทำงาน: {str(exc).strip()}")
        print("ตรวจ VPN / SSH tunnel แล้วรันโปรแกรมใหม่")
        return 1
    print("จบการทำงาน")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nยกเลิกโดยผู้ใช้")
        sys.exit(130)
