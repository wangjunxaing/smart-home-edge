/*
 * llm_proxy.cpp — C++ PTY 代理（本地 LLM 流式转发）
 * =================================================
 * 作用：把 Python 侧与 llm_demo 之间的 PTY 交互，封装为 Unix Domain Socket 接口：
 *   1. fork PTY 启动 llm_demo，等待其输出 "rkllm init success" 表示模型就绪
 *   2. 监听 Unix Domain Socket（默认 /tmp/llm_proxy.sock）
 *   3. 收到 Python 提问后写入 PTY，解析 demo 输出并逐段流式回传（token-by-token）
 *   4. 记录 user:/robot: 对话到 chat log（超过 50MB 自动轮转为 .old）
 *
 * 输出过滤规则（从 PTY 原始输出中挑出有效内容）:
 *   - 噪声: rkllm 初始化日志（I rkllm:, ----, Stage, Memory, Init, Prefill,
 *           Generate, Tokens）
 *   - 提问: 以 "user:" 开头的回显行
 *   - 回复: "robot:" 之后的内容（可跨多行，遇到下一个 user: 视为结束）
 *   - 噪声: 初始化阶段的中文提示行 / 以 "***" 开头的分隔行
 *
 * 协议（UDS，换行分隔的 JSON）:
 *   Python → Proxy:  {"cmd":"chat","msg":"..."}\n
 *   Python → Proxy:  {"cmd":"status"}\n
 *   Python → Proxy:  {"cmd":"restart"}\n
 *   Proxy → Python:  {"type":"token","text":"..."}\n
 *   Proxy → Python:  {"type":"done"}\n
 *   Proxy → Python:  {"type":"error","msg":"..."}\n
 *   Proxy → Python:  {"type":"status","ready":true|false}\n
 *
 * 编译: g++ -std=c++17 -O3 -o llm_proxy llm_proxy.cpp
 * 运行: ./llm_proxy <llm_demo_path> <model_path> <chat_log_path>
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>
#include <ctime>
#include <csignal>
#include <cerrno>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <sys/select.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <termios.h>
#include <pty.h>

/* ---- 配置常量 ---- */
static const char *SOCK_PATH   = "/tmp/llm_proxy.sock";
static const int   PTY_BUF_SZ  = 8192;
static const int   INIT_TIMEOUT = 60;
static const int   REPLY_TIMEOUT = 60;
static const int   REPLY_SILENCE = 3;   // 有输出后静默 3 秒视为回复结束（兜底）
static const long  MAX_CHAT_LOG_MB = 50;

/* ---- 全局状态 ---- */
static int   pty_fd     = -1;
static pid_t child_pid  = -1;
static bool  model_ready = false;
static FILE *chat_log_fp = nullptr;
static char  chat_log_path[512] = {0};

/* ---- 对话日志 ---- */

static void log_chat(const char *role, const char *text) {
    if (!chat_log_fp) return;
    time_t now = time(nullptr);
    struct tm tm_buf;
    localtime_r(&now, &tm_buf);
    char ts[32];
    strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm_buf);
    fprintf(chat_log_fp, "[%s] %s: %s\n", ts, role, text);
    fflush(chat_log_fp);
}

static void json_escape_and_write(int fd, const char *type, const char *text) {
    /* 组装 JSON 帧并写入（转义 " 与 \ 与换行，丢弃 \r） */
    char buf[PTY_BUF_SZ];
    char *p = buf;
    char *end = buf + sizeof(buf) - 4;
    p += snprintf(p, end - p, "{\"type\":\"%s\",\"text\":\"", type);
    for (const char *s = text; *s && p < end - 2; s++) {
        if (*s == '"')  { *p++ = '\\'; *p++ = '"'; }
        else if (*s == '\\') { *p++ = '\\'; *p++ = '\\'; }
        else if (*s == '\n') { *p++ = '\\'; *p++ = 'n'; }
        else if (*s == '\r') { /* skip */ }
        else { *p++ = *s; }
    }
    p += snprintf(p, end - p, "\"}\n");
    write(fd, buf, p - buf);
}

