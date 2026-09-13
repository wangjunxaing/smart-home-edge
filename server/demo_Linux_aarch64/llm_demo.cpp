// Copyright (c) 2024 by Rockchip Electronics Co., Ltd. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <string.h>
#include <unistd.h>
#include <string>
#include "rkllm.h"
#include <fstream>
#include <iostream>
#include <csignal>
#include <vector>
#include <sstream>

using namespace std;
LLMHandle llmHandle = nullptr;

// ========== 全局配置 ==========
bool enable_auto_clear = true;   // 是否每次提问前自动清除历史
bool enable_json_output = false; // 是否启用 JSON 格式输出（用于智能家居控制）
string system_prompt_content = ""; // 存储系统提示词

// ========== 信号处理 ==========
void exit_handler(int signal)
{
    if (llmHandle != nullptr)
    {
        {
            cout << "程序即将退出" << endl;
            LLMHandle _tmp = llmHandle;
            llmHandle = nullptr;
            rkllm_destroy(_tmp);
        }
    }
    exit(signal);
}

// ========== 回调函数 ==========
void callback(RKLLMResult *result, void *userdata, LLMCallState state)
{
    if (state == RKLLM_RUN_FINISH)
    {
        printf("\n");
    } else if (state == RKLLM_RUN_ERROR) {
        printf("\\run error\n");
    } else if (state == RKLLM_RUN_NORMAL) {
        if (result->last_hidden_layer.embd_size != 0 && result->last_hidden_layer.num_tokens != 0) {
            int data_size = result->last_hidden_layer.embd_size * result->last_hidden_layer.num_tokens * sizeof(float);
            printf("\ndata_size:%d",data_size);
            std::ofstream outFile("last_hidden_layer.bin", std::ios::binary);
            if (outFile.is_open()) {
                outFile.write(reinterpret_cast<const char*>(result->last_hidden_layer.hidden_states), data_size);
                outFile.close();
                std::cout << "Data saved to output.bin successfully!" << std::endl;
            } else {
                std::cerr << "Failed to open the file for writing!" << std::endl;
            }
        }
        printf("%s", result->text);
    }
}

// ========== 打印帮助信息 ==========
void print_help()
{
    cout << "\n========== 命令列表 ==========" << endl;
    cout << "  exit        - 退出程序" << endl;
    cout << "  clear       - 清除对话历史" << endl;
    cout << "  help        - 显示此帮助" << endl;
    cout << "  auto_clear  - 切换自动清除模式 (当前: " << (enable_auto_clear ? "ON" : "OFF") << ")" << endl;
    cout << "  json        - 切换 JSON 输出模式 (当前: " << (enable_json_output ? "ON" : "OFF") << ")" << endl;
    cout << "=============================\n" << endl;
}

// ========== 解析特殊命令 ==========
bool handle_special_command(const string& input)
{
    if (input == "help") {
        print_help();
        return true;
    }
    if (input == "auto_clear") {
        enable_auto_clear = !enable_auto_clear;
        cout << "[System] 自动清除模式: " << (enable_auto_clear ? "ON" : "OFF") << endl;
        return true;
    }
    if (input == "json") {
        enable_json_output = !enable_json_output;
        cout << "[System] JSON 输出模式: " << (enable_json_output ? "ON" : "OFF") << endl;
        return true;
    }
    return false;
}

// ========== 构建系统提示词（用于智能家居场景） ==========
string build_system_prompt()
{
    stringstream ss;
    ss << "你是一个智能家居助手，名叫小智。你的职责是帮助用户控制家居设备。\n";
    ss << "当用户表达需求时，你需要：\n";
    ss << "1. 理解用户的意图（开灯、关灯、调亮度、开空调、查询状态等）\n";
    ss << "2. 如果用户明确要求操作设备，直接给出操作指令\n";
    ss << "3. 如果用户描述环境状态（如'有点暗'），主动建议操作\n";
    ss << "4. 回答要简洁明了，不要有多余的闲聊\n";
    ss << "\n";
    ss << "你可以识别的设备：灯、空调、窗帘、风扇、电视\n";
    ss << "你可以识别的操作：开、关、调亮、调暗、调温、升降\n";
    ss << "\n";
    ss << "回答格式要求：\n";
    ss << "- 如果是操作指令，以『指令:』开头\n";
    ss << "- 如果是建议，以『建议:』开头\n";
    ss << "- 如果是普通问答，正常回复\n";
    return ss.str();
}

