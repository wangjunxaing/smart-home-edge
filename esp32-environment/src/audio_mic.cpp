/*
 * 麦克风音频模块 — INMP441 + VAD + PCM 流式上报
 * ==============================================
 * 接线（INMP441）: BCK->GPIO26  WS->GPIO25  SD->GPIO35  L/R->GND  VCC->3V3
 *
 * 原代码问题复盘与本版修复:
 *   1. 原设计最长录音 15 秒需 480KB 缓冲，远超 ESP32 可用 RAM（约 300KB），
 *      堆耗尽必崩 —— 本版改为 1 秒定长 PCM 包（32KB）边录边传
 *   2. INMP441 输出 32 位槽中的 24 位数据，按 16 位读取会错位/无声
 *      —— 本版按 32 位读取、右移 16 取高 16 位
 *   3. ESP32 老版 I2S 驱动的单声道（ONLY_LEFT）模式存在通道映射怪癖，
 *      表现为读数恒 0 —— 本版按立体声（RIGHT_LEFT）读取，启动时做
 *      通道自检：分别累计左右时隙能量，自动选用有信号的一个
 *   4. v1.4.2: 语音 RMS 仅约 400（满幅 32768），服务器 Vosk 难以识别
 *      —— 采样增益 x4（右移 14 代替 16），带饱和截幅保护
 *      v1.4.2: 风扇等稳态噪音越过固定门限造成误触发
 *      —— 底噪自适应滑动平均，门限 = max(300, 底噪 x3)，并限制最长录音 10 秒
 *
 * 工作流程:
 *   持续读取 20ms 帧 → 三特征 VAD（能量 RMS + 过零率 ZCR + 低频占比）
 *   → 连续 5 帧语音触发录音（带 0.5 秒预滚，保住唤醒词首字）
 *   → 每攒满 1 秒 POST 一次原始 PCM 到 /api/mic/stream（服务器 Vosk 识别）
 *   → 静音超过 1.5 秒结束本轮，尾部 >= 300ms 也上传
 *   静音期间不上传（服务器零负载）。
 */
#include "audio_mic.h"
#include "app_config.h"
#include "task_monitor.h"
#include "wifi_manager.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <driver/i2s.h>
#include <esp_task_wdt.h>

#if MIC_STREAM_ENABLED

// ===================== 硬件与采样参数 =====================
#define I2S_PORT        I2S_NUM_0
#define I2S_BCK_PIN     26
#define I2S_WS_PIN      25
#define I2S_DATA_PIN    35
#define SAMPLE_RATE     16000
#define FRAME_MS        20
#define FRAME_SAMPLES   (SAMPLE_RATE * FRAME_MS / 1000)   // 320 样本/帧（单声道）
#define CHUNK_FRAMES    50                 // 上传单元：50 帧 = 1 秒
#define PREROLL_FRAMES  25                 // 预滚：25 帧 = 0.5 秒

// ===================== VAD 参数 =====================
// ===== 2026-11-16 VAD 改进：高通 + 双门限滞回 =====
#define VAD_RMS_TRIGGER    500.0f   // 2026-11-16: 900 太高，1米正常说话(470~812)触发不了；高通后噪声仅35~86，500 仍安全
#define VAD_RMS_HOLD       350.0f   // 维持门限（录音中用，防漏检）
#define VAD_ZCR_MAX        0.35f    // 过零率上限（语音 < 0.3，噪声可能更高）
#define VAD_TRIGGER_FRAMES 4        // 连续 4 帧超触发门限才开录（80ms，抗瞬时噪声）
#define VAD_HANGOVER_MS    3000     // 2026-11-16: 3000ms 容忍说话中间停顿时长     // 2026-11-16: 1000 太短，说话中间停顿会被切句；改 1800
#define HPF_ALPHA          0.95f    // 高通系数（16kHz 下约压 200Hz 以下）
#define MAX_RECORD_FRAMES  500    // 最长连续录音 10 秒（50 帧/秒），防噪音霸占
#define AUDIO_DEBUG        1

// ===================== 缓冲区（静态分配）=====================
static int16_t chunkBuf[FRAME_SAMPLES * CHUNK_FRAMES];   // 32KB 上传缓冲
static int16_t ringBuf[FRAME_SAMPLES * PREROLL_FRAMES];  // 16KB 预滚环
static int ringPos = 0;
static WiFiClient audioClient;

// 通道自检状态：-1 自检中，0 = 左时隙，1 = 右时隙
static int channelSel = -1;

