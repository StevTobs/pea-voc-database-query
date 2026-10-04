# PEA VOC — ดึงข้อมูลตัวอย่างเป็น CSV

โปรแกรม Python สำหรับดึงข้อมูลตัวอย่าง (ไม่เกิน 100 แถว) จากฐานข้อมูล PostgreSQL `pea_voc`
เพื่อศึกษาโครงสร้างข้อมูลก่อนพัฒนา dashboard

## ความปลอดภัย

- ใช้คำสั่ง **SELECT เท่านั้น** session ถูกตั้งเป็น read-only (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` + `BEGIN READ ONLY`) และมี `statement_timeout`
- โปรแกรมไม่ INSERT / UPDATE / DELETE ไม่เปลี่ยนโครงสร้างหรือสิทธิ์ ไม่อ่าน sequence และไม่ใช้ `pg_dump`
- **รหัสผ่านกรอกผ่าน Terminal ทุกครั้ง** (ไม่แสดงบนหน้าจอ) ไม่เก็บในโค้ด ในไฟล์ `.env` หรือใน log
- ชื่อ schema/table ประกอบด้วย `psycopg.sql.Identifier` ป้องกัน SQL injection
- ไม่ส่งข้อมูลไปยังบริการภายนอก
- `.env` และ `outputs/` ถูกใส่ใน `.gitignore` แล้ว
- **ไฟล์ CSV อาจมีข้อมูลจริงขององค์กร** เก็บไว้เฉพาะในพื้นที่ที่องค์กรอนุญาต และลบเมื่อไม่ใช้แล้ว

## ติดตั้ง (ครั้งแรก)

ต้องมี Python 3.9 ขึ้นไป เปิดโฟลเดอร์นี้ใน VS Code แล้วเปิด Terminal (`` Ctrl+` ``)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

จากนั้นใน VS Code กด `Cmd+Shift+P` → **Python: Select Interpreter** → เลือก `.venv`

## กรอกไฟล์ `.env`

| ตัวแปร | ความหมาย |
|---|---|
| `DB_HOST`, `DB_PORT` | ที่อยู่ที่เครื่องนี้เข้าถึงได้จริง (ดูด้านล่าง) **ต้องกรอก** |
| `DB_NAME` | ค่าเริ่มต้น `pea_voc` |
| `DB_USER` | ค่าเริ่มต้น `voc_user` |
| `DB_SSLMODE` | ค่าเริ่มต้น `prefer` ถ้ามีไฟล์ CA แนะนำ `verify-full` |
| `DB_SSLROOTCERT` | path ของไฟล์ CA (ถ้ามี) |
| `DB_STATEMENT_TIMEOUT_MS` | เวลาสูงสุดต่อคำสั่ง (ค่าเริ่มต้น 30000 ms) |
| `DB_SAMPLE_ROWS` | จำนวนแถวตัวอย่าง 1–100 |
| `DB_RELATED_ROWS` | จำนวนเรื่อง VOC สำหรับคำสั่ง `v` (ค่าเริ่มต้น 20) |

> ไม่ต้องใส่รหัสผ่านใน `.env` และอย่าใช้ port ชั่วคราวของ DBeaver (เช่น `127.0.0.1:43400`) เพราะจะเปลี่ยนทุกครั้งที่เปิด DBeaver

### กรณีต่อผ่าน VPN โดยตรง

```
DB_HOST=<IP ของ PostgreSQL>
DB_PORT=<port ของ PostgreSQL>
```

### กรณีต่อผ่าน SSH tunnel

เปิด Terminal อีกหน้าต่างแล้วรันคำสั่งนี้ทิ้งไว้ตลอดการใช้งาน (ระบบจะถามรหัสผ่าน SSH)

```bash
ssh -N -L 15432:<DB_HOST_ภายใน>:<DB_PORT_ภายใน> <ssh_user>@<ssh_host> -p 22
```

แล้วกรอก `.env` เป็น

```
DB_HOST=127.0.0.1
DB_PORT=15432
```

(เลือก local port อื่นแทน `15432` ได้ ถ้าถูกใช้งานอยู่)

## รันโปรแกรม