static void send_frame(int fd, const char *type, const char *text) {
    json_escape_and_write(fd, type, text);
}

static void send_simple(int fd, const char *type) {
    char buf[128];
    int n = snprintf(buf, sizeof(buf), "{\"type\":\"%s\"}\n", type);
    write(fd, buf, n);
}

static bool set_nonblock(int fd) {
    int fl = fcntl(fd, F_GETFL);
    if (fl == -1) return false;
    return fcntl(fd, F_SETFL, fl | O_NONBLOCK) != -1;
}

static void rotate_chat_log() {
    if (!chat_log_fp) return;
    long sz = ftell(chat_log_fp);
    if (sz > MAX_CHAT_LOG_MB * 1024 * 1024) {
        fclose(chat_log_fp);
        char backup[600];
        snprintf(backup, sizeof(backup), "%s.old", chat_log_path);
        rename(chat_log_path, backup);
        chat_log_fp = fopen(chat_log_path, "a");
        if (chat_log_fp) {
            fprintf(chat_log_fp, "[%s] --- log rotated ---\n",
                    "1970-01-01 00:00:00");
        }
    }
}

/* ---- PTY 输出行过滤状态机 ---- */

enum FilterState {
    ST_SKIP_ECHO,    // 跳过提问回显（send 出去的文本被 PTY 回显）
    ST_SEARCH,       // 等待出现 robot: 或 user:
    ST_ROBOT,        // 正在收集 robot: 之后的内容
    ST_DONE
};

struct LineFilter {
    FilterState state = ST_SKIP_ECHO;
    std::string robot_buf;   // 当前 robot 回复的累积缓存

    /* 送入一行原始输出；返回 true 表示 out 里组出了一段完整 robot 回复 */
    bool feed(const char *line_raw, std::string &out) {
        out.clear();
        const char *line = line_raw;
        /* 跳过行首空白 */
        while (*line == ' ' || *line == '\t') line++;
        if (!*line) return false;

        /* 过滤 rkllm 初始化日志 */
        if (strncmp(line, "I rkllm:", 8) == 0) return false;
        if (strncmp(line, "----", 4) == 0)      return false;
        if (strncmp(line, "Stage", 5) == 0)     return false;
        if (strncmp(line, "Memory", 6) == 0)    return false;
        if (strncmp(line, "Init", 4) == 0)      return false;
        if (strncmp(line, "Prefill", 7) == 0)   return false;
        if (strncmp(line, "Generate", 8) == 0)  return false;
        if (strncmp(line, "Tokens", 6) == 0)    return false;

        /* 过滤初始化阶段的中文提示行。
         * 原实现匹配一条 7 字中文关键词，但该处注释与字符串在早期以错误编码写入时
         * 已退化为 "???????"（原文不可考），条件实际失效；改为按"模型未就绪 + 中文行"
         * 判断，语义等价且不依赖具体文案。 */
        if (!model_ready && (unsigned char)line[0] >= 0x80) return false;
        if (strncmp(line, "***", 3) == 0)  return false;

        /* user: 提示符：交出已累积的回复；否则进入回显跳过态 */
        if (strncmp(line, "user:", 5) == 0) {
            if (!robot_buf.empty()) {
                out = robot_buf;
                robot_buf.clear();
                log_chat("robot", out.c_str());
                return true;
            }
            state = ST_SKIP_ECHO; // 等待本轮提问的回显结束
            return false;
        }

        /* robot: 提示符：先交出上一段，再开始新的回复 */
        if (strncmp(line, "robot:", 6) == 0) {
            if (!robot_buf.empty()) {
                out = robot_buf;
                robot_buf.clear();
                log_chat("robot", out.c_str());
                // 交出上一段未收尾的 robot 内容
            }
            const char *content = line + 6;
            while (*content == ' ') content++;
            robot_buf = content;
            return true; // 已组出一段内容
        }

        /* 提问回显（send 出去的文本被 PTY 回显）：跳过并进入搜索态 */
        if (state == ST_SKIP_ECHO) {
            state = ST_SEARCH;
            return false;
        }

        /* 其余行：作为 robot 回复的续行追加 */
        if (!robot_buf.empty()) robot_buf += '\n';
        robot_buf += line;
        return false;
    }

