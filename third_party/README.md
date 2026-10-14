# 第三方运行库、许可证和对应源码

本目录随雨聊壁纸源码与便携版本一起分发，保留上游版权、许可证和再分发条件。项目原创应用代码的 MIT 许可证不替代或更改以下第三方条款；下载或导出的图片也不受代码许可证覆盖。

## 使用范围

| 组件 | 本次使用版本与位置 | 随附材料 |
| --- | --- | --- |
| pystray | 0.19.5，托盘 Python 模块，未修改 | `pystray-0.19.5/COPYING`、`COPYING.LGPL`、`source/pystray`、完整 `upstream/` 源码与逐文件清单。 |
| Pillow | 12.3.0，图像读取、验证、裁切与预览 | `Pillow-12.3.0/LICENSE` 为 Windows wheel 原始完整聚合许可证，含所带 codec/font 库条款。 |
| six | 1.17.0，pystray 运行依赖 | `six-1.17.0/LICENSE`。 |
| CPython | 3.11.4，便携 Python 运行时 | `CPython-LICENSE.txt` 保留该 Windows 安装版完整许可证，包括 Microsoft Distributable Code 附加条件；`CPython-3.11.4/INCORPORATED-LICENSES.rst` 补充标准库内嵌软件条款。 |
| Expat | CPython 3.11.4 内嵌版本，`pyexpat` | `Expat-CPython-3.11.4/COPYING`。 |
| libmpdec | CPython 3.11.4 内嵌版本，`_decimal` | `CPython-3.11.4/libmpdec-LICENSE.txt` 保留完整文件头许可；原始 `libmpdec-mpdecimal.c` 和 incorporated licenses 一并保留。 |
| Tcl / Tk | Windows DLL 显示 8.6.12；采用 CPython 依赖版本 8.6.12.1 | 分别提供 `Tcl-8.6.12.1/license.terms`、`Tk-8.6.12.1/license.terms`；二者不能互相替代。 |
| OpenSSL / SSLeay | 1.1.1u，CPython 的 TLS DLL；本版没有 OpenSSL 3 | `OpenSSL-1.1.1u/LICENSE`，完整双许可证及致谢。 |
| libffi | 3.4.4，版本依据 CPython 3.11.4 官方构建配置 | `libffi-3.4.4/LICENSE`；CPython incorporated licenses 也保留其条款。 |
| XZ / liblzma | 5.2.5，CPython `_lzma` 所链 liblzma | `XZ-5.2.5/COPYING`；此 liblzma 为公共域部分，不包含 xz 命令行工具的 getopt 单元。Pillow 所带 XZ 版本另由 Pillow 聚合许可证覆盖。 |
| zlib 及其它 CPython 内嵌组件 | 随 CPython 3.11.4；Mersenne Twister、cfuhash 等 | 完整条款保留在 `CPython-3.11.4/INCORPORATED-LICENSES.rst`。 |
| Microsoft Visual C runtime（审计材料） | 开发本地包的 `VCRUNTIME140.dll` 14.34.31938.0 来自 CPython Windows 安装版；新公开候选包不分发此 DLL | `CPython-LICENSE.txt` 的 Windows 附加条件、`Microsoft-VCRUNTIME-REDISTRIBUTION.md` 及来源哈希保留作审计；公开版使用用户已安装的官方 VC++ x64 runtime。 |
| PyInstaller | 6.22.3，仅用于打包；bootloader 与运行时 hooks 进入便携程序 | `PyInstaller-6.22.3/COPYING.txt` 保留 GPL 及 bootloader 例外；`Apache-2.0.txt` 为运行时 hooks / `_pyi_rth_utils` 所用完整 Apache 2.0 条款。 |

未将 SQLite、NumPy、SciPy 或 libheif 本体作为本版运行依赖。新公开候选包目标为 Windows 10/11 x64，使用系统 UCRT 及用户已安装的官方 VC++ x64 runtime；不携带 `VCRUNTIME140*.dll`、`ucrtbase.dll` 或 `api-ms-win-*.dll`。开发本地包曾收集的额外 DLL 不能凭本目录材料取得任意再分发授权；用户原来的本地试用包没有被修改。

