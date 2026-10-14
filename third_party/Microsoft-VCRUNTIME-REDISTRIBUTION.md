# Microsoft runtime 分发材料与范围

本版本使用 CPython 3.11.4 Windows 安装版提供的 `VCRUNTIME140.dll`，文件版本 14.34.31938.0。`CPython-3.11.4/Windows-runtime-provenance.json` 记录原文件 SHA-256 和开发便携包的逐字节一致性，不记录开发电脑绝对路径。这证明文件来源和内容，不等于已经确认所有发布者的 Microsoft 再分发许可资格。

上述 DLL 来源记录属于开发本地包审计。**新公开候选包不分发 `VCRUNTIME140*.dll`、`ucrtbase.dll` 或 `api-ms-win-*.dll`**，只面向 Windows 10/11 x64，使用系统 UCRT 和用户已安装的官方 VC++ x64 runtime。安装运行库由用户按 Microsoft 官方说明完成，程序不自动下载安装；原来的用户本地包不改动。

CPython Windows 版原始完整 `CPython-LICENSE.txt` 随附。其 Windows 附加条件另原样提取在 `CPython-3.11.4/Windows-REDISTRIBUTION.txt`，包括 Microsoft Distributable Code 版权、不得暗示背书、Microsoft 平台限定、不得用于恶意/欺骗/违法程序，以及要求下游接受适用保护条件的条款。项目原创代码的 MIT 许可不覆盖、取消或重新授权这些 Microsoft 条款。

适用官方材料：

- [Visual C++ Runtime 2015–2022 完整许可条款入口](https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/)。
- [Microsoft Visual C++ 文件再分发规则](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170)：独立 runtime 包、合并模块和个别二进制分发受到 Microsoft Software License Terms 及许可资格限制。
- [Visual Studio 许可目录](https://visualstudio.microsoft.com/license-terms/)，按实际使用产品和组件核对相应完整条款。
- [Universal CRT 部署说明](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment?view=msvc-170)：本项目目标 Windows 10/11 x64 使用系统 UCRT。

最终公开二进制包应核对适用许可及实际文件来源。开发构建曾因 PATH 搜索收集 Codex libheif 工具目录中的 `ucrtbase.dll` 和 `api-ms-win-*`；这些额外文件不因本材料存在而获得任意再分发授权，不能随干净公开包混入。未修改用户正在使用的本地版本。

如果发布者不能确认独立 Microsoft DLL 的适用分发条件，先公开应用源码与第三方源码材料，保留可执行版本为本地试用，不将公开二进制标为已全面合规。本文仅记录具体来源、现有条款和需核对范围，没有读取任何登录或许可凭据。