    /* 收尾：把尚未返回的累积内容取出（超时 / 静默兜底路径使用） */
    bool flush(std::string &out) {
        if (!robot_buf.empty()) {
            out = robot_buf;
            robot_buf.clear();
            return true;
        }
        return false;
    }

    void reset() {
        state = ST_SKIP_ECHO;
        robot_buf.clear();
    }
};

/* ---- PTY 启动与就绪等待 ---- */

static bool start_llm(const char *demo_path, const char *model_path) {
    if (model_ready) return true;

    fprintf(stderr, "[proxy] Starting llm_demo via PTY...\n");

    pid_t pid = forkpty(&pty_fd, nullptr, nullptr, nullptr);
    if (pid < 0) {
        fprintf(stderr, "[proxy] forkpty failed: %s\n", strerror(errno));
        return false;
    }

    if (pid == 0) {
        /* 子进程：用 stdbuf 关闭 demo 的输出缓冲，保证 token 能被即时读到 */
        const char *sargv[] = {"stdbuf", "-o0", "-e0", demo_path, model_path, "512", "4096", nullptr};
        execvp("stdbuf", const_cast<char *const *>(sargv));
        /* stdbuf 不可用则直接启动 demo */
        const char *argv[] = {demo_path, model_path, "512", "4096", nullptr};
        execv(demo_path, const_cast<char *const *>(argv));
        _exit(1);
    }

    child_pid = pid;
    set_nonblock(pty_fd);

    /* 设置 PTY 窗口尺寸（部分程序据此决定是否输出进度） */
    struct winsize ws = {24, 80, 0, 0};
    ioctl(pty_fd, TIOCSWINSZ, &ws);

    /* 等待 rkllm init success 标记 */
    fprintf(stderr, "[proxy] Waiting for model init (max %ds)...\n", INIT_TIMEOUT);
    char buf[PTY_BUF_SZ];
    std::string accum;
    time_t start_t = time(nullptr);
    bool init_ok = false;

    while (time(nullptr) - start_t < INIT_TIMEOUT) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(pty_fd, &rfds);
        struct timeval tv = {0, 200000}; // 200ms
        int ret = select(pty_fd + 1, &rfds, nullptr, nullptr, &tv);
        if (ret > 0) {
            ssize_t n = read(pty_fd, buf, sizeof(buf) - 1);
            if (n <= 0) break;
            buf[n] = '\0';
            accum += buf;

            /* 按行转发到 stderr，便于排查启动问题 */
            size_t pos;
            while ((pos = accum.find('\n')) != std::string::npos) {
                std::string line = accum.substr(0, pos);
                accum.erase(0, pos + 1);
                while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
                    line.pop_back();
                if (!line.empty())
                    fprintf(stderr, "[llm] %s\n", line.c_str());
            }
            if (accum.find("rkllm init success") != std::string::npos ||
                strstr(buf, "rkllm init success")) {
                init_ok = true;
                break;
            }
        }
    }

    if (!init_ok) {
        fprintf(stderr, "[proxy] Model init timeout or failed\n");
        return false;
    }

    /* 清空启动阶段的残留输出（避免首次提问读到旧内容） */
    sleep(1);
    while (true) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(pty_fd, &rfds);
        struct timeval tv = {0, 300000};
        if (select(pty_fd + 1, &rfds, nullptr, nullptr, &tv) > 0) {
            char tmp[4096];
            if (read(pty_fd, tmp, sizeof(tmp)) <= 0) break;
        } else break;
    }

    model_ready = true;
    fprintf(stderr, "[proxy] Model ready\n");
    return true;
}

