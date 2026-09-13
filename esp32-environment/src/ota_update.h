/*
 * OTA 固件升级模块 — 头文件（5.2 签名+回滚版）
 * ============================================
 * 可靠性设计（v2）:
 *   1) 服务器对固件 SHA-256 摘要做 RSA-2048 签名（私钥在服务器）；
 *      设备流式写盘同时计算 SHA-256，验摘要 + 验签名后才切换启动分区——防伪防篡改
 *   2) 双 OTA 分区（app0/app1）+ 应用级健康回滚：新固件启动后宽限期未确认健康、
 *      或连续异常重启超阈值 → 自动切回旧分区重启（无需 bootloader 回滚支持）
 *   3) 失败冷却 10 分钟，防循环下载刷写
 */

#ifndef OTA_UPDATE_H
#define OTA_UPDATE_H

// 是否应该尝试升级（升级中标志与失败冷却期）
bool otaShouldAttempt();

// 引导健康检查（setup 早期调用一次）：异常引导计数；超阈值自动回滚到旧分区并重启
void otaBootHealthCheck();

// 健康确认（net_task 周期调用，内部按毫秒节流）：新固件稳定运行超过宽限期 → 清除 pending
void otaHealthTick();

// 执行 OTA 升级（阻塞数秒~数十秒，验签通过后切换分区并自动重启，不返回）。
// path 为服务器相对路径（如 /firmware/file），version 为目标版本号（仅日志用）。
void otaPerform(const char *path, const char *version);

#endif // OTA_UPDATE_H
