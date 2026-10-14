# 第三方通知与对应源码

应用原创代码和文档采用根目录 MIT 许可；下列软件及运行库保持自己的许可证。图像的作者和图片许可另行逐图查看，不由任何软件许可证覆盖。

| 软件 | 本次版本 / 许可材料 |
| --- | --- |
| CPython | 3.11.4；`third_party/CPython-LICENSE.txt` 和 `CPython-3.11.4/INCORPORATED-LICENSES.rst` 包含 Python 及所带软件通知。 |
| Pillow | 12.3.0；`third_party/Pillow-12.3.0/LICENSE` 包含 Pillow 及所带图像/字体库的许可。 |
| pystray | 0.19.5，LGPL-3.0-or-later；GPL/LGPL 全文、完整上游源码和运行包副本在对应目录。 |
| six | 1.17.0，MIT；许可在对应目录。 |
| PyInstaller | 6.22.3；COPYING 包含打包 bootloader 例外，运行钩子等使用 Apache 2.0，全文在对应目录。 |
| Tcl / Tk | 8.6.12 系列；保留精确来源的 `license.terms`。 |
| OpenSSL / SSLeay | 1.1.1u；保留该版本双许可全文，不以 OpenSSL 3 许可替代。 |
| Expat / libmpdec / libffi / liblzma | 保留对应的 Expat COPYING、libmpdec 完整首部、libffi LICENSE、XZ COPYING 和 CPython 所带软件通知。 |

文件来源、不可变上游提交、摘要和提取说明在 `third_party/SOURCE-MANIFEST.json`。公开便携包不分发 Microsoft VCRUNTIME/UCRT/API-set DLL；它们使用 Windows 10/11 和适用 VC++ x64 运行库的系统安装版本，其许可不由本应用 MIT 授予。

本产品使用 FreeType；相应字体库通知完整保留在 Pillow 许可文件中。

This software is based in part on the work of the Independent JPEG Group.

This product includes software developed by the OpenSSL Project for use in the OpenSSL Toolkit (https://www.openssl.org/).

This product includes cryptographic software written by Eric Young (eay@cryptsoft.com).

This product includes software written by Tim Hudson (tjh@cryptsoft.com).

## LGPL 修改与重建

冻结到 Python PYZ 的模块不被当成可直接替换的共享库。完整 pystray 对应源码位于 `third_party/pystray-0.19.5/upstream/`，本应用对应源码、固定依赖和构建工具位于仓库；可按 [维护说明](docs/MAINTENANCE.md) 修改 pystray、替换虚拟环境包并重新构建本应用。分发时保持准确版本/提交的这些材料及许可证，无需签名密钥或激活服务。

不同 Python/Pillow 或系统构建环境可能改变所带库版本；再分发前重新核对二进制与通知对应关系，不沿用本版清单作所有版本的声明。
