# 🍓 Track 2 · Edge AI ตรวจคุณภาพเมล็ดบน Raspberry Pi 5

**ผู้ดูแล:** Boss, Peach · ภาพรวมทั้งระบบดูที่ [docs/architecture.md](../docs/architecture.md)

## หน้าที่
ตรวจเมล็ดในโฟลเดอร์ภาพที่กรรมการให้ (ไม่ใช้กล้องสด) นับจำนวนเม็ดต่อคลาส และคำนวณสัดส่วนเมล็ดเสีย
ส่งผลตาม [สัญญาข้อมูล](../docs/data-contract.md#vision-track-2)

| รับเข้า | ส่งออก |
|:--|:--|
| โฟลเดอร์ภาพ + label 7 คลาสจากผู้จัด | `tgr/lot/{lot_id}/vision` และไฟล์ผลรวมของโฟลเดอร์ |

## โครงสร้าง
```text
track2-edge-ai/
├── configs/classes.yaml   ชื่อคลาส (แก้ที่นี่ที่เดียวเมื่อโจทย์จริงต่างจากสไลด์)
├── train/                 สคริปต์เทรนบน HPC
├── infer/                 batch_eval บน Pi: โฟลเดอร์ภาพ → JSON
└── models/                ที่วางไฟล์ .onnx ตอนรัน (ไม่ commit เข้า Git)
```

## เครื่องที่ใช้
| งาน | เครื่อง | หมายเหตุ |
|:--|:--|:--|
| เทรน | HPC ของมหาวิทยาลัย | วิธีเข้าใช้งานเก็บไว้นอก repo |
| รันจริง | Raspberry Pi 5 RAM 8 GB | CPU เท่านั้น ห้ามใช้ AI HAT |

## แนวทาง
1. **Day 1 Classification:** ตัดภาพเป็นรายเม็ด + augmentation เพราะข้อมูลมีแค่ 2–3 ภาพต่อคลาส
2. **Day 2 Detection/Segmentation:** หาเม็ดในภาพกอง (YOLO-seg) แล้วให้ classifier ตัดสินทีละเม็ด
3. **Day 3 Integration:** รวมเป็น pipeline เดียวบน Pi แล้ว export เป็น ONNX / NCNN
- วัดเวลาและ RAM บน Pi จริงทุกครั้ง ไม่เชื่อแค่ตัวเลขในสไลด์

## งานเตรียม (ดู [บอร์ด](../docs/sprint-board.csv))
T2-02 ตัดภาพรายเม็ด · T2-03 classifier · T2-04 JSON รูปแบบใหม่ · T2-05 YOLO-seg · T2-07 pipeline เทรนคำสั่งเดียว
