# 游戏终端

终端在运行《Forza Horizon 5》的 Windows 电脑上执行，使用 Windows Graphics Capture 采集游戏窗口，压缩 JPEG 后上传中心；操作经 `Python → vgamepad → ViGEmBus → 虚拟 Xbox 360 手柄 → 游戏` 执行。中心可以运行在另一台电脑或 Docker 中，终端需要直接访问 Windows 桌面和驱动，因此在 Windows 本机运行。

## 双击启动和关闭

配置好本目录 `.env`，安装 uv 和 ViGEmBus 后，可以直接双击：

- `启动终端.cmd`：自动用 uv 准备依赖并在后台启动；已运行时会提示，不会重复启动。
- `关闭终端.cmd`：释放手柄输入并关闭后台终端；已经关闭时也可以再次点击。

首次启动需要联网下载依赖。失败时窗口会保留错误信息；正常启动后的日志位于 `.runtime/terminal.log`。游戏窗口出现后自动开始采集，中心连接和 60 FPS 配置均读取本目录 `.env`。

## 安装和前台验证

要求 Windows 10/11、[uv](https://docs.astral.sh/uv/getting-started/installation/)，以及已安装的 [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases)。安装驱动后按安装器要求重启。`terminal/` 可以单独复制到游戏电脑，不依赖项目其他目录。uv 按本目录 `.python-version` 使用 Python 3.12，按独立 `uv.lock` 安装依赖到本目录 `.venv`：

```powershell
cd terminal
uv sync --locked
Copy-Item .env.example .env
# 编辑 .env，让 CENTER_TOKEN 与中心 .env 中的口令一致。
uv run auto-driver-terminal --terminal-id game-pc
```

默认目标采集帧率为 60 FPS，可配置范围为 1–120 FPS。在本目录 `.env` 设置 `TERMINAL_FPS=60`，或通过 `--fps` 覆盖；需要更高目标时可运行 `uv run auto-driver-terminal --fps 120`。

启动时自动读取本目录 `.env`，不依赖当前工作目录；已存在的进程环境变量优先。省略 `--center` 和 `.env` 的 `CENTER_URL` 时，终端在 UDP 9999 上广播探测，同时探测本机中心；探测回应通过随机 nonce 匹配，不包含口令。终端自动建立带 Bearer 认证的 WebSocket，断线后重新探测、重连。多台终端须使用不同的 `--terminal-id`，默认取本机主机名。可指定中心地址，绕过广播：

```powershell
$env:CENTER_URL = "http://192.168.1.10:8000"
uv run auto-driver-terminal --terminal-id game-pc --window-title "Forza Horizon 5" --fps 60 --width 1280 --jpeg-quality 75
```

先启动游戏，使用窗口化或无边框窗口；`--window-title` 为标题的子串，匹配忽略大小写。游戏窗口不存在或关闭时采集自动重试，状态会报错，不上传假画面。终端进程可以隐藏运行，不会主动切换窗口焦点；游戏是否失焦暂停、接受后台手柄输入取决于游戏设置。不要最小化游戏、锁屏或切换到非交互服务会话，否则采集可能停帧。需要游戏继续驾驶时，让游戏保持活动并渲染。

中心防火墙需允许 TCP 8000 和 UDP 9999，终端允许 UDP 探测回应；跨子网或 Docker Desktop 广播受限时使用 `CENTER_URL`。广播发现不传输口令，实际连接仍需认证。若使用 HTTP，适合可信局域网；通过 HTTPS 中心或反向代理可加密跨网络传输。两端系统时间需同步，过期指令会被拒绝。

## 隐藏后台运行

先执行 `uv sync --locked`，然后在 `terminal/` 目录执行。脚本使用 uv 创建的本目录 `.venv\Scripts\pythonw.exe`，继承当前环境变量：

```powershell
.\start.ps1 --terminal-id game-pc
Get-Content .\.runtime\terminal.log -Tail 30
.\stop.ps1
```

也可用 `-Python "C:\Python312\pythonw.exe"` 指定解释器。启动日志和 PID 记录保存在 `terminal/.runtime/`。停止脚本发出停止文件通知，终端先释放手柄、断开网络，再停止采集；脚本校验进程启动时间，避免操作复用 PID 的其他进程。此方式是登录用户桌面内的后台进程，需要用户保持登录。

若 PowerShell 默认禁止本地脚本，可用单次进程参数运行，不需要修改系统执行策略：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 --terminal-id game-pc`；停止时将文件名改为 `stop.ps1`。

## 操作约束

每条操作完整替换手柄状态：`steering` 为 `[-1, 1]`，`throttle`、`brake` 为 `[0, 1]`，省略的轴归零，省略的 `buttons` 释放所有按钮。`duration_ms` 默认为 200，范围为 0–5000；0 表示立即全部释放。支持 `A B X Y LB RB BACK START GUIDE LTHUMB RTHUMB DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT`。持续驾驶时，AI 应在每条操作到期前继续发送新操作。

独立于网络事件循环的计时器在操作到期时释放输入；断线、正常退出也释放输入。旧计时器不会释放新操作。已经过期或同一连接内重复的操作会被拒绝，不重放重连前的操作。硬件驱动执行失败会回传失败确认，中心可据此终止当前训练步骤。

原生 Windows 捕获 SDK 在独立子进程内运行。子进程通过固定大小共享内存只保留一张最新 JPEG，状态通道也有容量上限；原生崩溃或长时间无响应时父进程自动重启采集子进程，网络连接和手柄计时器继续工作。采集使用实际可见窗口 HWND，默认排除 Steam 的 `[Streaming]` 启动窗口。停止时先请求子进程退出；若 SDK 卡住，只终止本终端创建的采集进程及其子进程。

## 无硬件联调

```powershell
uv run auto-driver-terminal --center http://127.0.0.1:8000 --terminal-id mock-pc --mock
uv run python -m unittest discover -s tests -v
```

`--mock` 生成带时间戳的测试画面并使用内存手柄，不操作真实游戏。真实模式不会自动回退成模拟模式。通过中心页面或 API 可验证画面、执行确认、`step` 新观察和重连；真实游戏和手柄驱动仍需在游戏电脑上验证。

## 模块

- `capture.py`：Windows 窗口采集、JPEG 编码和单帧缓存；网络变慢时不积压历史画面。
- `control.py`：虚拟 Xbox 360 映射、参数校验、完整状态替换及输入超时释放。
- `discovery.py`：UDP 自动发现和中心地址校验。
- `client.py`：认证连接、画面上传、心跳、操作确认与断线重连。
- `__main__.py`：CLI 入口；`uv run auto-driver-terminal --help` 查看参数。

二进制画面格式为 `b"ADF1" + struct.pack("!d", captured_at) + JPEG`，其中时间戳在终端采集回调内生成。`step` 可使用同一终端的操作执行时间与采集时间判断观察是否晚于操作保持时段。
