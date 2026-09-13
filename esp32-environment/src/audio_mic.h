/*
 * 麦克风音频模块 — 头文件
 * =======================
 * INMP441 采集（I2S）+ VAD 语音门控 + PCM 流式上报到服务器。
 * 独立 audio_task 运行，与传感器采集、网络通信互不阻塞。
 */

#ifndef AUDIO_MIC_H
#define AUDIO_MIC_H

// 初始化 I2S 麦克风（setup 中调用一次）
void audioMicInit();

// 音频任务主体（FreeRTOS 任务函数，不返回）
void audioTask(void *param);

#endif // AUDIO_MIC_H