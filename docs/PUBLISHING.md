# 发布说明

公开源码在 <https://github.com/Huaiyuan-Jiang/reader>，按 GPL-3.0 授权（见 `LICENSE`）。公开仓库由下面的源码包生成，不带开发历史、截图和个人数据。

项目可以在本机运行，已增加可编辑的多模型配置。`tools/package_source.py` 会生成干净的源码 ZIP，显式排除个人凭据、配置、阅读数据、缓存、截图和 `.git`。

```powershell
python tools/package_source.py --output "你的目录\reader-source.zip"
```

源码 ZIP 包含运行代码、测试、依赖清单、启动文件、配置示例、许可证和本文档。不包含任何 API Key。解压后主人安装依赖、设置自己的 AI 就能使用。

## 验证

网络协议用模拟 HTTP 传输验证。真实 API 的额度、模型权限与图片能力需要在主人自己的账户上点击测试连接。

## 尚未完成

- 新电脑安装并用自己的模型配置做一次实际阅读验收。目前发布的是源码，不是签名的安装包或 Windows EXE。

不要直接共享本机数据目录、Claude 登录目录或携带个人 Key 的打包程序。
