/*
 * WiFi 多网络配置文件（模板）
 * ==========================
 * 使用方法:
 *   1. 复制本文件并重命名为 wifi_config.h（与本文件同目录）
 *   2. 在 WIFI_CREDENTIALS 中填入真实的 WiFi 名称和密码
 *
 * 注意: wifi_config.h 含有真实密码，已被 .gitignore 排除，不会提交到仓库。
 *
 * 连接策略:
 *   1. 启动时按数组顺序逐个尝试连接
 *   2. 连接成功后，本次运行期间不再切换
 *   3. 重启后重新从第一个网络开始尝试
 *   4. 每个网络最多重试 WIFI_MAX_ATTEMPTS 次
 *   5. 单次尝试等待 WIFI_ATTEMPT_TIMEOUT 毫秒
*/

#ifndef WIFI_CONFIG_H
#define WIFI_CONFIG_H

// ===================== WiFi 凭证列表 =====================
// 每个条目: {SSID (WiFi名称), 密码}
// 修改后无需改 WIFI_NETWORK_COUNT，由 sizeof 自动计算
const char *WIFI_CREDENTIALS[][2] = {
    {"your_wifi_name_1", "your_wifi_password_1"},
    {"your_wifi_name_2", "your_wifi_password_2"},
    // 在此添加更多网络...
};

// 自动计算网络数量（数组总大小 / 单个元素大小）
#define WIFI_NETWORK_COUNT (sizeof(WIFI_CREDENTIALS) / sizeof(WIFI_CREDENTIALS[0]))

// 每个 WiFi 的最大重试次数（单网络最多等待 3×5 = 15 秒）
#define WIFI_MAX_ATTEMPTS 3

// 每次尝试等待时间（毫秒），每 500ms 检查一次连接状态
#define WIFI_ATTEMPT_TIMEOUT 5000

#endif // WIFI_CONFIG_H
