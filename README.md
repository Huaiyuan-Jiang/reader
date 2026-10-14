# reader · AI 陪读阅读器

Windows 桌面软件，右边读书，左边问 AI。支持 PDF、EPUB、划线、读书小测、阅读计时和读书笔记。每天默认目标 45 分钟，可分几次完成；主人主动开始后，阅读窗口全屏置顶，副屏显示遮罩，离开时暂停计时，紧急退出通过四步确认完成。计时全屏阅读期间暂时禁用 Alt+Tab 和 Alt+Shift+Tab，读满或退出后恢复。

## 运行

第一次使用可以先看[图文新手教程](docs/GETTING_STARTED.md)。软件书架右侧也有“新手教程”入口，打开随 EXE 提供的离线图文手册。

在 [Releases](https://github.com/Huaiyuan-Jiang/reader/releases) 下载 `reader-windows-x64.exe`，双击即可运行，无需安装 Python。适用于 Windows 10 / 11 的 x64 电脑。首次打开会稍慢，程序需要解压运行组件。

当前 v1.0.1 尚未签名。签名状态、构建检查和后续签名流程见 [代码签名与发布政策](docs/CODE_SIGNING.md)。

如从源码运行，需要 Windows、Python 3.13。先安装依赖，再双击 **`打开reader.bat`**：

```powershell
python -m pip install -r requirements.txt
```

```powershell
python -m reader            # 打开书架
```

## 两步连接 AI

在书架右侧点击 **AI 设置**。

1. 选 **DeepSeek**、**MiMo**、**GPT / OpenAI** 或 **Claude API**，填自己的 API Key。
2. 点 **连接并使用**，程序自动保存、发送短请求验证，成功后即可用于下一次阅读。

模型 ID、接口地址、协议、输出上限等已经预填。需要修改时展开 **高级选项**。Key 用 Windows DPAPI 加密保存在本机，源码包不携带开发者的 Key。

**Claude Code** 直接使用本机 Claude Code 的登录，默认使用独立的 `.claude-reader` 目录。选它后点连接，未登录会打开登录窗口。

**本地 Harness** 选它后点 **复制接入提示词**，把提示词给你常用的本地 AI 工具。提示词包含实际项目目录和接入步骤，让工具完成本机连接与测试。接口说明见 [Harness 接入](docs/HARNESS.md)。

详细配置、环境变量、模型能力和数据发送范围见 [模型配置说明](docs/AI_CONFIGURATION.md)。普通阅读无需 AI 登录。

## 测试

```powershell
python -m unittest discover -s tests
```

测试使用离屏界面，不调用真模型、不进入锁屏。网络协议使用模拟 HTTP，本地 Harness 使用假桥接进程。

## 开发用的环境变量

| 变量 | 作用 |
|---|---|
| `READER_NO_LOCK=1` | **开发开关**：读书界面用普通窗口（1280×800，有标题栏），不调 `show_locked()`、不铺副屏遮罩。计时、发呆、离开、小测、紧急出口、读书笔记都照常是真的。点窗口的 × = 顶条的「结束」。 |
| `READER_DATA_DIR=<文件夹>` | 所有数据换个地方放：书架、读书笔记、识别结果、会话心跳 `session.json` 和阅读统计 `status.json`。默认 `%LOCALAPPDATA%\reader\`。 |
| `READER_PDF_RENDER=thread` | PDF 出图不用子进程，改在线程里做。 |
| `READER_FONT_ENGINE=system` | 不换 FreeType，用系统的字体渲染。 |

**在主人的电脑上开发、试用、截图，一律用 `READER_NO_LOCK=1`，再把 `READER_DATA_DIR` 指到临时文件夹**：
真锁屏会把整个屏幕压住；不换数据目录会把试读记进真的阅读统计。

```powershell
$env:READER_NO_LOCK = "1"
$env:READER_DATA_DIR = "$env:TEMP\reader-dev"
python -m reader
```

## 准备分享

```powershell
python tools/package_source.py --output "reader-source.zip"
```

只打包运行源码、测试、依赖和说明；排除凭据、个人书籍、阅读数据、截图与 Git 历史。详见 [发布说明](docs/PUBLISHING.md)。公开源码在 <https://github.com/Huaiyuan-Jiang/reader>。


## 许可证

按 [GNU GPL v3.0](LICENSE) 授权：可以自由使用、修改和再发布，再发布（含修改版）时也须按 GPL-3.0 公开源码。

打包版包含 PyMuPDF / MuPDF（AGPL-3.0）等第三方组件，其条款继续适用；见 [第三方许可说明](THIRD_PARTY_NOTICES.md)。Release 同时提供对应源码、构建依赖版本和许可证。exe 未做代码签名。