// ===================== 初始化 =====================
void audioMicInit() {
  const i2s_config_t i2s_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,  // INMP441: 32 位槽装 24 位数据
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,  // 立体声读取，避开单声道驱动怪癖
    .communication_format = (i2s_comm_format_t)(I2S_COMM_FORMAT_STAND_I2S),
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = FRAME_SAMPLES * 2,              // 每帧含左右两个槽
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };
  const i2s_pin_config_t pin_config = {
    .bck_io_num = I2S_BCK_PIN,
    .ws_io_num = I2S_WS_PIN,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_DATA_PIN
  };
  esp_err_t err = i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
  if (err == ESP_OK) err = i2s_set_pin(I2S_PORT, &pin_config);
  if (err == ESP_OK)
    err = i2s_set_clk(I2S_PORT, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_STEREO);
  Serial.printf("[音频] INMP441 初始化%s（16kHz/立体声读取，启动后通道自检）\n",
                err == ESP_OK ? "完成" : "失败");
}

// ===================== PCM 上传 =====================
static bool postPcmChunk(const int16_t *pcm, size_t samples) {
  if (!wifiConnected() || !resolveServerHost()) return false;
  HTTPClient http;
  http.begin(audioClient, makeURL("/api/mic/stream"));
  http.addHeader("Content-Type", "application/octet-stream");
  http.setTimeout(3000);
  int httpCode = http.POST((uint8_t *)pcm, samples * sizeof(int16_t));
  Serial.printf("[音频] POST /api/mic/stream %u 字节 -> %d\n",
                (unsigned)(samples * sizeof(int16_t)), httpCode);
  http.end();
  return httpCode == 200;
}

// 2026-11-16：句子结束标记（让服务器立即识别，不靠静音超时猜）
static void postEndMarker() {
  if (!wifiConnected() || !resolveServerHost()) return;
  HTTPClient http;
  http.begin(audioClient, makeURL("/api/mic/stream"));
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("X-Audio-End", "1");
  http.setTimeout(2000);
  int httpCode = http.POST((uint8_t *)nullptr, 0);
  Serial.printf("[音频] 发送结束标记 -> %d\n", httpCode);
  http.end();
}

// ===================== VAD 帧特征 =====================
static bool frameIsSpeech(const int16_t *frame, float vadRmsMin, float *outRms,
                          float *outZcr, float *outLow) {
  // 2026-11-16：high-pass 后的帧；判据 = 能量 + ZCR（弃用 lowRatio，实测失效）
  float sumSq = 0, lowSum = 0, totalSum = 0;
  int crossings = 0;
  for (int i = 0; i < FRAME_SAMPLES; i++)
    sumSq += (float)frame[i] * frame[i];
  for (int i = 1; i < FRAME_SAMPLES; i++) {
    if ((frame[i] >= 0) != (frame[i - 1] >= 0)) crossings++;
    int16_t low = (frame[i - 1] + frame[i]) / 2;
    lowSum += (float)low * low;
    totalSum += (float)frame[i] * frame[i];
  }
  float rms = sqrt(sumSq / FRAME_SAMPLES);
  float zcr = (float)crossings / (FRAME_SAMPLES - 1);
  float lowRatio = totalSum > 0 ? lowSum / totalSum : 0;
  *outRms = rms; *outZcr = zcr; *outLow = lowRatio;
  // 2026-11-16：弃用 lowRatio（语音/噪声都接近 1，无区分度），只用能量 + ZCR
  return (rms > vadRmsMin) && (zcr < VAD_ZCR_MAX);
}