static bool restart_llm(const char *demo_path, const char *model_path) {
    fprintf(stderr, "[proxy] Restarting llm_demo...\n");
    model_ready = false;

    if (child_pid > 0) {
        kill(child_pid, SIGTERM);
        sleep(1);
        int status;
        waitpid(child_pid, &status, WNOHANG);
        kill(child_pid, SIGKILL);
        waitpid(child_pid, &status, 0);
        child_pid = -1;
    }
    if (pty_fd >= 0) {
        close(pty_fd);
        pty_fd = -1;
    }
    return start_llm(demo_path, model_path);
}

static bool is_child_alive() {
    if (child_pid <= 0) return false;
    int status;
    pid_t r = waitpid(child_pid, &status, WNOHANG);
    if (r == 0) return true;
    if (r == child_pid) {
        fprintf(stderr, "[proxy] llm_demo exited (status=%d)\n",
                WIFEXITED(status) ? WEXITSTATUS(status) : -1);
        child_pid = -1;
        return false;
    }
    return (errno != ECHILD);
}

/* ---- 提问下发与回复流式转发 ---- */

/* 返回 s 前 end 字节中完整 UTF-8 字符的结束长度（防止截断多字节汉字） */
static size_t utf8_complete_len(const std::string &s, size_t end) {
    size_t good = 0, i = 0;
    while (i < end) {
        unsigned char ch = (unsigned char)s[i];
        size_t len = (ch < 0x80) ? 1 : (ch >= 0xF0) ? 4 : (ch >= 0xE0) ? 3 : (ch >= 0xC0) ? 2 : 1;
        if (i + len > end) break;
        i += len;
        good = i;
    }
    return good;
}

