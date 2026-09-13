/*
 * 传感器采集模块
 * ==============
 * 设计要点:
 *   - 支持部分降级：一个传感器坏了不影响另一个继续上报
 *   - 读取失败带重试与 I2C 总线恢复（Wire.end() → Wire.begin() 完整重置），
 *     这对长期运行的嵌入式设备至关重要——I2C 可能因静电、噪声进入异常状态
 *   - 无效数据用 -1.0 哨兵值标记，由 net_task 做异常分流
 *   - 采集周期读取共享配置 SEND_INTERVAL（服务器可动态下发），
 *     使用 vTaskDelayUntil 保证周期精度不受单次执行耗时影响
 */
#include "sensors_mod.h"
#include "app_config.h"
#include <Wire.h>
#include <WiFi.h>           // WiFi.RSSI()
#include <esp_timer.h>      // esp_timer_get_time() — t0_ms 采集完成打点
#include <BH1750.h>
#include <Adafruit_AHTX0.h>
#include <esp_task_wdt.h>

// ===================== 传感器对象与降级标志 =====================
static BH1750 lightMeter;
static Adafruit_AHTX0 aht;
static bool bh1750_ok = false;     // 初始化成功标志（失败则跳过读取）
static bool aht20_ok = false;

// ===================== INA219 电流采样（5.1 功耗实测） =====================
// 模块默认地址 0x40，共用 I2C 总线；常见模块采样电阻 0.1Ω。
// 只读分流电压寄存器（0x01，LSB=10µV）：I(mA) = Ushunt(µV) / (R_shunt(mΩ)) = µV/100
#define INA219_ADDR        0x40
#define INA219_REG_CONFIG  0x00
#define INA219_REG_SHUNT   0x01
#define INA219_SHUNT_MOHM  100.0f     // 0.1Ω = 100mΩ
static bool ina219_ok = false;

static bool ina219Write16(uint8_t reg, uint16_t val) {
  Wire.beginTransmission(INA219_ADDR);
  Wire.write(reg);
  Wire.write((uint8_t)(val >> 8));
  Wire.write((uint8_t)(val & 0xFF));
  return Wire.endTransmission() == 0;
}

static bool ina219Read16(uint8_t reg, int16_t *out) {
  Wire.beginTransmission(INA219_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)INA219_ADDR, 2) != 2) return false;
  uint8_t hi = Wire.read(), lo = Wire.read();
  *out = (int16_t)((hi << 8) | lo);
  return true;
}

static void ina219Init() {
  Wire.beginTransmission(INA219_ADDR);
  if (Wire.endTransmission() != 0) {
    Serial.println("[INA219] 未检测到（功耗上报停用）");
    ina219_ok = false;
    return;
  }
  // 0x399F：32V 量程 / ±320mV / 12bit / 连续采样
  ina219_ok = ina219Write16(INA219_REG_CONFIG, 0x399F);
  Serial.printf("[INA219] 初始化%s\n", ina219_ok ? "OK（0.1Ω 采样）" : "失败（降级运行）");
}

// 读取一次电流（mA）；无效返回 -1
static float ina219ReadCurrentMa() {
  if (!ina219_ok) return -1.0f;
  int16_t raw = 0;
  if (!ina219Read16(INA219_REG_SHUNT, &raw)) return -1.0f;
  float uV = (float)raw * 10.0f;              // LSB = 10µV
  float ma = uV / INA219_SHUNT_MOHM;          // I(mA) = U(µV)/R(mΩ)
  if (ma < 0.5f && ma > -0.5f) ma = 0.0f;     // 抑制零点噪声
  return ma;
}

// ===================== 初始化 =====================
void sensorsInit() {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(100000);           // 100kHz 标准 I2C 速率

  Serial.print("[BH1750] 初始化... ");
  if (lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE, 0x23, &Wire)) {
    bh1750_ok = true;
    Serial.println("OK");
  } else {
    Serial.println("失败（降级运行）");
  }

  Serial.print("[AHT20] 初始化... ");
  if (aht.begin(&Wire)) {
    aht20_ok = true;
    Serial.println("OK");
  } else {
    Serial.println("失败（降级运行）");
  }

  ina219Init();                    // 功耗测量（可选外设）
}

