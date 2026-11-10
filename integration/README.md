# 🧩 Integration · รวมข้อมูลล็อต

ใช้ในโจทย์ Integration วันที่ 19 พ.ย. และการสาธิตวันที่ 21 ภาพรวมดูที่ [docs/architecture.md](../docs/architecture.md)

## หน้าที่
รับ `moisture` (Track 1) และ `vision` (Track 2) ของล็อตเดียวกัน จับคู่ด้วย `lot_id` แล้วส่ง `sample` ต่อ
รูปแบบข้อมูลดู [docs/data-contract.md](../docs/data-contract.md)

## MQTT broker (บนโน้ตบุ๊กของทีม)
ผู้จัดไม่มี broker ให้ ทีมต้องตั้งเอง
```bash
brew install mosquitto
mosquitto -c integration/mosquitto.conf -v
```
ทดสอบรับและส่ง:
```bash
mosquitto_sub -h localhost -t 'tgr/lot/#' -v
mosquitto_pub -h localhost -t tgr/lot/LOT-01/moisture -f contracts/moisture.example.json
```

## สิ่งที่ยังต้องทำ
- ตัวรวมข้อมูล (fusion) ที่ subscribe `tgr/lot/+/moisture` และ `tgr/lot/+/vision` แล้ว publish `tgr/lot/{lot_id}/sample`
- ตรวจในห้องแข่งว่า ESP32 และ Pi ต่อ broker บนโน้ตบุ๊กผ่านเครือข่ายของงานได้หรือไม่