```bash
source .venv/bin/activate
python sample_export.py
```

หรือใน VS Code ไปที่ **Run and Debug** → เลือก **Sample export (CSV)** → กด ▶

ขั้นตอนการทำงาน:

1. กรอกรหัสผ่านของ `voc_user` (พิมพ์แล้วจะไม่แสดง) ผิดได้ 3 ครั้ง
2. โปรแกรมแสดงรายชื่อตารางใน schema `public` และ `logs` ที่บัญชีนี้มีสิทธิ์ SELECT
3. พิมพ์หมายเลข หรือ `schema.table` (`v` = ชุดตัวอย่าง VOC ที่สัมพันธ์กัน, `l` = แสดงรายการอีกครั้ง, `q` = ออก)
4. ไฟล์ถูกบันทึกที่ `outputs/<schema>_<table>_sample.csv` และแสดงตัวอย่างไม่เกิน 10 แถว

### ชุดตัวอย่าง VOC ที่สัมพันธ์กัน (คำสั่ง `v`)

การเลือกทีละตารางจะได้ "100 แถวแรก" ของแต่ละตารางแยกกัน ซึ่งมักเป็นคนละเรื่องจึง join กันไม่ได้
คำสั่ง `v` จะดึงชุดที่เชื่อมกันได้แทน:

1. `voc_master` เรื่องล่าสุด N เรื่อง (`DB_RELATED_ROWS` ค่าเริ่มต้น 20, ไม่รวมเรื่องที่ถูกลบ)
2. `voc_detail` และ `voc_tracking` เฉพาะแถวของเรื่องเหล่านั้น (ไม่เกิน 100 แถวต่อไฟล์)

ไฟล์ทั้งสามจะเขียนทับไฟล์เดิมใน `outputs/` จากนั้นรัน `python build_dev_db.py` เพื่ออัปเดต dev.db
ถ้าขึ้นเตือนว่าถึงขีดจำกัด 100 แถว ให้ลด `DB_RELATED_ROWS`

## รูปแบบข้อมูลใน CSV

- เข้ารหัส UTF-8 พร้อม BOM เปิดใน Excel แล้วภาษาไทยไม่เพี้ยน
- เป็น **ข้อมูลตัวอย่าง N แถวแรก ไม่ใช่การสุ่ม** เรียงตาม primary key ถ้ามี (ถ้าไม่มี ลำดับขึ้นกับฐานข้อมูล)
- `NULL` → ช่องว่าง (ในตัวอย่างบน Terminal แสดงเป็น `NULL`)
- วันเวลา → ISO 8601 เช่น `2025-01-31T08:30:00+07:00` ค่าพิเศษเช่น `infinity` คงเป็นข้อความ
- JSON / JSONB / array → ข้อความ JSON (ภาษาไทยคงเดิม)
- `bytea` → ไม่ดึงเนื้อหาจริง แสดงเป็น `<binary N bytes>`
- UUID, inet, numeric, range ฯลฯ → ข้อความ

## แก้ปัญหา

| อาการ | ตรวจสอบ |
|---|---|
| timeout / could not translate host name | ต่อ VPN แล้วหรือยัง, `DB_HOST` ถูกต้องไหม |
| connection refused | SSH tunnel ยังเปิดอยู่ไหม, `DB_PORT` ตรงกับ local port ใน `-L` ไหม |
| SSL / certificate | ขอ `sslmode` และไฟล์ CA ที่ถูกต้องจากผู้ดูแลระบบ (โปรแกรมไม่ปิด SSL ให้อัตโนมัติ) |
| password authentication failed | ตรวจชื่อผู้ใช้/รหัสผ่าน |
| ไม่มีสิทธิ์อ่านตาราง | เลือกตารางอื่น หรือติดต่อผู้ดูแลฐานข้อมูล (โปรแกรมไม่เพิ่มสิทธิ์เอง) |
| statement timeout | ตารางหรือ view ช้า ลองตารางอื่นหรือเพิ่ม `DB_STATEMENT_TIMEOUT_MS` |

## ฐานข้อมูลจำลองสำหรับพัฒนา (dev.db)

