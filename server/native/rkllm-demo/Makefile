# 编译器
CXX = g++
CXXFLAGS = -std=c++11 -Wall -O2

# 目标文件名
TARGET = llm_demo
SRC = llm_demo.cpp

# 头文件路径
INC_DIR = ./

# 库文件路径
LIB_DIR = ./lib

# 链接库
LIBS = -lrkllmrt -lpthread -ldl
LDFLAGS = -L$(LIB_DIR) -Wl,-rpath,$(LIB_DIR)

# 默认目标
all: $(TARGET)

# lib/librkllmrt.so 由 RKLLM 官方 SDK 提供，体积大、不进 git（.gitignore 已排除），
# 新环境需先从 SDK 取回放到 lib/ 下；缺库时给出明确提示而不是让 ld 报 "cannot find -lrkllmrt"。
LIB_RKLLM = $(LIB_DIR)/librkllmrt.so

$(LIB_RKLLM):
	@echo "[ERROR] 缺少 $(LIB_RKLLM)"
	@echo "        该库来自 RKLLM 官方 SDK（体积大，未入 git），请先放入 $(LIB_DIR)/ 再 make。"
	@exit 1

# 编译
$(TARGET): $(SRC) $(LIB_RKLLM)
	$(CXX) $(CXXFLAGS) -I$(INC_DIR) -o $@ $< $(LDFLAGS) $(LIBS)
	@echo "[OK] llm_demo 编译完成"

# 清理
clean:
	rm -f $(TARGET)

# 重新编译
rebuild: clean all

.PHONY: all clean rebuild