static int do_chat(const char *prompt, int client_fd) {
    if (!model_ready || !is_child_alive()) {
        send_simple(client_fd, "error");
        return -1;
    }

    fprintf(stderr, "[proxy] Chat: %.50s...\n", prompt);

    /* 写入 PTY */
    std::string to_send = std::string(prompt) + "\n";
    ssize_t written = write(pty_fd, to_send.c_str(), to_send.size());
    if (written <= 0) {
        fprintf(stderr, "[proxy] PTY write failed\n");
        send_simple(client_fd, "error");
        return -1;
    }

    /* 记录 user 到 chat log */
    log_chat("user", prompt);

    LineFilter filter;
    filter.reset();
    char buf[PTY_BUF_SZ];
    std::string accum;
    std::string robotAll;          // 目前已组出的完整 robot 文本
    time_t start_t = time(nullptr);
    time_t last_output_t = start_t;
    bool has_content = false;
    bool robot_started = false;
    size_t emitted = 0;            // 已流式发出的字节数

    while (time(nullptr) - start_t < REPLY_TIMEOUT) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(pty_fd, &rfds);
        struct timeval tv = {0, 200000}; // 200ms
        int ret = select(pty_fd + 1, &rfds, nullptr, nullptr, &tv);

        if (ret > 0) {
            ssize_t n = read(pty_fd, buf, sizeof(buf) - 1);
            if (n <= 0) break;
            buf[n] = '\0';
            accum += buf;
            last_output_t = time(nullptr);

            /* 完整行处理 */
            size_t pos;
            while ((pos = accum.find('\n')) != std::string::npos) {
                std::string line = accum.substr(0, pos);
                accum.erase(0, pos + 1);
                /* 去 \r */
                while (!line.empty() &&
                       (line.back() == '\r' || line.back() == ' '))
                    line.pop_back();
                if (line.empty()) continue;

                /* user: 开头整行（兜底路径，正常收尾走半截行检测） */
                if (robot_started && line.size() >= 5 &&
                    strncmp(line.c_str(), "user:", 5) == 0) {
                    std::string remaining;
                    if (filter.flush(remaining)) {
                        if (!remaining.empty()) robotAll = remaining;
                        if (remaining.size() > emitted)
                            send_frame(client_fd, "token",
                                       remaining.substr(emitted).c_str());
                    }
                    if (!robotAll.empty())
                        log_chat("robot", robotAll.c_str());
                    send_simple(client_fd, "done");
                    return 0;
                }

                std::string robot_text;
                filter.feed(line.c_str(), robot_text);
                if (!filter.robot_buf.empty()) robot_started = true;
            }

            /* ---- 半截行处理：robot 内容流式转发 + user: 提示符即时收尾 ----
               demo 的 token 不带换行、结束提示符 "user: " 也不带换行，
               只处理完整行会导致回复积压到超时，必须解析半截行 */
            if (!accum.empty()) {
                size_t lead = 0;
                while (lead < accum.size() &&
                       (accum[lead] == ' ' || accum[lead] == '\t' || accum[lead] == '\r'))
                    lead++;
                std::string_view view(accum.data() + lead, accum.size() - lead);

                /* 组当前 robot 全文 = 过滤缓存 + 半截行内容 */
                std::string current = filter.robot_buf;
                if (view.substr(0, 6) == "robot:") {
                    robot_started = true;
                    std::string content(view.substr(6));
                    size_t sp = content.find_first_not_of(' ');
                    content = (sp == std::string::npos) ? "" : content.substr(sp);
                    if (!current.empty() && !content.empty()) current += '\n';
                    current += content;
                } else if (robot_started && !view.empty() &&
                           view.substr(0, 5) != "user:" &&
                           view.find("rkllm") == std::string_view::npos) {
                    if (!current.empty()) current += '\n';
                    current += std::string(view);
                }
                if (!current.empty()) robotAll = current;

                /* 流式发出新增部分（按 UTF-8 字符边界截断） */
                if (current.size() > emitted) {
                    std::string delta = current.substr(emitted);
                    size_t good = utf8_complete_len(delta, delta.size());
                    if (good > 0) {
                        send_frame(client_fd, "token",
                                   delta.substr(0, good).c_str());
                        emitted += good;
                        has_content = true;
                    }
                }

                /* user: 提示符出现 → 回复结束 */
                if (robot_started && view.substr(0, 5) == "user:") {
                    if (current.size() > emitted) {
                        send_frame(client_fd, "token",
                                   current.substr(emitted).c_str());
                    }
                    if (!robotAll.empty())
                        log_chat("robot", robotAll.c_str());
                    send_simple(client_fd, "done");
                    return 0;
                }
            }
        }

        /* 有内容后静默 REPLY_SILENCE 秒视为结束（兜底） */
        if (has_content &&
            time(nullptr) - last_output_t >= REPLY_SILENCE) {
            if (!robotAll.empty())
                log_chat("robot", robotAll.c_str());
            send_simple(client_fd, "done");
            return 0;
        }
    }

    /* 超时兜底：发出剩余内容 */
    std::string remaining;
    if (filter.flush(remaining) && remaining.size() > emitted)
        send_frame(client_fd, "token", remaining.substr(emitted).c_str());
    if (!robotAll.empty())
        log_chat("robot", robotAll.c_str());
    send_simple(client_fd, "done");
    return 0;
}
/* ---- UDS 服务端 ---- */

static int create_uds_server() {
    unlink(SOCK_PATH);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        fprintf(stderr, "[proxy] socket() failed: %s\n", strerror(errno));
        return -1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCK_PATH, sizeof(addr.sun_path) - 1);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "[proxy] bind() failed: %s\n", strerror(errno));
        close(fd);
        return -1;
    }

    if (listen(fd, 1) < 0) {
        fprintf(stderr, "[proxy] listen() failed: %s\n", strerror(errno));
        close(fd);
        return -1;
    }

    fprintf(stderr, "[proxy] Listening on %s\n", SOCK_PATH);
    return fd;
}