สร้างฐานข้อมูล SQLite บนเครื่องจากไฟล์ CSV ใน `outputs/` (ไม่ต้องเชื่อมต่อฐานข้อมูลจริง)

```bash
python build_dev_db.py
```

- ตัดคอลัมน์ข้อมูลส่วนบุคคลและข้อความอิสระออกอัตโนมัติ (ชื่อ, เลขบัตร, เบอร์, อีเมล, ที่อยู่, รายละเอียดเรื่อง) และข้ามตาราง users / contacts / files / logs
- สร้างใหม่ทั้งหมดทุกครั้งที่รัน ตาราง `public.*` ใช้ชื่อเดิม
- เปิดดูใน VS Code ด้วยส่วนขยาย เช่น *SQLite Viewer* หรือใช้คำสั่ง `sqlite3 dev.db`
- มี view `v_voc_overview` (1 แถว = 1 เรื่อง พร้อมชื่อสถานะ/ประเด็น/ช่องทาง และ `days_to_complete`) ไว้ใช้เป็นแหล่งข้อมูลของ dashboard
- เป็นข้อมูลตัวอย่างเท่านั้น ตัวเลขสรุปไม่สะท้อนข้อมูลจริง

## เว็บแอป Data Viewer (app.py)

แอปบนเครื่องสำหรับเชื่อมต่อ SSH + ฐานข้อมูล ดูข้อมูลตัวอย่าง และบันทึก CSV ได้ในหน้าเดียว ไม่ต้องเปิด tunnel เอง

### ความปลอดภัยของแอป
- เปิดได้เฉพาะ `http://localhost:8501` บนเครื่องนี้ เครื่องอื่นใน Wi-Fi เข้าไม่ได้ และปิดการส่งสถิติของ Streamlit
- ทำงานเฉพาะในเครือข่ายที่อนุญาต (`ALLOWED_GATEWAY_MACS` / `ALLOWED_WIFI_SSIDS`) ตรวจซ้ำก่อนทุกคำสั่ง ถ้าย้ายเครือข่ายจะตัดการเชื่อมต่อทันที
- SSH ตรวจ host key กับ `~/.ssh/known_hosts` เสมอ ไม่รับ key ใหม่อัตโนมัติ
- รหัสผ่าน SSH และฐานข้อมูลกรอกในแอปทุกครั้ง ใช้ตอนเชื่อมต่อแล้วทิ้ง ไม่บันทึก
- ค่าเริ่มต้นปกปิดข้อมูลส่วนบุคคล (ชื่อ เลขบัตร เบอร์ อีเมล ที่อยู่ ข้อความอิสระ) ทั้งบนจอและใน CSV ปิดได้ที่สวิตช์แถบซ้าย
- ยังเป็น SELECT อย่างเดียว session read-only ไม่เกิน 100 แถวต่อตาราง

### ตั้งค่า (ครั้งแรก)
1. ติดตั้ง package ใหม่: `pip install -r requirements.txt`
2. กรอกใน `.env`: `SSH_HOST`, `SSH_USER`, `SSH_REMOTE_DB_HOST`, `SSH_REMOTE_DB_PORT` (ดู `.env.example`)
3. กำหนดเครือข่ายที่อนุญาต: ต่อ Wi-Fi ที่องค์กรอนุญาต แล้วรัน `python netguard.py` จะแสดงบรรทัด `ALLOWED_GATEWAY_MACS=...` ให้คัดลอกไปใส่ `.env` (หลายเครือข่ายคั่นด้วย `,`)
4. ถ้ายังไม่เคย ssh เข้าเซิร์ฟเวอร์ ให้รัน `ssh <SSH_USER>@<SSH_HOST>` หนึ่งครั้งเพื่อยืนยัน host key

### เปิดแอป
- ดับเบิลคลิก `run_app.command` ใน Finder หรือ
- `streamlit run app.py` หรือ VS Code → Run and Debug → **Data Viewer app (Streamlit)**

แล้วกรอกรหัสผ่านที่แถบซ้าย → **เชื่อมต่อ** ใช้งานเสร็จกด **ตัดการเชื่อมต่อ** และปิดหน้าต่าง Terminal
