#pragma once
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

class MCPBridgeGUP;

// LLM client for the optional in-Max chat window. Targets Anthropic's
// Messages API directly (https://api.anthropic.com/v1/messages). The
// chat is OFF BY DEFAULT — the Python MCP layer only registers the
// chat tool surface when MCP_ENABLE_CHAT=true.
//
// Config is read from %LOCALAPPDATA%\3dsmax-mcp\mcp_config.ini [llm]
// section, the same file that gates [mcp] safe_mode. The api_key comes
// from ANTHROPIC_API_KEY (env var or %LOCALAPPDATA%\3dsmax-mcp\.env).
namespace LLMClient {

using json = nlohmann::json;

struct Config {
    std::string apiKey;
    std::string baseUrl;    // e.g. "https://api.anthropic.com"
    std::string model;      // e.g. "claude-sonnet-4-6"
    int maxTokens = 4096;
    float temperature = 0.7f;
    std::string promptMode = "compact"; // compact, full, none
    std::string toolProfile = "core";   // core, full
    bool includeSceneSnapshot = true;
    int maxSceneRoots = 25;
    int maxPromptChars = 12000;
    int maxToolResultChars = 12000;
    int maxHistoryToolChars = 1800;
    int maxToolSummaryChars = 600;
    int maxDisplayToolChars = 600;
    int maxToolLoops = 4;
};

// Anthropic Messages API uses content blocks rather than a flat string
// + separate "tool" role. We keep the same struct shape so callers (chat_ui.cpp,
// chat_handlers.cpp) do not change: the Chat() implementation converts to/from
// Anthropic content-block JSON internally.
//
// role: "user" or "assistant" (Anthropic does not accept role:"system" —
// the system prompt is a top-level request field — and does not accept
// role:"tool" — tool results are user-role messages with a tool_result
// content block).
struct Message {
    std::string role;       // "user", "assistant"
    std::string content;    // plain user/assistant text (when no tool calls)
    std::string toolCallId; // tool_use id this message is a result for
    json toolCalls;         // anthropic-shaped tool_use blocks (assistant turns)
};

struct ToolCall {
    std::string id;
    std::string name;
    json arguments;
};

struct Response {
    std::string text;
    std::vector<ToolCall> toolCalls;
    std::string finishReason; // "end_turn", "tool_use", "max_tokens", "stop_sequence"
    bool ok = false;
    std::string error;
};

// Load config from %LOCALAPPDATA%\3dsmax-mcp\mcp_config.ini [llm] section
// plus env-var fallback. Called at plugin Start() and by /reload slash command.
void Init();

// Read-only accessor (post-Init).
const Config& GetConfig();
std::string GetApiKeySource();
std::string GetApiKeyFingerprint();

// True when api_key + base_url are set.
bool IsConfigured();

// Send chat completion request (blocking — call from background thread).
Response Chat(
    const std::vector<Message>& messages,
    const json& tools = json::array(),
    int timeoutMs = 180000);

// Execute a tool call by routing through CommandDispatcher::Dispatch —
// inherits safe_mode filter (for execute_maxscript) and main-thread
// marshaling. Returns the handler's result JSON string, or an error JSON.
std::string ExecuteTool(const std::string& toolName, const json& input, MCPBridgeGUP* gup);

// Build system prompt: runtime preamble + cached SKILL.md + current scene snapshot.
// SKILL.md is loaded from %LOCALAPPDATA%\3dsmax-mcp\skill\SKILL.md (deployed by
// native/deploy.bat) and cached across turns; scene snapshot is re-read each turn.
std::string BuildSystemPrompt(MCPBridgeGUP* gup);

// Anthropic-format tool definitions ({name, description, input_schema}).
// Auto-generated from src/tools/*.py by scripts/gen_tool_registry.py
// into native/generated/chat_tool_registry.inc.
json GetToolDefinitions();

} // namespace LLMClient