// ===================== 音频任务 =====================
void audioTask(void *param) {
  (void)param;
  esp_task_wdt_add(NULL);

  int32_t raw[FRAME_SAMPLES * 2];          // 立体声：每帧 320 对槽
  int16_t frame[FRAME_SAMPLES];
  int speechRun = 0, silenceMs = 0, chunkPos = 0, dbgCount = 0, probeCount = 0;
  int recordFrames = 0;
  float noiseFloor = 60.0f;   // 底噪滑动估计（x4 增益后安静环境 RMS 约 130）
  unsigned long long probeL = 0, probeR = 0;
  bool recording = false;

  for (;;) {
    esp_task_wdt_reset();
    taskMonitorTick("audio");   // 5.10：周期抖动打点

    size_t bytesRead = 0;
    if (i2s_read(I2S_PORT, raw, sizeof(raw), &bytesRead, pdMS_TO_TICKS(60)) != ESP_OK
        || bytesRead < sizeof(raw))
      continue;

    // ---- 启动通道自检：累计 50 帧（1 秒）左右时隙能量，自动选通道 ----
    if (channelSel < 0) {
      for (int i = 0; i < FRAME_SAMPLES * 2; i += 2) {
        probeL += (unsigned long long)llabs((long long)raw[i]);
        probeR += (unsigned long long)llabs((long long)raw[i + 1]);
      }
      if (++probeCount >= 50) {
        channelSel = (probeR > probeL * 2) ? 1 : 0;
        Serial.printf("[音频] 通道自检: L=%llu R=%llu -> 使用%s时隙\n",
                      probeL, probeR, channelSel ? "右" : "左");
        if (probeL < 1000000 && probeR < 1000000)
          Serial.println("[音频] 警告: 两个时隙信号都极弱，请检查麦克风供电与 SD 接线");
      }
      continue;
    }

    // 提取所选时隙：右移 14 = x4 增益，饱和截幅防削波
    // 2026-11-16：加一阶高通滤波，压掉 ~200Hz 以下环境噪声（风扇/电源/空调），
    //             人声主要能量在 300Hz~3kHz，可比显著提升 SNR
    static float hpfPrevIn = 0, hpfPrevOut = 0;
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      int32_t v = raw[i * 2 + channelSel] >> 14;
      if (v > 32767) v = 32767;
      else if (v < -32768) v = -32768;
      // 一阶 IIR 高通：y[n] = a*(y[n-1] + x[n] - x[n-1])
      float y = HPF_ALPHA * (hpfPrevOut + (float)v - hpfPrevIn);
      hpfPrevIn = (float)v;
      hpfPrevOut = y;
      if (y > 32767) y = 32767;
      else if (y < -32768) y = -32768;
      frame[i] = (int16_t)y;
    }

    // 2026-11-16 双门限滞回：
    //   未录音 -> 用高门限触发（防环境噪声误触发）
    //   录音中 -> 用低门限维持（防语音被漏检导致提前结束）
    float vadThresh = recording ? VAD_RMS_HOLD : VAD_RMS_TRIGGER;
    // 底噪极低时可略微降低触发门限（但不能低于基线）
    if (!recording && noiseFloor < 100.0f) vadThresh = VAD_RMS_TRIGGER * 0.8f;
    if (vadThresh < VAD_RMS_HOLD) vadThresh = VAD_RMS_HOLD;

    float rms, zcr, lowRatio;
    bool isSpeech = frameIsSpeech(frame, vadThresh, &rms, &zcr, &lowRatio);

    // 2026-11-16 修复：录音中也更新底噪（否则阈值冻结 -> isSpeech 永真 -> 录音不结束）
    // 只用"明显低于当前阈值"的帧更新，避免语音把底噪抬高
    if (!isSpeech || rms < vadThresh * 0.8f)
      noiseFloor = noiseFloor * 0.997f + rms * 0.003f;

    memcpy(&ringBuf[ringPos * FRAME_SAMPLES], frame, FRAME_SAMPLES * sizeof(int16_t));
    ringPos = (ringPos + 1) % PREROLL_FRAMES;

    if (!recording) {
      speechRun = isSpeech ? speechRun + 1 : 0;
      if (speechRun >= VAD_TRIGGER_FRAMES) {
        recording = true;
        silenceMs = 0;
        chunkPos = 0;
        recordFrames = 0;
        for (int i = 0; i < PREROLL_FRAMES; i++) {
          int idx = (ringPos + i) % PREROLL_FRAMES;
          memcpy(&chunkBuf[chunkPos * FRAME_SAMPLES],
                 &ringBuf[idx * FRAME_SAMPLES], FRAME_SAMPLES * sizeof(int16_t));
          chunkPos++;
        }
        Serial.printf("[音频] 语音开始 (RMS=%.0f)\n", rms);
      }
    } else {
      memcpy(&chunkBuf[chunkPos * FRAME_SAMPLES], frame, FRAME_SAMPLES * sizeof(int16_t));
      chunkPos++;
      silenceMs = isSpeech ? 0 : silenceMs + FRAME_MS;

      if (chunkPos >= CHUNK_FRAMES) {
        postPcmChunk(chunkBuf, CHUNK_FRAMES * FRAME_SAMPLES);
        chunkPos = 0;
      }
      if (silenceMs >= VAD_HANGOVER_MS || ++recordFrames >= MAX_RECORD_FRAMES) {
        if (chunkPos >= 15)
          postPcmChunk(chunkBuf, chunkPos * FRAME_SAMPLES);
        chunkPos = 0;
        recording = false;
        speechRun = 0;
        Serial.println(silenceMs >= VAD_HANGOVER_MS
                       ? "[音频] 语音结束" : "[音频] 达到最长录音时长，强制结束");
        // 2026-11-16：通知服务器本句结束（立即识别）
        postEndMarker();
      }
    }

#if AUDIO_DEBUG
    if (++dbgCount >= 100) {
      dbgCount = 0;
      // 打印原始十六进制样本：若全 0 说明采集层无数据（接线/通道问题）
      Serial.printf("[音频] RMS=%.0f ZCR=%.2f LOW=%.2f floor=%.0f thr=%.0f\n",
                    rms, zcr, lowRatio, noiseFloor, vadThresh);
    }
#endif
  }
}

#else

void audioMicInit() { Serial.println("[音频] 麦克风已禁用（MIC_STREAM_ENABLED=0）"); }
void audioTask(void *param) { (void)param; for (;;) vTaskDelay(pdMS_TO_TICKS(1000)); }

#endif // MIC_STREAM_ENABLED