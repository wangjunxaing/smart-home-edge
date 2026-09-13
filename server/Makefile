# Makefile — Orange Pi 5 Pro (RK3588, aarch64) C/C++ 编译
# ================================================================
# 用法:
#   make              — 编译全部目标
#   make clean        — 清理编译产物
#   make llm_proxy    — 只编译 LLM PTY 代理
#   make sensor_history — 只编译传感器历史聚合器
#
# 交叉编译 (在 x86 主机上执行):
#   make CROSS=aarch64-linux-gnu-
#
# 本机编译 (在 Orange Pi 上执行):
#   make

CROSS   ?=
CC      = $(CROSS)gcc
CXX     = $(CROSS)g++
CFLAGS  = -O3 -march=armv8-a -mtune=cortex-a76 -Wall -Wextra
CXXFLAGS = -std=c++17 -O3 -march=armv8-a -mtune=cortex-a76 -Wall -Wextra
LDFLAGS_C = -lsqlite3 -lm
LDFLAGS_CXX = -lutil   # forkpty 依赖 -lutil

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
	@echo "[OK] 清理完成"
