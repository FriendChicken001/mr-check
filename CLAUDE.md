# CLAUDE.md

คำแนะนำสำหรับ Claude Code เวลาทำงานในโปรเจกต์นี้

## โปรเจกต์นี้คืออะไร

Local web tool (ไม่มี build step, ไม่มี framework) สำหรับเช็คว่า branch ปลายทางบน
GitLab มี commit จากทุก MR ในไฟล์ CSV ครบหรือยัง ดูรายละเอียดการใช้งานใน `README.md`

## หลักการที่ต้องรักษาไว้ (อย่าเปลี่ยนโดยไม่ถามก่อน)

- **รันแบบ local เท่านั้น** — ห้าม deploy ขึ้น public URL, ห้ามเพิ่ม auth/hosting
  โดยไม่ถามผู้ใช้ก่อน เพราะการตัดสินใจตอนคุยกันคือให้เป็น local tool ที่แต่ละคนรันเอง
  บนเครื่องตัวเอง ไม่ใช่ web service ที่ deploy จริง — source code แจกกันผ่าน private
  git repo (clone แล้วรัน `python3 server.py` เอง) แทนการแจกโฟลเดอร์ตรงๆ แต่หลักการ
  ยังเหมือนเดิมคือรันบน `127.0.0.1` เท่านั้น
- **ไม่มี dependency ภายนอก** — `server.py` ใช้แค่ Python standard library
  (`http.server`, `urllib`, `csv`, `json`, `ssl`) จงใจไม่ใช้ Flask/FastAPI หรือ
  npm/Next.js เพราะต้องรันได้ทันทีด้วย `python3 server.py` บนเครื่อง macOS โดยไม่ต้อง
  ติดตั้งอะไรเพิ่ม
- **ห้าม log หรือเก็บ token ไว้ที่ไหน** — token ที่กรอกในหน้าเว็บต้องถูกใช้แค่ใน
  request เดียวแล้วทิ้ง ห้ามเขียนลงไฟล์ ห้าม print/log ค่า token
- **GitLab API เรียกจากฝั่ง server เท่านั้น** — ห้ามเปลี่ยนให้ browser ยิง GitLab API
  ตรง (จะเจอปัญหา CORS และ token เสี่ยงหลุดง่ายกว่า)

## โครงสร้างไฟล์

- `server.py` — backend: parse CSV, เรียก GitLab API, endpoint `POST /api/check`
- `index.html` — frontend ล้วน (vanilla JS, ไม่มี build)
- `README.md` — วิธีรันและใช้งานสำหรับผู้ใช้ทั่วไป

## รูปแบบ CSV ที่รองรับ

ต้องมี header row และมีคอลัมน์ชื่อ `MR` (case-insensitive) ที่เก็บ URL รูปแบบ
`https://<gitlab-host>/<group>/.../-/merge_requests/<iid>` ถ้าหา column `MR`
ไม่เจอ จะ fallback ไปใช้คอลัมน์ที่ 4 (index 3)

## การทดสอบเวลาแก้โค้ด

ไม่มี test suite อัตโนมัติ ให้ตรวจด้วยมือ:
```bash
python3 -m py_compile server.py   # เช็ค syntax error ของ backend
python3 server.py                 # รันแล้วเปิด http://127.0.0.1:8765 ทดสอบ flow จริง
```