// ===================== I2C 总线恢复 =====================
// 读数持续失败时执行完整总线重置并重新初始化两个传感器
static void i2cBusRecover() {
  Wire.end();
  vTaskDelay(pdMS_TO_TICKS(100));
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(100000);
  if (bh1750_ok)
    lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE, 0x23, &Wire);
  if (aht20_ok)
    aht.begin(&Wire);
}

// ===================== 单次采集 =====================
static SensorReading readSensors() {
  SensorReading r;
  r.lux = -1.0;
  r.temperature = -1.0;
  r.humidity = -1.0;
  r.rssi = WiFi.RSSI();
  r.current_ma = ina219ReadCurrentMa();   // INA219（未接则 -1）

  // --- BH1750 光照（3 次重试 + 总线恢复）---
  if (bh1750_ok) {
    uint8_t att = 3;
    while (att--) {
      r.lux = lightMeter.readLightLevel();
      if (!isnan(r.lux) && r.lux >= 0) break;
      vTaskDelay(pdMS_TO_TICKS(50));
    }
    if (isnan(r.lux) || r.lux < 0) {
      r.lux = -1.0;
      Serial.println("[BH1750] 读取失败，尝试恢复I2C...");
      i2cBusRecover();
    }
  }

  // --- AHT20 温湿度（单次重试 + 总线恢复）---
  // 注意：te/he 未初始化，getEvent 失败时其内容是栈上残留（往往是上一次的成功
  // 读数！）——必须先判返回值再使用，否则失效数据会以"最后一次正常值"的形式上报
  if (aht20_ok) {
    sensors_event_t te, he;
    bool readOk = aht.getEvent(&he, &te);
    if (!readOk) {
      Serial.println("[AHT20] 读取失败，尝试恢复I2C...");
      i2cBusRecover();
      readOk = aht.getEvent(&he, &te);   // 重试一次
    }
    if (readOk) {
      r.temperature = isnan(te.temperature) ? -1.0 : te.temperature;
      r.humidity = isnan(he.relative_humidity) ? -1.0 : he.relative_humidity;
    } else {
      r.temperature = -1.0;
      r.humidity = -1.0;
      Serial.println("[AHT20] 重试仍失败，本轮温湿度标记无效");
    }
  }

  // --- 采集完成打点（论文时延测量：HTTP/MQTT 两模式随包上报 t0_ms）---
  r.t0_ms = (uint64_t)(esp_timer_get_time() / 1000);

  return r;
}

// ===================== 采集任务 =====================
// 周期读取传感器并写入 sensorQueue。
// 队列满时丢弃最旧的一条（保新不保旧，适合实时监控场景），
// 8 条缓冲也提供了断网期间的短暂数据暂存能力。
void sensorTask(void *param) {
  (void)param;
  esp_task_wdt_add(NULL);   // 挂入任务看门狗
  static uint32_t dropCount = 0;

  for (;;) {
    // 读取当前上报周期（持锁时间极短，不影响其他任务）
    unsigned long interval;
    if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
      interval = SEND_INTERVAL;
      xSemaphoreGive(configMutex);
    } else {
      interval = 5000;             // 取锁失败用默认值兜底
    }

    SensorReading r = readSensors();

    Serial.printf("[传感器] ID:%s 光照:%.1f lx 温度:%.1f C 湿度:%.1f %% 电流:%.1f mA\n",
                  ESP32_ID.c_str(), r.lux, r.temperature, r.humidity, r.current_ma);

    if (xQueueSend(sensorQueue, &r, 0) != pdTRUE) {
      // 队列满：丢弃最旧一条再写入，保证队列里永远是最新数据
      SensorReading stale;
      if (xQueueReceive(sensorQueue, &stale, 0) == pdTRUE) {
        xQueueSend(sensorQueue, &r, 0);
      }
      if ((++dropCount % 10) == 1)
        Serial.printf("[传感器] 队列满，丢弃旧数据（累计 %lu 次）\n", dropCount);
    }

    // 分片睡眠（每片 1 秒喂一次狗）：SEND_INTERVAL 最大 60 秒，
    // 整段睡眠会超过看门狗超时造成误判
    unsigned long slept = 0;
    while (slept < interval) {
      unsigned long slice = (interval - slept > 1000) ? 1000 : (interval - slept);
      vTaskDelay(pdMS_TO_TICKS(slice));
      slept += slice;
      esp_task_wdt_reset();
    }
  }
}