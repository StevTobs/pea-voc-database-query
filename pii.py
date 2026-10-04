"""กฎระบุคอลัมน์ข้อมูลส่วนบุคคล/ข้อความอิสระ ใช้ร่วมกันระหว่าง build_dev_db.py และ app.py"""
from __future__ import annotations

import re
from typing import Dict, Set

# ตารางที่เป็นข้อมูลบุคคล ไฟล์แนบ หรือ log ที่อาจมี payload/token
SENSITIVE_TABLES: Set[str] = {
    "users", "user_roles", "user_depts", "voc_contacts", "voc_files",
    "request_acceptance_sms_history", "voc_opm_api_log", "sync_log",
}
SENSITIVE_SCHEMAS: Set[str] = {"logs"}

# คอลัมน์ที่ต้องปกปิดเฉพาะตาราง
SENSITIVE_COLUMNS: Dict[str, Set[str]] = {
    "voc_detail": {
        "first_name", "last_name", "citizen_id", "phone", "email",
        "user_number", "meter_number", "prefix",
        "contact_house_no", "contact_moo", "contact_village", "contact_road",
        "contact_soi", "contact_postal",
        "incident_house_no", "incident_moo", "incident_village", "incident_road",
        "incident_soi", "incident_full_address", "incident_lat", "incident_lng",
        "details", "event_description", "product_importance_input",
    },
    "voc_master": {"cancel_description"},
    "voc_tracking": {"detail", "customer_comment", "customer_reject_reason", "reviewed_reason"},
}

# คอลัมน์ในทุกตารางที่ชื่อตรงกับรูปแบบนี้
SENSITIVE_PATTERN = re.compile(
    r"(citizen|national_id|id_card|passport|phone|mobile|tel$|email|password|passwd|"
    r"token|secret|api_key|first_name|last_name|full_name|address|lat$|lng$|latitude|longitude)",
    re.IGNORECASE,
)


def is_sensitive_table(schema: str, table: str) -> bool:
    return schema in SENSITIVE_SCHEMAS or table in SENSITIVE_TABLES


def is_sensitive_column(table: str, column: str) -> bool:
    return column in SENSITIVE_COLUMNS.get(table, set()) or bool(SENSITIVE_PATTERN.search(column))