// ========== 设置 Chat Template ==========
void setup_chat_template()
{
    // 构建系统提示词
    system_prompt_content = build_system_prompt();
    
    // 使用 rkllm_set_chat_template 设置完整的模板
    // 参数: handle, system_prompt, prefix (user), postfix (assistant)
    // 
    // 注意：这里使用 Qwen 原生格式
    // system: <|im_start|>system\n{system_prompt}<|im_end|>\n
    // user:   <|im_start|>user\n{input}<|im_end|>\n
    // assistant: <|im_start|>assistant\n{output}<|im_end|>\n
    
    string system_with_format = "<|im_start|>system\n" + system_prompt_content + "<|im_end|>\n";
    string user_prefix = "<|im_start|>user\n";
    string assistant_postfix = "<|im_end|>\n<|im_start|>assistant\n";
    
    int ret = rkllm_set_chat_template(
        llmHandle,
        system_with_format.c_str(),      // system_prompt (包含格式)
        user_prefix.c_str(),              // prompt_prefix
        assistant_postfix.c_str()         // prompt_postfix
    );
    
    if (ret == 0) {
        cout << "[System] Chat template 设置成功" << endl;
        cout << "[System] System prompt: " << system_prompt_content.substr(0, 80) << "..." << endl;
    } else {
        cout << "[System] Chat template 设置失败，错误码: " << ret << endl;
        cout << "[System] 将使用模型默认模板" << endl;
    }
}

// ========== 主函数 ==========
int main(int argc, char **argv)
{
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0] << " model_path max_new_tokens max_context_len\n";
        return 1;
    }

    signal(SIGINT, exit_handler);
    printf("rkllm init start\n");

    // 设置参数及初始化
    RKLLMParam param = rkllm_createDefaultParam();
    param.model_path = argv[1];

    // 设置采样参数
    param.top_k = 1;
    param.top_p = 0.95;
    param.temperature = 0.6;      // 降低温度，让回答更确定、更稳定
    param.repeat_penalty = 1.1;
    param.frequency_penalty = 0.0;
    param.presence_penalty = 0.0;

    param.max_new_tokens = std::atoi(argv[2]);
    param.max_context_len = std::atoi(argv[3]);
    param.skip_special_token = true;
    param.extend_param.base_domain_id = 0;
    param.extend_param.embed_flash = 1;

    int ret = rkllm_init(&llmHandle, &param, callback);
    if (ret == 0){
        printf("rkllm init success\n");
    } else {
        printf("rkllm init failed\n");
        exit_handler(-1);
    }

    // ========== 设置 Chat Template（系统提示词） ==========
    setup_chat_template();

    cout << "[System] 智能家居助手模式已启用" << endl;
    print_help();

    RKLLMInput rkllm_input;
    RKLLMInferParam rkllm_infer_params;
    memset(&rkllm_infer_params, 0, sizeof(RKLLMInferParam));

    rkllm_infer_params.mode = RKLLM_INFER_GENERATE;
    rkllm_infer_params.keep_history = 0;  // 默认不保留历史
    
    while (true)
    {
        // ========== 每次循环自动清除 KV Cache（如果启用） ==========
        if (enable_auto_clear) {
            ret = rkllm_clear_kv_cache(llmHandle, 1);
            if (ret != 0) {
                // 静默失败，不影响主流程
            }
        }

        std::string input_str;
        printf("\n");
        printf("user: ");
        std::getline(std::cin, input_str);
        
        // 处理空输入
        if (input_str.empty()) {
            continue;
        }

        // 处理特殊命令
        if (handle_special_command(input_str)) {
            continue;
        }

        // 退出命令
        if (input_str == "exit") {
            break;
        }

        // 清除命令（手动）
        if (input_str == "clear") {
            ret = rkllm_clear_kv_cache(llmHandle, 1);
            if (ret != 0) {
                printf("clear kv cache failed!\n");
            } else {
                printf("kv cache cleared!\n");
            }
            continue;
        }

        // ========== 执行推理 ==========
        rkllm_input.input_type = RKLLM_INPUT_PROMPT;
        rkllm_input.prompt_input = (char *)input_str.c_str();
        printf("robot: ");

        ret = rkllm_run(llmHandle, &rkllm_input, &rkllm_infer_params, NULL);
        if (ret != 0) {
            printf("\n[Error] 推理失败，错误码: %d\n", ret);
            // 尝试重置
            rkllm_clear_kv_cache(llmHandle, 1);
        }
    }

    rkllm_destroy(llmHandle);
    return 0;
}