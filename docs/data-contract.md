# 🔌 สัญญาข้อมูลระหว่าง Track

ทุก Track ส่งข้อมูลตามรูปแบบนี้ ถ้าต้องเปลี่ยน ให้คุยในทีมก่อนแล้วแก้ไฟล์นี้กับตัวอย่างใน `contracts/` พร้อมกัน

## กติการ่วม
- **ตัวเชื่อม:** `lot_id` ต้องเหมือนกันทุก Track สำหรับล็อตเดียวกัน
- **เวลา:** `ts` เป็น ISO 8601 เวลาไทย เช่น `2026-11-19T10:15:00+07:00`
- **หน่วย:** เปอร์เซ็นต์ใช้เลข 0–100 และชื่อช่องลงท้ายด้วย `_pct`
- **ค่าที่ยังไม่มี:** ใส่ `null` ห้ามละช่องทิ้ง
- **การส่ง:** MQTT QoS 1 ที่ broker บนโน้ตบุ๊ก พอร์ต 1883

## Topic
| Topic | ผู้ส่ง | ผู้รับ | ตัวอย่าง |
|:--|:--|:--|:--|
| `tgr/lot/{lot_id}/moisture` | Track 1 | Integration | [`contracts/moisture.example.json`](../contracts/moisture.example.json) |
| `tgr/lot/{lot_id}/vision` | Track 2 | Integration | [`contracts/vision.example.json`](../contracts/vision.example.json) |
| `tgr/lot/{lot_id}/sample` | Integration | Track 3 / dashboard | [`contracts/lot.example.json`](../contracts/lot.example.json) |

## moisture (Track 1)
| ช่อง | ชนิด | ความหมาย |
|:--|:--|:--|
| `lot_id` | string | รหัสล็อต |
| `source` | string | ชื่ออุปกรณ์ เช่น `esp32-01` |
| `ts` | string | เวลาที่วัด |
| `moisture_pct` | number | ความชื้นหลังแปลงด้วยสมการ calibration |
| `raw_adc` | integer | ค่า ADC ดิบ (เก็บไว้ตรวจย้อนหลัง) |

## vision (Track 2)
| ช่อง | ชนิด | ความหมาย |
|:--|:--|:--|
| `lot_id` | string | รหัสล็อต |
| `source` | string | ชื่ออุปกรณ์ เช่น `pi5-01` |
| `ts` | string | เวลาที่ประมวลผล |
| `counts` | object | จำนวนเม็ดต่อคลาส ชื่อคลาสตาม `track2-edge-ai/configs/classes.yaml` |
| `total` | integer | จำนวนเม็ดทั้งหมด |
| `defect_pct` | number | เม็ดบกพร่อง ÷ ทั้งหมด × 100 |
| `model` | string | ชื่อและรุ่นของโมเดล |
| `infer_ms` | number | เวลาประมวลผลต่อภาพ (มิลลิวินาที) |

## sample (Integration)
ชื่อช่องใน `sample` ตรงกับ `lots[].sample` ใน API ตลาดของผู้จัด จึงส่งเข้า `appraisal.true_value()` ได้ทันที

| ช่อง | ชนิด | มาจาก |
|:--|:--|:--|
| `lot_id` | string | ทั้งสอง Track |
| `sample.defect_pct` | number \| null | Track 2 |
| `sample.moisture_pct` | number \| null | Track 1 |
| `sample.screen` | integer \| null | ยังไม่มี Track ไหนวัด (ใส่ `null`) |
| `updated_at` | string | เวลาที่รวมล่าสุด |