static int handle_client(int client_fd, const char *demo_path,
                         const char *model_path) {
    char buf[PTY_BUF_SZ];
    ssize_t n = read(client_fd, buf, sizeof(buf) - 1);
    if (n <= 0) return -1;
    buf[n] = '\0';

    /* 解析 JSON 命令（简单字符串匹配，够用且零依赖） */
    if (strstr(buf, "\"cmd\":\"chat\"") || strstr(buf, "\"cmd\": \"chat\"")) {
        /* 提取 msg 值：键名闭引号 -> 冒号 -> 值开引号，读到值闭引号 */
        const char *key = strstr(buf, "\"msg\"");
        const char *colon = key ? strchr(key + 5, ':') : nullptr;
        const char *val = colon ? strchr(colon + 1, '"') : nullptr;
        if (!val) {
            send_simple(client_fd, "error");
            return 0;
        }

        std::string msg;
        const char *p = val + 1;
        while (*p) {
            if (*p == '\\' && *(p + 1)) {
                p++;
                if (*p == 'n') msg += '\n';
                else if (*p == 't') msg += '\t';
                else if (*p == 'r') msg += '\r';
                else if (*p == '"') msg += '"';
                else if (*p == '\\') msg += '\\';
                else if (*p == '/') msg += '/';
                else if (*p == 'u') {
                    /* \uXXXX 解码为 UTF-8（含 emoji 代理对） */
                    unsigned cp = 0;
                    bool ok = true;
                    for (int k = 1; k <= 4; k++) {
                        char c = p[k];
                        cp <<= 4;
                        if (c >= '0' && c <= '9') cp |= (unsigned)(c - '0');
                        else if (c >= 'a' && c <= 'f') cp |= (unsigned)(c - 'a' + 10);
                        else if (c >= 'A' && c <= 'F') cp |= (unsigned)(c - 'A' + 10);
                        else { ok = false; break; }
                    }
                    if (ok) {
                        p += 4;
                        if (cp >= 0xD800 && cp <= 0xDBFF &&
                            p[1] == '\\' && p[2] == 'u') {
                            unsigned lo = 0;
                            bool ok2 = true;
                            for (int k = 3; k <= 6; k++) {
                                char c = p[k];
                                lo <<= 4;
                                if (c >= '0' && c <= '9') lo |= (unsigned)(c - '0');
                                else if (c >= 'a' && c <= 'f') lo |= (unsigned)(c - 'a' + 10);
                                else if (c >= 'A' && c <= 'F') lo |= (unsigned)(c - 'A' + 10);
                                else { ok2 = false; break; }
                            }
                            if (ok2 && lo >= 0xDC00 && lo <= 0xDFFF) {
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                p += 6;
                            }
                        }
                        if (cp < 0x80) {
                            msg += (char)cp;
                        } else if (cp < 0x800) {
                            msg += (char)(0xC0 | (cp >> 6));
                            msg += (char)(0x80 | (cp & 0x3F));
                        } else if (cp < 0x10000) {
                            msg += (char)(0xE0 | (cp >> 12));
                            msg += (char)(0x80 | ((cp >> 6) & 0x3F));
                            msg += (char)(0x80 | (cp & 0x3F));
                        } else {
                            msg += (char)(0xF0 | (cp >> 18));
                            msg += (char)(0x80 | ((cp >> 12) & 0x3F));
                            msg += (char)(0x80 | ((cp >> 6) & 0x3F));
                            msg += (char)(0x80 | (cp & 0x3F));
                        }
                    } else {
                        msg += "\\u";
                    }
                }
                else { msg += '\\'; msg += *p; }
            } else if (*p == '"') {
                break;
            } else {
                msg += *p;
            }
            p++;
        }
        if (msg.empty() || msg == ": ") {
            send_simple(client_fd, "error");
            return 0;
        }
        return do_chat(msg.c_str(), client_fd);

    } else if (strstr(buf, "\"cmd\":\"status\"") ||
               strstr(buf, "\"cmd\": \"status\"")) {
        char resp[64];
        snprintf(resp, sizeof(resp),
                 "{\"type\":\"status\",\"ready\":%s}\n",
                 model_ready ? "true" : "false");
        write(client_fd, resp, strlen(resp));
        return 0;

    } else if (strstr(buf, "\"cmd\":\"restart\"") ||
               strstr(buf, "\"cmd\": \"restart\"")) {
        restart_llm(demo_path, model_path);
        send_simple(client_fd, "done");
        return 0;
    }

    send_simple(client_fd, "error");
    return 0;
}