## 必要致谢

This software is based in part on the work of the FreeType Team (https://freetype.org/).

This software is based in part on the work of the Independent JPEG Group.

This product includes software developed by the OpenSSL Project for use in the OpenSSL Toolkit (http://www.openssl.org/).

This product includes cryptographic software written by Eric Young (eay@cryptsoft.com). This product includes software written by Tim Hudson (tjh@cryptsoft.com).

这些致谢不表示上游作者或 Microsoft 为本应用背书。详细条件以随附的原始许可证为准。

## pystray 的 LGPL 条款与重新组合

本应用使用 pystray 0.19.5，Copyright (C) 2016–2022 Moses Palmér；其源码版权头采用 **LGPL-3.0-or-later**。随每份便携包提供 GNU GPL 和 GNU LGPL 全文，不对该库的修改或为调试这些修改进行的逆向工程施加额外限制。

便携程序中的 pystray 模块被冻结在 PyInstaller archive 中；旁边的源码用于查看、修改和重新构建，不能仅因它是 Python 包就声称可直接替换共享库。为支持按 LGPL 第 4d0 条重新组合，**便携包必须与同一应用版本的源码材料一并提供或从同一分发页面提供**：`yuliao_wallpaper/`、`scripts/`、固定依赖清单以及本目录。不得只附一个会变化的上游主页链接而省去同版本应用构建材料。

官方 v0.19.5 源码已固定到 commit `1907f8681d6d421517c63d94f425f9cdd74d0034`，完整档案及解压文件见 `pystray-0.19.5/upstream-source.zip`、`upstream/`、`upstream-files.json`。该官方 archive 原样包含 setup 配置、README、变更记录、文档（含 Sphinx 配置）、包源码，以及 `tests/__init__.py`、`tests/icon_tests.py`、`tests/menu_descriptor_tests.py`；其中没有 `.git` 历史。本项目原样保留上游测试文件，没有执行这些下载的测试。

重新组合方法：

1. 取得与便携程序匹配的雨聊壁纸源码，使用完整 CPython 3.11 创建 `.venv`，从官方 PyPI 安装 `requirements-dev.txt` 中的固定依赖。
2. 复制 `upstream/lib/pystray` 到可编辑工作副本，修改该库，保留其版权及许可证，并标明修改与日期。
3. 将修改后的 `pystray` 包目录替换项目 `.venv/Lib/site-packages/pystray`；无需执行本目录下载的上游 setup 文件。
4. 从应用源码根目录执行 `.venv/Scripts/python.exe scripts/collect_notices.py`，再运行 `scripts/build.ps1 -OutputDirectory dist-0.2`。构建脚本使用虚拟环境中的包并生成新的整目录便携程序。
5. 使用新构建目录运行修改版；不要覆盖正在运行的旧程序。若再分发修改版，保留同版本应用源码、修改后的库源码、许可证及修改记录；本次分发的“未修改”声明不自动适用于你的修改版。

以上为源码重新组合步骤，不表示本轮已经执行或验收用户修改后的软件。源档案没有被安装、导入或执行。

## 来源与完整性

`SOURCE-MANIFEST.json` 记录逐项原始版本引用、commit 或 Git blob 固定 URL、字节数、SHA-256 及提取说明。版本 tag 的解析不应冒称永远不变；本清单以 commit 或已验证的内容寻址 Git blob 固定所保存文本。`pystray-0.19.5/upstream-files.json` 记录档案哈希与每个解压文件哈希。下载仅使用官方 GitHub HTTPS，文本上限 1 MiB、档案上限 10 MiB、展开上限 20 MiB、成员上限 1000；解压拒绝绝对路径、`..`、盘符、链接和 Windows 保留名，不读取或包含私人照片、凭据或配置。

旧的 wheel/Windows 安装版许可证原样保留。按不同来源并存的副本保持各自来源记录，不删改上游条款以求统一。分发时应保留整个本目录，并在版本发布页面注明对应应用源码版本与档案哈希。
