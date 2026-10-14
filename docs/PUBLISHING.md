# 发布说明

源码仓库：<https://github.com/Huaiyuan-Jiang/reader>。

## Windows EXE

Windows x64、Python 3.13 和 PyInstaller 构建，双击 reader-windows-x64.exe 即可运行，无需安装 Python。Claude Code 模式需要用户自行安装并登录 Claude Code；HTTP API 模式需要用户自己的 API Key。字体默认使用系统字体，发行包不收集本机字体目录。

EXE 未进行代码签名。首次打开需要解压组件，可能比后续启动慢。

已提供云端构建、可选签名及签名后检查流程；服务尚需开通，详见 [代码签名与发布政策](CODE_SIGNING.md)。不要将尚未签名的构建标成已签名。工作流默认只上传构建产物，不自动发布 Release。

## 重建

在新建的 Python 3.13 虚拟环境中执行：

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --clean --noconfirm reader.spec
```

结果位于 dist/reader.exe。不要从包含个人凭据或阅读数据的开发目录构建。

## 发布检查

```powershell
python -m unittest discover -s tests
dist/reader.exe --self-test smoke-result.json
python tools/package_source.py --output reader-source.zip
```

自检使用生成的 PDF / EPUB 和临时数据，不调用真实模型，不开启真实锁屏。检查 PDF 渲染及子进程、EPUB、DPAPI、AI 与 OCR 导入和书架界面。真实模型权限、OCR 系统语言及其他电脑的运行情况需另外验证。

Release 提供 EXE、对应源码、SHA256 校验文件、第三方许可包和本轮安全检查说明。源码打包仅使用显式清单，排除 .git、凭据、个人配置、书籍、笔记、阅读记录、截图和缓存。

自有源码使用 GPL-3.0；第三方组件的许可独立适用，特别是 PyMuPDF / MuPDF 的 AGPL-3.0，详见 THIRD_PARTY_NOTICES.md。

## 隐私边界

程序将书架、阅读记录、笔记、OCR 缓存和加密凭据存放在当前用户的数据目录。启用 AI 后，阅读片段、提问、选用的页图及笔记所需上下文会发送给用户选择的服务。软件不包含开发者的 AI 凭据或预置个人阅读数据。

GitHub 账号名称、提交匿名邮箱、仓库及 Release 发布者是 GitHub 公开元数据；使用当前账号发布不能提供身份匿名性。
