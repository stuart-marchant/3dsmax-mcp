#include "mcp_bridge/bridge_gup.h"
#include "mcp_bridge/chat_ui.h"
#include "mcp_bridge/llm_client.h"
#include "mcp_bridge/handler_helpers.h"
#include <maxapi.h>
#include <notify.h>
#include <windows.h>
#include <cctype>
#include <string>

// Chat is disabled by default. The artist (or their MCP definition) must set
// MCP_ENABLE_CHAT=1 in the process environment to load the in-Max chat UI and
// the LLM client. Without it we skip both — the macroscript that opens the
// chat window is never registered, and LLMClient::Init() is not called.
static bool ChatEnabledByEnv() {
    char buf[64] = {};
    DWORD n = GetEnvironmentVariableA("MCP_ENABLE_CHAT", buf, sizeof(buf));
    if (n == 0 || n >= sizeof(buf)) return false;
    std::string value(buf, n);
    for (auto& c : value) c = (char)std::tolower((unsigned char)c);
    return value == "1" || value == "true" || value == "yes" || value == "on";
}

// ── ClassDesc2 ──────────────────────────────────────────────────
class MCPBridgeClassDesc : public ClassDesc2 {
public:
    int IsPublic() override { return TRUE; }
    void* Create(BOOL) override { return new MCPBridgeGUP(); }
    const TCHAR* ClassName() override { return _T("MCP Bridge"); }
    const TCHAR* NonLocalizedClassName() override { return _T("MCP Bridge"); }
    SClass_ID SuperClassID() override { return GUP_CLASS_ID; }
    Class_ID ClassID() override { return MCP_BRIDGE_CLASS_ID; }
    const TCHAR* Category() override { return _T("MCP"); }
    const TCHAR* InternalName() override { return _T("MCPBridge"); }
    HINSTANCE HInstance() override { return hInstance; }
};

static MCPBridgeClassDesc mcpBridgeDesc;
ClassDesc2* GetMCPBridgeDesc() { return &mcpBridgeDesc; }

// ── Register macroscripts after Max is fully loaded ─────────────
static MCPBridgeGUP* g_gupInstance = nullptr;

void ShowChat() {
    if (!g_gupInstance) return;

    extern void ProcessChatMessage(const std::string& text, MCPBridgeGUP* gup);
    extern void ProcessChatAction(const std::string& action, const std::string& detail, MCPBridgeGUP* gup);

    MCPChatUI::SetMessageCallback([](const std::string& text) {
        ProcessChatMessage(text, g_gupInstance);
    });
    MCPChatUI::SetActionCallback([](const std::string& action, const std::string& detail) {
        ProcessChatAction(action, detail, g_gupInstance);
    });

    MCPChatUI::Show(g_gupInstance);

    if (LLMClient::IsConfigured()) {
        MCPChatUI::AppendMessage("ai", "Chat ready. Model: " + LLMClient::GetConfig().model);
    } else {
        MCPChatUI::AppendMessage("system",
            "No API key. Edit %LOCALAPPDATA%\\3dsmax-mcp\\.env (add ANTHROPIC_API_KEY), "
            "then /reload.");
    }
}

static void OnSystemStartupDone(void* param, NotifyInfo* info) {
    if (!g_gupInstance) return;
    UnRegisterNotification(OnSystemStartupDone, nullptr, NOTIFY_SYSTEM_STARTUP);

    // Chat macroscript is only registered when the operator opts in via env var.
    // This keeps the "MCP Chat" toolbar button from appearing on artist machines
    // that have not been signed off for third-party LLM egress.
    if (!ChatEnabledByEnv()) return;

    // Register a global MAXScript struct with functions that call our C++ directly
    // via the hidden executor window's WM_USER message
    // The trick: we post WM_USER+0x4D43 with a special lParam to trigger ShowChat
    HandlerHelpers::RunMAXScript(
        "macroScript MCP_Chat category:\"MCP\" tooltip:\"Open MCP AI Chat\" buttonText:\"MCP Chat\" "
        "( on execute do ( "
        "  local hwnds = windows.getChildHWND 0 \"MCPBridgeExecutor\"; "
        "  if hwnds != undefined and hwnds.count > 0 do ( "
        "    windows.sendMessage hwnds[1] 0x5144 1 0 "
        "  ) "
        ") )"
    );
}

// ── GUP implementation ──────────────────────────────────────────
DWORD MCPBridgeGUP::Start() {
    g_gupInstance = this;
    executor_.Initialize();

    const bool chatEnabled = ChatEnabledByEnv();

    pipe_server_ = std::make_unique<PipeServer>(this);
    pipe_server_->Start();

    if (chatEnabled) {
        // Chat UI init deferred out of DllMain — calling LoadLibraryW for
        // msftedit.dll under the loader lock deadlocks Max startup.
        MCPChatUI::Init(hInstance);
        // Init LLM client — reads %LOCALAPPDATA%\3dsmax-mcp\mcp_config.ini [llm]
        // and ANTHROPIC_API_KEY from .env / env.
        LLMClient::Init();
    }

    Interface* ip = GetCOREInterface();
    if (ip) {
        LogSys* log = ip->Log();
        if (log) {
            log->LogEntry(SYSLOG_INFO, NO_DIALOG,
                _T("MCP Bridge"),
                _T("MCP Bridge: Native pipe server started on \\\\.\\pipe\\3dsmax-mcp"));
            if (!chatEnabled) {
                log->LogEntry(SYSLOG_INFO, NO_DIALOG, _T("MCP Bridge"),
                    _T("MCP Bridge: in-Max chat disabled (set MCP_ENABLE_CHAT=1 to enable)"));
            } else if (LLMClient::IsConfigured()) {
                std::wstring model = HandlerHelpers::Utf8ToWide(LLMClient::GetConfig().model);
                std::wstring msg = L"MCP Bridge: Standalone chat ready (" + model + L")";
                log->LogEntry(SYSLOG_INFO, NO_DIALOG, _T("MCP Bridge"), msg.c_str());
            }
        }
    }

    // Register macroscripts after Max is fully loaded. The chat macro
    // registration itself is gated on MCP_ENABLE_CHAT (see OnSystemStartupDone).
    RegisterNotification(OnSystemStartupDone, nullptr, NOTIFY_SYSTEM_STARTUP);

    return GUPRESULT_KEEP;
}

void MCPBridgeGUP::Stop() {
    // Drain detached chat threads (ProcessChatMessage launches std::thread.detach()
    // per user message; they sit in WinHTTP and capture `this`) before tearing
    // down the executor and chat UI, otherwise the thread resumes into freed
    // memory. Bounded so a stuck HTTP call doesn't hang Max shutdown forever.
    extern bool WaitForChatTurns(int timeout_ms);
    bool drained = WaitForChatTurns(5000);
    if (!drained) {
        Interface* ip = GetCOREInterface();
        LogSys* log = ip ? ip->Log() : nullptr;
        if (log) {
            log->LogEntry(SYSLOG_WARN, NO_DIALOG, _T("MCP Bridge"),
                _T("MCP Bridge: chat thread still in flight at shutdown — proceeding anyway"));
        }
    }

    MCPChatUI::Destroy();
    if (pipe_server_) {
        pipe_server_->Stop();
        pipe_server_.reset();
    }
    executor_.Shutdown();
    g_gupInstance = nullptr;
}

void MCPBridgeGUP::DeleteThis() {
    delete this;
}
