# Makefile — Orange Pi 5 Pro (RK3588, aarch64) C/C++ 组件
# ================================================================
# 用法:
#   make                # 编译全部
#   make llm_proxy      # 只编译本地 LLM PTY 代理
#   make sensor_history # 只编译历史数据聚合加速器
#   make clean          # 清理产物
#
# 交叉编译 (在 x86 主机上):
#   make CROSS=aarch64-linux-gnu-
#
# 产物 llm_proxy / sensor_history 直接输出到本目录（server/native/），
# 与 core/config.py 的 LLM_PROXY_BIN / SENSOR_HISTORY_BIN 对应。
# rkllm 推理 demo（llm_demo）在 rkllm-demo/ 下单独 make。

CROSS   ?=
CC      = $(CROSS)gcc
CXX     = $(CROSS)g++
CFLAGS  = -O3 -march=armv8-a -mtune=cortex-a76 -Wall -Wextra
CXXFLAGS = -std=c++17 -O3 -march=armv8-a -mtune=cortex-a76 -Wall -Wextra
LDFLAGS_C = -lsqlite3 -lm
LDFLAGS_CXX = -lutil   # forkpty 需要 -lutil

TARGETS = llm_proxy sensor_history

.PHONY: all clean

all: $(TARGETS)

llm_proxy: llm_proxy.cpp
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS_CXX)
	@echo "[OK] llm_proxy 编译完成"

sensor_history: sensor_history.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS_C)
	@echo "[OK] sensor_history 编译完成"

clean:
	rm -f $(TARGETS)
	@echo "[OK] 已清理"
