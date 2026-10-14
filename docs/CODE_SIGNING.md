# Windows 代码签名与发布政策

当前正式版 v1.0.1 尚未进行 Authenticode 签名。本项目已准备云端构建与签名工作流，但尚未获得签名服务资格或证书；这不代表已获得微软认证或 SignPath 赞助。

## 发布者与来源

维护者为 GitHub 仓库所有者 Huaiyuan-Jiang。源码、构建记录与 Release 均位于本仓库。签名只能来自本仓库 main 分支上通过测试的 GitHub 托管 Windows 构建。拉取请求不能使用签名凭据。签名服务只签署本项目 EXE，不重新签署第三方库。

工作流固定第三方 Action 的提交版本。release-signing 环境应设置维护者审批，并限制部署分支为 main。云服务侧也必须限制本仓库和该环境，使用最小签名权限。不要把 PFX、私钥或服务令牌提交到仓库。

## 流程

1. 固定 Python 3.13、Windows x64 和依赖版本；逐模块运行隔离测试。
2. 构建带产品名和版本资源的 EXE；核对打包的 reader 模块与当前源码。
3. 使用临时 PDF / EPUB 和临时数据执行八项 EXE 自检。
4. 维护者主动运行工作流，选择已配置的签名服务；默认 none 只生成明确标记为 unsigned 的构建产物。
5. 对签名后的最终文件检查 Windows 信任验证结果与时间戳，再运行八项 EXE 自检，最后生成 SHA256。
6. 人工核对源码版本、许可、签名发布者和最终文件后再发布新的 Release。工作流不会自动覆盖或发布 Release。

版本资源目前为 1.0.1；发布下一版本时，应同步更新 tools/windows_version_info.txt 和 Release 标签。旧 Release 不会因为新版本签名而自动获得签名。

## 免费开源签名：SignPath Foundation

申请入口：https://signpath.org/apply.html 。需要服务方审核开源许可、项目情况及构建流程，获批并配置 GitHub App、签名策略及人工审批后才可使用；新项目不保证获批。申请前需在 GitHub 和 SignPath 开启多因素认证。

在 release-signing 环境配置 SIGNPATH_API_TOKEN secret，以及 SIGNPATH_ORGANIZATION_ID、SIGNPATH_PROJECT_SLUG、SIGNPATH_SIGNING_POLICY_SLUG variables。Artifact 配置的根应为 zip-file，只选择里面的 reader.exe 签名。服务方策略须限制本仓库 main 分支、GitHub 托管构建及审批人员。获批后再按条款注明实际签名服务和证书提供者。

条款：https://signpath.org/terms.html 。GitHub 集成：https://docs.signpath.io/trusted-build-systems/github 。

## 微软 Artifact Signing

需要付费 Azure 订阅、受支持地区的身份验证、Public Trust 签名账号与证书配置。个人身份信息以服务方实际要求为准；证书发布者名称可能公开显示经过验证的法定姓名，不使用虚构公司名称。

在 release-signing 环境配置 AZURE_CLIENT_ID、AZURE_TENANT_ID、AZURE_SUBSCRIPTION_ID、SIGNING_ENDPOINT、SIGNING_ACCOUNT、SIGNING_PROFILE variables。使用 Azure OIDC 联合身份，subject 必须精确限制为 repo:Huaiyuan-Jiang/reader:environment:release-signing；仅赋予该证书配置上的 Artifact Signing Certificate Profile Signer 角色，不保存客户端密码。使用 Public Trust 证书及 SHA256/RFC3161 时间戳。

说明：https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options 。

## 隐私与用户控制

软件不预置开发者凭据、个人书籍或阅读数据。不提供后台遥测。普通阅读无需联网；用户主动启用 AI 后，所选阅读片段、提问或页图会发送给用户选定的服务，详见 [模型配置说明](AI_CONFIGURATION.md)。本地阅读数据和凭据不会随签名申请上传；服务处理的是公开源码构建的 EXE 与构建元数据。

用户主动开始计时阅读后，软件暂时阻止 Alt+Tab 切换，读满、结束或紧急退出后恢复；不修改 Windows 永久键盘设置。签名不是运行能力或安全性认证，也不能保证 SmartScreen 对每个新文件立即取消警告。自检不替代真实 AI 账号、OCR 语言安装、实机锁屏和不同硬件上的测试。