/* ---- 信号与运行标志 ---- */

static volatile sig_atomic_t g_running = 1;

static void sig_handler(int) {
    g_running = 0;
}

/* ---- 用法说明 ---- */

static void print_usage(const char *prog) {
    fprintf(stderr,
        "Usage: %s <llm_demo_path> <model_path> <chat_log_path>\n"
        "  llm_demo_path  - path to llm_demo binary\n"
        "  model_path     - path to .rkllm model file\n"
        "  chat_log_path  - path to chat log file (user/robot history)\n",
        prog);
}

int main(int argc, char **argv) {
    if (argc < 4) {
        print_usage(argv[0]);
        return 1;
    }

    const char *demo_path  = argv[1];
    const char *model_path = argv[2];
    const char *log_path   = argv[3];

    strncpy(chat_log_path, log_path, sizeof(chat_log_path) - 1);

    /* 打开对话日志 */
    chat_log_fp = fopen(chat_log_path, "a");
    if (!chat_log_fp) {
        fprintf(stderr, "[proxy] Warning: cannot open chat log %s\n",
                chat_log_path);
    }

    /* 注册信号处理 */
    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);
    signal(SIGPIPE, SIG_IGN);
    signal(SIGCHLD, SIG_DFL);

    /* 启动 LLM 并等待模型就绪 */
    if (!start_llm(demo_path, model_path)) {
        fprintf(stderr, "[proxy] Failed to start LLM, exiting\n");
        if (chat_log_fp) fclose(chat_log_fp);
        return 1;
    }

    /* 创建 UDS 服务端 */
    int server_fd = create_uds_server();
    if (server_fd < 0) {
        if (chat_log_fp) fclose(chat_log_fp);
        return 1;
    }

    fprintf(stderr, "[proxy] Ready for connections\n");

    /* 主循环：子进程守护 + 接受客户端连接 */
    while (g_running) {
        /* 子进程存活检查（模型就绪后才会判定，避免误重启） */
        if (model_ready && !is_child_alive()) {
            model_ready = false;
            fprintf(stderr, "[proxy] llm_demo died, restarting...\n");
            start_llm(demo_path, model_path);
        }

        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(server_fd, &rfds);
        struct timeval tv = {1, 0}; // 1 秒超时：既让出 CPU，也能及时响应 g_running 变化
        int ret = select(server_fd + 1, &rfds, nullptr, nullptr, &tv);
        if (ret < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (ret == 0) continue;

        int client_fd = accept(server_fd, nullptr, nullptr);
        if (client_fd < 0) continue;

        handle_client(client_fd, demo_path, model_path);
        close(client_fd);

        rotate_chat_log();
    }

    /* 收尾清理 */
    fprintf(stderr, "[proxy] Shutting down...\n");
    if (child_pid > 0) {
        kill(child_pid, SIGTERM);
        sleep(1);
        kill(child_pid, SIGKILL);
        int status;
        waitpid(child_pid, &status, 0);
    }
    if (pty_fd >= 0) close(pty_fd);
    close(server_fd);
    unlink(SOCK_PATH);
    if (chat_log_fp) fclose(chat_log_fp);

    return 0;
}
