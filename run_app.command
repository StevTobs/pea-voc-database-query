#!/bin/zsh
# ดับเบิลคลิกใน Finder เพื่อเปิดแอป (ปิดหน้าต่างนี้ = ปิดแอปและ SSH tunnel)
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/streamlit ]; then
  echo "ยังไม่ได้ติดตั้ง — รัน: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  read -k1 "?กดปุ่มใดก็ได้เพื่อปิด"
  exit 1
fi
exec .venv/bin/streamlit run app.py
