// 鸢尾输入法 · TSF 前端与 Python 引擎服务的命名管道客户端。
// 零外部依赖(纯 WinAPI), UTF-8 传输, 4 字节小端长度前缀 + JSON。
#pragma once

#include <string>
#include <vector>
#include <windows.h>

struct IrisCandidate {
    std::wstring text;     // 候选文本
    std::wstring pinyin;   // 音节分隔拼音(ni'hao)
};

class IrisPipeClient {
public:
    ~IrisPipeClient() { Disconnect(); }

    // 连接引擎服务(服务端须已启动 python -m ime.tsf_server)
    bool Connect(DWORD timeoutMs = 3000) {
        Disconnect();
        const DWORD start = GetTickCount();
        while (GetTickCount() - start < timeoutMs) {
            handle_ = CreateFileW(
                L"\\\\.\\pipe\\iris-ime",
                GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                OPEN_EXISTING, 0, nullptr);
            if (handle_ != INVALID_HANDLE_VALUE) return true;
            if (GetLastError() != ERROR_PIPE_BUSY) return false;
            WaitNamedPipeW(L"\\\\.\\pipe\\iris-ime", 200);
        }
        return false;
    }

    void Disconnect() {
        if (handle_ != INVALID_HANDLE_VALUE) {
            CloseHandle(handle_);
            handle_ = INVALID_HANDLE_VALUE;
        }
    }

    bool IsConnected() const { return handle_ != INVALID_HANDLE_VALUE; }

    // 发送请求 JSON, 返回响应 JSON(utf-8)。失败返回空串。
    std::string Request(const std::string& requestUtf8) {
        if (!IsConnected() && !Connect()) return "";
        const std::string payload =
            Prefix(requestUtf8.size()) + requestUtf8;
        DWORD written = 0;
        if (!WriteFile(handle_, payload.data(), (DWORD)payload.size(),
                       &written, nullptr) || written != payload.size()) {
            Disconnect();
            return "";
        }
        std::string lenBuf = ReadExact(4);
        if (lenBuf.size() < 4) { Disconnect(); return ""; }
        const DWORD len = *reinterpret_cast<const DWORD*>(lenBuf.data());
        std::string resp = ReadExact(len);
        if (resp.size() != len) { Disconnect(); return ""; }
        return resp;
    }

    // candidates 请求: pinyin -> 候选列表(utf-16)
    bool GetCandidates(const std::wstring& pinyin,
                       const std::wstring& context,
                       std::vector<IrisCandidate>* out) {
        std::string req = "{\"op\":\"candidates\",\"pinyin\":\"" +
                          Utf8(pinyin) + "\",\"context\":\"" + Utf8(context) + "\"}";
        const std::string resp = Request(req);
        if (resp.empty()) return false;
        return ParseCandidates(resp, out);
    }

    // commit 通知(用户词学习; prev 两段为合成词学习)
    void NotifyCommit(const std::wstring& text, const std::wstring& key,
                      const std::wstring& prevText = L"",
                      const std::wstring& prevKey = L"") {
        std::string req = "{\"op\":\"commit\",\"text\":\"" + Utf8(text) +
                          "\",\"key\":\"" + Utf8(key) + "\"";
        if (!prevText.empty()) {
            req += ",\"prev_text\":\"" + Utf8(prevText) +
                   "\",\"prev_key\":\"" + Utf8(prevKey) + "\"";
        }
        req += "}";
        Request(req);
    }

private:
    HANDLE handle_ = INVALID_HANDLE_VALUE;

    static std::string Prefix(size_t n) {
        std::string s(4, '\0');
        const DWORD v = (DWORD)n;
        memcpy(&s[0], &v, 4);
        return s;
    }

    std::string ReadExact(DWORD len) {
        std::string out;
        out.reserve(len);
        char buf[4096];
        DWORD read = 0;
        while (out.size() < len) {
            if (!ReadFile(handle_, buf, min(len - (DWORD)out.size(), sizeof(buf)),
                          &read, nullptr) || read == 0) {
                break;
            }
            out.append(buf, read);
        }
        return out;
    }

    static std::string Utf8(const std::wstring& w) {
        if (w.empty()) return "";
        const int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(),
                                          nullptr, 0, nullptr, nullptr);
        std::string s(n, 0);
        WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &s[0],
                            n, nullptr, nullptr);
        return JsonEscape(s);
    }

    static std::string JsonEscape(const std::string& s) {
        std::string out;
        out.reserve(s.size() + 8);
        for (char c : s) {
            switch (c) {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                default:
                    if ((unsigned char)c < 0x20) {
                        char buf[8];
                        sprintf_s(buf, "\\u%04x", c);
                        out += buf;
                    } else out += c;
            }
        }
        return out;
    }

    // 迷你 JSON 解析: 只提取 candidates 数组里的 text/pinyin 字符串值。
    // 服务端响应格式受我们控制, 定向解析足够。
    static bool ParseCandidates(const std::string& resp,
                                std::vector<IrisCandidate>* out) {
        const std::string key = "\"candidates\":[";
        const size_t arr = resp.find(key);
        if (arr == std::string::npos) return false;
        size_t i = arr + key.size();
        while (i < resp.size()) {
            const size_t objStart = resp.find("{", i);
            if (objStart == std::string::npos) break;
            const size_t objEnd = resp.find("}", objStart);
            if (objEnd == std::string::npos) break;
            const std::string obj = resp.substr(objStart, objEnd - objStart);
            IrisCandidate c;
            if (GetStringField(obj, "text", &c.text) &&
                GetStringField(obj, "pinyin", &c.pinyin)) {
                out->push_back(c);
            }
            i = objEnd + 1;
        }
        return !out->empty();
    }

    static bool GetStringField(const std::string& obj, const char* name,
                               std::wstring* out) {
        const std::string needle = "\"" + std::string(name) + "\":\"";
        const size_t p = obj.find(needle);
        if (p == std::string::npos) return false;
        std::string utf8;
        size_t i = p + needle.size();
        while (i < obj.size()) {
            if (obj[i] == '\\') {
                if (i + 1 < obj.size()) {
                    char c = obj[i + 1];
                    switch (c) {
                        case '"': utf8 += '"'; break;
                        case '\\': utf8 += '\\'; break;
                        case 'n': utf8 += '\n'; break;
                        case 'u': {
                            if (i + 5 < obj.size()) {
                                wchar_t wc = (wchar_t)strtoul(obj.substr(i + 2, 4).c_str(), nullptr, 16);
                                AppendUtf8(wc, utf8);
                                i += 4;
                            }
                            break;
                        }
                        default: utf8 += c;
                    }
                    i += 2;
                    continue;
                }
            }
            if (obj[i] == '"') break;
            utf8 += obj[i];
            ++i;
        }
        return Utf16(utf8, out);
    }

    static void AppendUtf8(wchar_t wc, std::string& out) {
        char buf[4];
        const int n = WideCharToMultiByte(CP_UTF8, 0, &wc, 1, buf, 4, nullptr, nullptr);
        out.append(buf, n);
    }

    static bool Utf16(const std::string& utf8, std::wstring* out) {
        if (utf8.empty()) { *out = L""; return true; }
        const int n = MultiByteToWideChar(CP_UTF8, 0, utf8.data(), (int)utf8.size(),
                                          nullptr, 0);
        out->resize(n);
        MultiByteToWideChar(CP_UTF8, 0, utf8.data(), (int)utf8.size(),
                            &(*out)[0], n);
        return true;
    }
};
