# 维护与重建

`yuliao_wallpaper/` 为应用源码；`tests/` 为后台测试；`scripts/` 为启动/构建工具；`third_party/` 为许可通知、来源摘要和 LGPL 对应源码。运行数据位于用户本机的独立目录，不写入源码或构建包。

安装完整 CPython 3.11.4 后，在源码根目录初始化；当前便携构建固定此版本，以匹配附带运行时通知：

```powershell
.\start.cmd -Setup
.\.venv\Scripts\python.exe -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --no-deps --disable-pip-version-check -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

固定版本更新前核对官方发布、Windows wheel 和 Python 兼容性，更新后重做后台验证。不要把虚拟环境、缓存、相片、日志或恢复备份提交到仓库。

## 构建便携包

由使用者先退出该版本，避免 exe 被占用。运行：

```powershell
.\scripts\build.ps1
```

默认输出 `dist/YuliaoWallpaper`，保留整个目录。需要旁路输出可用 `-OutputDirectory dist-0.2`。脚本不分发 Microsoft VCRUNTIME/UCRT/API-set DLL，目标系统需自行具备 Win10/11 UCRT 和适用的 VC++ x64 运行库。仅源码和窗口自检不等于实际桌面验收。构建后核对 Python/Tcl/Tk/编码库版本及 `third_party/` 对应通知；改变构建环境时不能沿用不匹配的运行库通知。

## 替换 LGPL pystray 后重建

`third_party/pystray-0.19.5/upstream/` 是该版本完整、未修改的上游源码；`source/pystray/` 是与 wheel 中运行包逐文件匹配的副本，GPL/LGPL 全文一并保留。对应来源及校验摘要在 `third_party/SOURCE-MANIFEST.json`。

可修改 pystray 源码，将修改后的包目录替换虚拟环境的 `Lib/site-packages/pystray/`，保留兼容的 Windows 后端 API，然后用本仓库应用源码、依赖清单和构建脚本重新构建完整包。构建无需签名密钥或激活服务。本应用 MIT 许可不限制对这些 LGPL 部分的修改或为调试修改进行逆向工程。

分发时保留对应版本的应用源码、pystray 完整源码、许可证和以上重建方法；不能只附一个上游首页链接或把冻结 PYZ 视为可直接替换的共享库。公开 release 应固定到准确源码提交/tag，便于对应构建。

## 人工确认清单

- 用户主动启动，检查窗口、字体、DPI、滚动和窄窗口布局。
- 选择明确本地图片，手动启用、切换并恢复，核对原图和填充/平铺设置。
- 检查托盘、暂停、取消、多选、排序、移除恢复及导出清单。
- 检查真实联网、断网缓存、睡眠和长期轮换；多屏启用应安全拒绝。

检查前暂停其他壁纸软件；无人值守测试不能调用实际换图、停止其他应用或反复开关测试窗口。
