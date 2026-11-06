# 游戏终端

终端在运行《Forza Horizon 5》的 Windows 电脑上执行。游戏窗口原始帧分成两条独立管道：硬件 H.264 视频经 RTSP 发布到中心的 MediaMTX，再由浏览器通过 WebRTC 播放；AI 观察按独立频率压缩为带采集时间戳的 JPEG，经已有认证 WebSocket 上传，继续支持 `frame` 和 `step` API。

操作通过 `Python → vgamepad → ViGEmBus → 虚拟 Xbox 360 手柄 → 游戏` 执行。网络和手柄计时器运行在父进程，WGC、OpenCV 和视频发布运行在隔离子进程，采集崩溃或编码阻塞不会阻塞输入到期释放。

## 双击启动和关闭

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases)，按驱动安装器要求重启。`terminal/` 可以单独复制到游戏电脑，不依赖项目其他目录。

1. 复制 `.env.example` 为 `.env`，将 `CENTER_TOKEN` 设为与中心 `.env` 相同的配对口令。
2. 打开游戏，双击 `启动终端.cmd`。首次自动执行本目录 `uv sync --locked`，创建独立环境后隐藏运行；已运行时重复点击直接返回成功。
3. 双击 `关闭终端.cmd`，停止发布和采集、释放手柄并退出。重复关闭也成功。

正常日志位于 `.runtime/terminal.log`。失败时窗口保留错误信息；自动化可设置 `TERMINAL_LAUNCHER_NO_PAUSE=1` 禁止失败暂停。脚本校验 PID 和进程启动时间，不操作复用相同 PID 的其他进程。

默认通过 UDP 9999 自动发现中心，随后认证连接；口令不在 UDP 中广播。中心下发本终端的 RTSP 发布地址，断线撤销旧发布配置，重连后按新地址启动。跨子网或 Docker Desktop 广播受限时，在 `.env` 设置 `CENTER_URL=http://中心地址:8000`。多终端应使用不同的 `TERMINAL_ID`。

## 视频和 AI 观察配置

```dotenv
TERMINAL_FPS=60
TERMINAL_OBSERVATION_FPS=10
TERMINAL_VIDEO_ENABLED=true
TERMINAL_VIDEO_BITRATE=8M
TERMINAL_VIDEO_ENCODER=auto
```

视频目标帧率默认 60，范围 1–120；AI JPEG 观察默认 10，范围同样为 1–120，两者独立。`8M` 表示每秒 8,000,000 bit 的视频目标码率。实际帧率取决于游戏渲染、图像尺寸、硬件和网络，配置值不是测量值。JPEG 质量只影响 AI 观察，不决定视频质量。

```powershell
cd terminal
uv sync --locked
uv run auto-driver-terminal --fps 60 --observation-fps 10 --video-bitrate 8M
# 可选提高视频目标，保持训练观察频率较低。
uv run auto-driver-terminal --fps 120 --observation-fps 10
```

默认 `auto` 实际编码测试帧，依次探测 `h264_nvenc`、`h264_qsv`、`h264_amf`；可用 `--video-encoder` 固定一种。失败明确显示在 `status.video.error`，不会静默切换软件 H.264。`status.video.ready` 只有在 FFmpeg 确实编码并发布帧后才为真，`encoder` 显示实际硬件实现。

FFmpeg 由 uv 依赖 `imageio-ffmpeg` 随环境安装，无需手动下载或配置系统 PATH。特殊环境可在 `.env` 设置 `FFMPEG_BINARY`，或传 `--ffmpeg-binary` 指向自己的可执行文件。硬件编码仍需对应显卡和驱动。RTSP 凭据只保存在运行配置中，状态和日志会脱敏。

不需要视频或无可用硬件时，显式设置 `TERMINAL_VIDEO_ENABLED=false` 或使用 `--no-video`，保留 JPEG 观察与控制 API。中心需部署 MediaMTX，并开放 RTSP TCP 8554、HTTP 8000 和配置的 WebRTC ICE 端口；媒体公网地址及 Docker UDP 映射由中心配置。

## 采集与控制约束

建议游戏使用窗口化或无边框模式。`--window-title` 是忽略大小写的标题子串，优先精确标题，默认排除 Steam 的 `[Streaming]` 启动窗口。终端不切换游戏焦点；游戏失焦是否暂停及接受手柄输入取决于游戏设置。最小化、锁屏或非交互会话可能停止游戏渲染，需要持续画面时保持用户登录且窗口继续渲染。

原始像素在回调内复制到拥有内存的 BGR 图像，再交给视频及观察工作槽。两条管道都只保留最新待处理图像；编码器或网络慢时覆盖旧帧，避免排队累积延迟。FFmpeg 写管道由专用线程处理，采集回调不等待 RTSP。JPEG 只按观察频率编码，不再每个视频帧编码一次。

每条动作完整替换手柄状态：`steering` 为 `[-1, 1]`，`throttle`、`brake` 为 `[0, 1]`；省略的轴归零，省略的按钮释放。`duration_ms` 默认 200，范围 0–5000，0 表示立即全部释放。支持 `A B X Y LB RB BACK START GUIDE LTHUMB RTHUMB DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT`。

ACK 成功执行时间在驱动提交成功后记录；`step` 等待采集时间晚于动作保持时段的观察。独立计时器在到期时释放输入，旧计时器不会覆盖新动作；过期及同一连接内重复的命令会被拒绝。视频断流不伪造动作确认或观察时间戳。两端系统时间应同步。

采集管理线程监督原生子进程，异常自动退避重启；发布地址或授权变化时停止旧子进程并立即重建。正常退出先协作停止；SDK 卡住时仅回收本终端拥有的采集与 FFmpeg 进程树，父进程继续负责输入释放。

视频发布另有独立进度看门狗：输入仍活跃时，首次 8 秒无输出或正常发布后 5 秒无帧进展，会终止卡住的编码器以解除管道写入阻塞，随后重新连接。停止终端后不会再次启动。Windows Job 将 FFmpeg 归属到采集进程，即使原生进程意外崩溃也会自动回收编码器，避免旧中心继续收到孤儿推流。

## 无游戏输入验证

```powershell
uv run auto-driver-terminal --mock --terminal-id video-qa
uv run python -m unittest discover -s tests -v
```

`--mock` 仅把图像换成带帧号的合成图、手柄换成内存后端。启用视频时仍走实际硬件编码和 RTSP/WebRTC 链路，可检查 H.264、浏览器解码帧率、重连以及 AI API，同时不操作游戏。纯无硬件联调用 `--mock --no-video`。

## 功能分层

| 目录 | 职责 |
| --- | --- |
| `config/` | `.env`、CLI、连接配置及参数校验 |
| `services/` | 组装并统一启动、退出各项服务 |
| `capture/` | WGC/合成图像、原始帧分流、AI JPEG 采样、共享内存和进程监督 |
| `video/` | 实际硬件探测、FFmpeg 最新帧写入、RTSP 发布、状态与凭据脱敏 |
| `control/` | 动作协议、ViGEmBus 适配及独立输入计时器 |
| `network/` | UDP 发现、认证 WebSocket、观察上传、心跳、动作 ACK 及重连 |

`__main__.py` 只保留入口，`client.py`、`discovery.py` 保留旧导入兼容。`terminal.capture` 和 `terminal.control` 是功能包，稳定导出原接口。AI 观察格式仍为 `b"ADF1" + struct.pack("!d", captured_at) + JPEG`，训练程序无需跟随视频协议改变。
