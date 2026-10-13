# 第三方许可说明

reader 自有源码使用 GPL-3.0，完整条款见 LICENSE。

Windows 发行包使用以下第三方组件，原版权与许可继续适用：

- PyMuPDF / MuPDF：Artifex，AGPL-3.0（本发行包使用开源许可）。[官方许可说明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。
- PySide6 / Qt / Shiboken：Qt Project，适用 GPL / LGPL 及包内列出的第三方条款。
- NumPy：NumPy Developers，BSD 许可及其内置数学库许可。
- Claude Agent SDK：Anthropic，MIT；Claude Code 登录使用用户自行安装的 CLI。
- HTTPX、AnyIO、MCP 及其他 Python 运行依赖：各自的包内许可。
- CPython：Python Software Foundation 许可。
- PyInstaller 启动器：GPL，附带允许应用打包分发的例外。

GPLv3 第 13 节允许与 AGPLv3 代码组合分发；reader 自有部分继续适用 GPLv3，AGPLv3 的相关网络交互要求适用于组合程序。不得把 PyMuPDF 部分描述为只有 GPL 授权。[GNU 说明](https://www.gnu.org/licenses/quick-guide-gplv3.en.html)。

Release 的源码附件提供 reader 的对应源码、构建入口、PyInstaller 配置和准确依赖版本。第三方组件未经修改，可按 requirements-build.txt 中版本从各官方项目或 PyPI 获取源码；LICENSES.zip 提供发行环境的许可证副本及组件来源清单。
