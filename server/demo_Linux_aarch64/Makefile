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

# 编译
$(TARGET): $(SRC)
	$(CXX) $(CXXFLAGS) -I$(INC_DIR) -o $@ $^ $(LDFLAGS) $(LIBS)

# 清理
clean:
	rm -f $(TARGET)

# 重新编译
rebuild: clean all

.PHONY: all clean rebuild
