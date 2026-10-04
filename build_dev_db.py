"""สร้างฐานข้อมูลจำลอง SQLite (dev.db) จากไฟล์ CSV ใน outputs/ สำหรับพัฒนา dashboard

- ทำงานบนเครื่องเท่านั้น ไม่เชื่อมต่อฐานข้อมูลจริง
- ตัดคอลัมน์ข้อมูลส่วนบุคคลและข้อความอิสระออกก่อนโหลด
- ข้ามตารางที่เป็นข้อมูลบุคคลหรือ log ทั้งตาราง
- สร้างใหม่ทั้งหมดทุกครั้งที่รัน
"""
from __future__ import annotations

import csv
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from pii import is_sensitive_column, is_sensitive_table

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "outputs"
DB_PATH = PROJECT_DIR / "dev.db"

# view สำหรับพัฒนา dashboard: 1 แถว = 1 เรื่อง VOC พร้อมชื่อจากตาราง lookup
# voc_status มี status_en ซ้ำ จึงเลือกแถวที่ id น้อยที่สุดต่อ status_en เพื่อไม่ให้แถวเพิ่ม
VIEWS: Dict[str, str] = {
    "v_status_unique": """
        CREATE VIEW v_status_unique AS
        SELECT s.* FROM voc_status s
        JOIN (SELECT status_en, MIN(id) AS id FROM voc_status GROUP BY status_en) u
          ON u.id = s.id
    """,
    "v_voc_overview": """
        CREATE VIEW v_voc_overview AS
        SELECT m.id                         AS voc_master_id,
               m.voc_no,
               m.created_at,
               substr(m.created_at, 1, 7)   AS created_month,
               m.voc_type,
               m.status,
               s.status_th,
               s.group_status_th,
               i.name                       AS issue_name,
               d.id IS NOT NULL             AS has_detail,
               ch.name                      AS main_channel_name,
               d.severity_level,
               d.request_pea,
               d.accept_date,
               d.completed_date,
               ROUND(julianday(d.completed_date) - julianday(m.created_at), 2) AS days_to_complete,
               m.cancel_date IS NOT NULL    AS is_canceled
        FROM voc_master m
        LEFT JOIN v_status_unique s ON s.status_en = m.status
        LEFT JOIN voc_issues i      ON i.id = m.issue
        LEFT JOIN voc_detail d      ON d.voc_master_id = m.id
        LEFT JOIN voc_channels ch   ON ch.id = d.main_channel
        WHERE m.is_deleted IS NOT 1
    """,
}

INT_RE = re.compile(r"-?(0|[1-9]\d{0,17})")
REAL_RE = re.compile(r"-?\d+\.\d+")


def parse_filename(path: Path) -> Optional[tuple]:
    stem = path.stem
    if not stem.endswith("_sample"):
        return None
    schema, _, table = stem[: -len("_sample")].partition("_")
    return (schema, table) if table else None


def infer_type(values: Sequence[str]) -> str:
    present = [v for v in values if v != ""]
    if not present:
        return "TEXT"
    if all(v in ("true", "false") for v in present):
        return "BOOLEAN"
    # เลขที่ขึ้นต้นด้วย 0 (รหัสไปรษณีย์ รหัสหน่วยงาน) เก็บเป็น TEXT เพื่อไม่ให้ 0 หาย
    if all(INT_RE.fullmatch(v) for v in present):
        return "INTEGER"
    if all(INT_RE.fullmatch(v) or REAL_RE.fullmatch(v) for v in present):
        return "REAL"
    return "TEXT"


def convert(value: str, col_type: str):
    if value == "":
        return None
    if col_type == "BOOLEAN":
        return 1 if value == "true" else 0
    if col_type == "INTEGER":
        return int(value)
    if col_type == "REAL":
        return float(value)
    return value


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def load_table(conn: sqlite3.Connection, path: Path, schema: str, table: str) -> str:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return "ไฟล์ว่าง ข้าม"
    header, data = rows[0], rows[1:]

    keep_idx: List[int] = []
    dropped: List[str] = []
    for i, col in enumerate(header):
        if is_sensitive_column(table, col):
            dropped.append(col)
        else:
            keep_idx.append(i)

    columns = [header[i] for i in keep_idx]
    types = [infer_type([r[i] for r in data]) for i in keep_idx]
    id_unique = "id" in columns and len({r[header.index("id")] for r in data}) == len(data)

    name = table if schema == "public" else f"{schema}_{table}"
    col_defs = []
    for col, col_type in zip(columns, types):
        pk = " PRIMARY KEY" if col == "id" and id_unique else ""
        col_defs.append(f"{quote_ident(col)} {col_type}{pk}")
    conn.execute(f"CREATE TABLE {quote_ident(name)} ({', '.join(col_defs)})")

    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"INSERT INTO {quote_ident(name)} VALUES ({placeholders})",
        ([convert(r[i], t) for i, t in zip(keep_idx, types)] for r in data),
    )

    msg = f"{len(data)} แถว, {len(columns)} คอลัมน์"
    if dropped:
        msg += f" (ตัดออก {len(dropped)}: {', '.join(dropped)})"
    return msg


def main() -> int:
    files = sorted(OUTPUT_DIR.glob("*_sample.csv"))
    if not files:
        print("ไม่พบไฟล์ CSV ใน outputs/ — รัน sample_export.py ก่อน")
        return 1

    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    try:
        print(f"สร้าง {DB_PATH.name} จาก {len(files)} ไฟล์\n")
        for path in files:
            parsed = parse_filename(path)
            if not parsed:
                print(f"  - {path.name}: ชื่อไฟล์ไม่ตรงรูปแบบ ข้าม")
                continue
            schema, table = parsed
            if is_sensitive_table(schema, table):
                print(f"  - {schema}.{table}: ข้าม (ข้อมูลบุคคล/log)")
                continue
            print(f"  ✓ {schema}.{table}: {load_table(conn, path, schema, table)}")
        print()
        for name, ddl in VIEWS.items():
            try:
                conn.execute(ddl)
                conn.execute(f"SELECT 1 FROM {quote_ident(name)} LIMIT 1").fetchall()
                print(f"  ✓ view {name}")
            except sqlite3.Error as exc:
                conn.execute(f"DROP VIEW IF EXISTS {quote_ident(name)}")
                print(f"  - view {name}: สร้างไม่ได้ ({exc}) — อาจยังไม่ได้ export ตารางที่เกี่ยวข้อง")
        conn.commit()
    finally:
        conn.close()
    print(f"\nเสร็จแล้ว: {DB_PATH.name}  (ข้อมูลตัวอย่างเท่านั้น ตัวเลขสรุปไม่สะท้อนข้อมูลจริง)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
