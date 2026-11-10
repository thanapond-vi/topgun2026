# 🔌 Track 1 · ESP32 วัดความชื้นเมล็ด

**ผู้ดูแล:** June, Jeff · ภาพรวมทั้งระบบดูที่ [docs/architecture.md](../docs/architecture.md)

## หน้าที่
อ่านค่าจากเซ็นเซอร์ความชื้นที่ประกอบเองจากชิ้นส่วนที่ผู้จัดให้ แปลงเป็น % แล้วส่งผ่าน Wi-Fi ตาม [สัญญาข้อมูล](../docs/data-contract.md#moisture-track-1)

| รับเข้า | ส่งออก |
|:--|:--|
| แรงดันจาก ADC | `tgr/lot/{lot_id}/moisture` ทุก ~3 วินาที |

## โครงสร้าง
```text
track1-esp32/
├── firmware/       โค้ด Arduino (.ino) ของ ESP32
└── calibration/    ข้อมูลวัดเทียบ + สมการแปลง ADC → %
```

## เริ่มใช้งาน
1. Arduino IDE → Settings → Additional boards manager URLs ใส่ `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. Boards Manager ติดตั้ง `esp32` (Espressif) → เลือกบอร์ดและพอร์ต
3. เปิด sketch ใน `firmware/` แล้ว Upload

## ทักษะที่ต้องพร้อม
Digital/Analog I/O · PWM · I2C · SPI · UART (Serial Monitor)

## ข้อควรระวัง
- ใช้พิน ADC1 เพราะ ADC2 ใช้ไม่ได้ขณะเปิด Wi-Fi
- ไฟเลี้ยงไม่พอทำให้ Wi-Fi ผิดปกติ
- ใช้บอร์ดจ่ายไฟแยก ต้องต่อ GND ร่วมกับ ESP32 ทุกครั้ง

## งานเตรียม (ดู [บอร์ด](../docs/sprint-board.csv))
T1-01 ส่งข้อความผ่าน MQTT · T1-02 อ่าน ADC เทียบ multimeter · T1-04 ต้นแบบเซ็นเซอร์ · T1-05 calibration
