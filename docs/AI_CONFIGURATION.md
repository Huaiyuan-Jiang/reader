# 模型接入与修改

在书架右侧点击 **AI 设置**，选择服务，填自己的 API Key，点 **连接并使用**。模型与地址已预填，程序自动保存并发短请求验证，成功后下一次阅读的问答、小测、笔记都会使用这个配置。普通读书无需登录 AI。

默认界面只显示服务选择与 API Key。**高级选项**里可以改模型、地址、协议、认证头、环境变量名、输出上限、等待时间、图片支持、附加 JSON 参数以及 Claude Code 程序路径。新增配置可以保留多个服务或同一服务的多个模型。

## 已实现的接入路径

| 选项 | 接入方式 | 所需凭据 | 默认接口根地址 |
|---|---|---|---|
| Claude Code | 本机 Claude CLI + Claude Agent SDK | Claude Code 登录 | 不使用 API URL |
| GPT / OpenAI | OpenAI Responses API | 自己的 OpenAI API Key | `https://api.openai.com/v1` |
| DeepSeek | OpenAI 兼容 Chat Completions | 自己的 DeepSeek API Key | `https://api.deepseek.com` |
| Claude API | Anthropic Messages API | 自己的 Anthropic API Key | `https://api.anthropic.com/v1` |
| MiMo | OpenAI 兼容 Chat Completions，`api-key` 认证头 | 自己的 MiMo API Key | `https://api.xiaomimimo.com/v1` |
| 自定义兼容接口 | 可选上述三种 HTTP 协议 | 服务要求的 Key；本机服务可留空 | 默认 `http://localhost:11434/v1` |
| 本地 Harness | 官方本地调用 + JSONL 桥接 | 本机 Harness 已有登录 | 复制提示词后由工具自动配置 |

接口根地址不包含末尾的 `/responses`、`/chat/completions` 或 `/messages`，程序按所选协议添加。远程服务使用 HTTPS，本机 localhost 可使用 HTTP。不会把 Key 跟随重定向发往其他地址。

默认模型 ID 是可编辑的示例，账户权限与服务商可用模型以实际账户为准。服务商新增模型时改设置即可，不需要改适配代码。当前示例为 `claude-opus-5-5`、`gpt-6-astra`、`deepseek-flash`、`mimo-v2.6-pro`。

## Claude Code

程序使用本机安装的 Claude Code，默认在 `~/.claude-reader` 中单独登录。书架保留打开登录窗口和检查登录状态的入口。可在 AI 设置中修改登录目录，或者用 `READER_CLAUDE_CONFIG_DIR` 覆盖。

```powershell
$env:CLAUDE_CONFIG_DIR = "$HOME\.claude-reader"
claude auth login
```

运行时使用独立空工作目录、空工具列表和空设置来源。阅读问答无需操作电脑、读取项目或启用 MCP。CLI 路径优先使用 `CLAUDE_CLI`，其次使用配置中的路径，再查找本机默认安装。Claude Code 的账户登录与 Claude API Key 是两个独立选项；GPT、DeepSeek 使用自己的 HTTP 接口，不需要借用 Claude Code。

## 配置与凭据

普通设置默认保存到 `%LOCALAPPDATA%\reader\ai-config.json`，示例见项目根目录的 `ai-config.example.json`。可用 `READER_AI_CONFIG` 指定另一个配置文件，用 `READER_DATA_DIR` 将测试或便携数据放到另一个目录。

配置里保存 **Key 的环境变量名**，不保存实际 Key。凭据读取顺序为对应环境变量、本机加密凭据。

界面输入的 Key 经过 Windows DPAPI 加密，保存到数据目录下的 `ai-secrets.json`。它绑定当前 Windows 账户，不能作为可分享的配置复制到别人的电脑。环境变量中的 Key 优先，删除界面里保存的 Key 不会删除环境变量。

```powershell
# 也可以完全通过环境变量选择
$env:READER_AI = "openai"
$env:OPENAI_API_KEY = "你的Key"
$env:READER_MODEL = "你有权限使用的模型ID"
python -m reader
```

此文件中的值为占位符。`.env.example` 仅列出变量，程序不会自动读取 `.env`；环境变量需要由启动环境显式设置。

| 变量 | 作用 |
|---|---|
| `READER_AI` | 覆盖当前配置，支持配置 ID；`gpt` 是 `openai` 的别名 |
| `READER_MODEL` | 覆盖当前模型 ID |
| `READER_BASE_URL` | 覆盖当前 HTTP 接口根地址 |
| `READER_AI_CONFIG` | 指定普通配置文件 |
| `READER_CLAUDE_CONFIG_DIR` | 覆盖 Claude Code 登录目录 |
| `CLAUDE_CLI` | 覆盖 Claude Code 程序路径 |
| `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `READER_MIMO_KEY` | 各默认服务的 Key |

## 连接与能力检查

默认的 **连接并使用** 自动保存并发送一条简短请求，收到完整回答后才报告成功并关闭设置。失败会留在原窗口显示原因。Claude Code 未登录时会打开登录窗口，登录后再点连接即可。

高级选项中的 **检查凭据** 只查本机是否有 Key 或 Claude Code 是否已登录，不能证明 Key 有效或模型可用。**保存并测试连接** 可在不关闭窗口的情况下验证。请求可能消耗服务额度。

本地 Harness 的默认界面只提供复制接入提示词，无需主人填写命令或 Key。提示词交给本地工具完成配置，具体桥接契约见 [本地 Harness 说明](HARNESS.md)。

模型需要支持图像输入才能回答 PDF 图表。DeepSeek 当前 `deepseek-flash` 示例已按官方图像接口设置；如果换成仅支持文字的模型，请取消图片支持。未开启图片时，图表提问会显示说明，文字问答照常可用。图片能力依赖实际模型，开关不会给模型增加能力。

API 路径保留多轮历史；配置、模型或 Key 变化后旧会话不能续接，会要求重新提问，并重发相关原文。取消或超时不会写入成功会话。截断的输出、未完成的流、HTTP 错误会明确返回失败，不会自动换到另一家服务。一次性小测与笔记不留 API 会话。

使用所选云服务时，相关书中文字、提问和必要的页图会发给该服务。书架、阅读统计、OCR 缓存和读书笔记默认保存在本机。

## 核验资料

接口实现核对了以下官方文档。

- [OpenAI 文本生成与 Responses](https://developers.openai.com/api/docs/guides/text)
- [OpenAI 流式响应](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Claude Agent SDK Python](https://platform.claude.com/docs/en/agent-sdk/python)
- [Claude Messages 流式响应](https://platform.claude.com/docs/en/api/streaming)
- [DeepSeek API 快速开始](https://api-docs.deepseek.com/)
- [DeepSeek 图片输入](https://api-docs.deepseek.com/guides/vision)
- [MiMo 第一次 API 调用](https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call)
