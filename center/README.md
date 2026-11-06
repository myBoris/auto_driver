# 自动驾驶服务中心

本目录是独立的 uv 项目，可以单独复制到中心电脑。Python 服务依赖 aiohttp 和 python-dotenv；Docker 中另运行固定版本的 MediaMTX，负责转发终端发布的 H.264 视频。中心不需要游戏、ViGEmBus、GPU 或 terminal 目录；含图像解码的接入示例另通过 `examples` 扩展安装 Pillow。

浏览器视频与 AI 图像观察分别传输：终端将原始画面送入硬件 H.264 编码器，经 RTSP 发布到 MediaMTX，浏览器通过 WebRTC 播放；训练仍通过原有 JPEG 观察与动作 API 工作。浏览器不再逐帧请求 JPEG，视频也不会因训练读取图像而排队。

## Docker 部署

Docker 所需文件全部位于本目录，构建上下文也是本目录。首次部署时复制示例配置，将 `CENTER_TOKEN` 改为自己的配对口令，并在游戏电脑的 `terminal/.env` 中设置相同值：

```powershell
cd center
Copy-Item .env.example .env
# 编辑 .env 中的 CENTER_TOKEN
docker compose up -d --build
docker compose ps
docker compose logs -f
```

Linux Shell 可用 `cp .env.example .env`。Docker Compose 从本目录 `.env` 读取配置，镜像使用 uv 按 `uv.lock` 安装中心依赖。

默认 `CENTER_HOST=0.0.0.0`，监听所有 IPv4 网卡。本机打开 `http://localhost:8000`，其他电脑使用中心的任一可达 IP；页面会自动连接并播放在线终端的视频，无需输入口令或点击连接。

中心防火墙需放行 TCP 8000（页面、控制、训练和 WHEP）、UDP 9999（发现）、TCP 8554（终端 RTSP 发布）及 UDP/TCP 8189（WebRTC 媒体）。MediaMTX 的 8889 协商端口只在 Docker 内部提供给中心代理，不直接对外发布；媒体容器无需 GPU，也不重新编码终端视频。

默认将 `CENTER_ADVERTISE_URL` 留空：终端使用 UDP 探测回复的来源 IP 连接中心，无需固定局域网地址或配置 `CENTER_URL`。UDP 广播通常只覆盖同一网段；跨网段或 Docker Desktop 收不到 UDP 回复时，终端直接传 `--center http://中心电脑IP:8000`，或在 `terminal/.env` 配置 `CENTER_URL`。

`CENTER_ADVERTISE_URL` 仅用于反向代理或 NAT 场景下公布固定的可达入口，例如 `https://driver.example.com`；它不改变监听地址，也不限制访问来源。`0.0.0.0` 是监听通配地址，不能作为浏览器、终端的连接地址或通告地址。

默认只运行一个中心实例；终端状态与最新画面保存在当前进程内存，不要启动多个独立 worker。重启中心后终端会自动重新连接，历史帧不会持久化。Windows 原生采集与恢复由终端独立子进程处理，见 [terminal/README.md](../terminal/README.md)。

## 本机 uv 启动

需要 uv 和 Python 3.12；uv 可以自动准备 Python。首次使用同样先从 `.env.example` 创建本目录 `.env` 并设置配对口令，然后执行：

```powershell
cd center
uv sync --locked
uv run auto-driver-center
```

入口自动读取本目录 `.env`，不依赖当前工作目录；已有进程环境变量优先，CLI 参数可覆盖配置。可选参数：`--host`、`--port`、`--token`、`--discovery-port`、`--advertise-url`、`--no-discovery`。

本机启动 Python 服务时，`CENTER_MEDIA_HTTP_URL` 默认指向 `http://127.0.0.1:8889`。需要同时运行 MediaMTX，并把其 `authHTTPAddress` 指向此中心的 `/internal/media/auth`；仅调试控制与训练时，可在 `.env` 设置 `CENTER_MEDIA_ENABLED=0`。完整视频部署推荐直接使用本目录 Compose，它已配置好容器之间的服务地址。

## 视频与地址转换

中心在经过 Bearer 认证的 WebSocket `welcome.video` 中下发终端专用 RTSP 发布地址，使用终端连接中心时的主机名和 8554 端口。配对口令只存在于这条认证连接和服务器内部，浏览器获得的视频元数据不包含发布地址或口令。

播放器通过同源 Cookie 请求视频元数据，收集完整 ICE 后向 `video/whep` POST `application/sdp`。中心代为添加 MediaMTX reader 认证，返回 SDP 应答和本地会话 `Location`；浏览器结束播放时 DELETE 此本地地址。上游会话地址不会暴露给浏览器，并绑定终端连接代次；终端离线、连接被替换或中心关闭时也会回收会话。

Docker 中的 ICE host 候选通常是容器网桥地址。中心将其改为浏览器当前访问中心的主机地址和已发布的 8189 端口，同时保留 ICE/DTLS 凭据、UDP/TCP 类型和中继候选，因此不必固定某张网卡的 IP。一般无需设置 `CENTER_MEDIA_ICE_HOST`；有独立媒体公网入口时才用它覆盖候选主机。HTTP 反向代理只转发协商请求，WebRTC 的 8189 媒体端口仍需直接可达。

终端编码器未就绪时，`video` 元数据返回 `available=false` 及 `reason/message`，页面据此等待并重试。视频帧率受游戏实际渲染、硬件编码和网络影响；配置 60/120 FPS 表示目标上限，不表示游戏实际达到该帧率。

## HTTP API

监看页面在局域网内直接可用。访问 `/` 会自动设置 HttpOnly、SameSite=Strict 的 `auto_driver_dashboard` 会话 Cookie，页面通过同源请求调用业务 API；会话失效后页面自动请求 `/dashboard-session` 恢复。页面和会话响应均不包含 `CENTER_TOKEN`，携带 Cookie 的控制请求会校验所提供的 Origin。

训练程序继续使用请求头 `Authorization: Bearer <CENTER_TOKEN>` 调用 API。终端的 `/ws/terminal` 只接受 Bearer 配对认证，浏览器 Cookie 不能注册终端。配对口令不会通过 UDP 广播。HTTP 适用于可信局域网；需要经过不可信网络时可在中心前配置 HTTPS 代理，终端支持 HTTPS。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/dashboard-session` | 自动建立或恢复浏览器会话 |
| GET | `/api/terminals` | 所有已知终端及在线状态 |
| GET | `/api/terminals/{id}` | 终端采集、手柄和连接状态 |
| GET | `/api/terminals/{id}/frame` | 最新 JPEG，附帧序号和时间戳 |
| GET | `/api/terminals/{id}/frame?after=序号&timeout=5` | 等待更新帧，最多 30 秒 |
| GET | `/api/terminals/{id}/stream` | MJPEG 连续画面 |
| GET | `/api/terminals/{id}/video` | H.264 视频可用性、状态说明和同源 WHEP 地址 |
| POST | `/api/terminals/{id}/video/whep` | 使用 `application/sdp` 协商 WebRTC，返回 201 与本地 Location |
| DELETE | `/api/terminals/{id}/video/sessions/{session}` | 回收本地会话及对应上游媒体会话 |
| POST | `/api/terminals/{id}/actions` | 下发完整手柄状态，等待终端执行确认 |
| POST | `/api/terminals/{id}/release` | 立即释放全部输入，取消此前排队和等待中的操作 |
| POST | `/api/terminals/{id}/step` | 执行动作并等待该动作持续时间结束后采集的图像 |

动作请求示例：

```json
{"steering": 0.2, "throttle": 0.5, "brake": 0, "buttons": [], "duration_ms": 200}
```

`steering` 范围 -1～1，负数向左；油门、刹车为 0～1。`duration_ms` 为 0～5000 的整数，默认 200，到期终端自动回中；0 表示立刻释放。每条动作替换全部输入，省略字段归零。支持按钮 `A B X Y LB RB BACK START GUIDE LTHUMB RTHUMB DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT`。

成功返回 `terminal_id`、`command_id` 和 `ack`。`ack.ok=true` 表示虚拟手柄调用已成功，不能表示游戏物理结果。终端拒绝执行返回 502；确认或等帧超时返回 504；离线或被释放操作取消返回 409；未知终端返回 404；无可用新鲜画面返回 503；错误请求返回 400。

step 请求：

```json
{"action": {"steering": 0, "throttle": 0.2, "duration_ms": 100}, "observation_timeout": 5}
```

step 响应包含 `ack` 和 `observation`；后者含 `sequence`、`captured_at`、`received_at`、`content_type`、`jpeg_base64`。图像的终端采集时间至少为 `ack.applied_at + duration_ms / 1000`。图像编码和网络传输会带来延迟，不能保证游戏恰好推进固定帧数。中心不提供奖励函数、比赛重置或游戏遥测，这些由训练代码实现。

`frame` 响应头为 `X-Frame-Sequence`、`X-Frame-Timestamp`（终端采集时间）、`X-Frame-Received-At`（中心接收时间）。断开连接立即清除画面，超过 3 秒未收到更新的画面不会作为最新帧返回。终端与中心应启用系统时间同步，以便判断操作截止时间；step 的动作时间和采集时间都来自同一终端。

`frame/step` 是 AI 观察接口，采样频率与高帧率视频独立。`stream` 保留作 JPEG 诊断：只读取最新帧，关闭代理缓冲，慢客户端写入超时后中断连接，避免旧帧持续积压。正常监看由 WebRTC 播放器完成。

## 后端模块分工

```text
app.py                         应用装配、依赖注入
configuration/settings.py     配置默认值、环境变量和校验
api/middleware.py              Cookie/Bearer/Origin 与统一错误响应
api/v1/terminals.py            终端、动作、训练观察 HTTP 适配
api/v1/media.py                WHEP、会话删除和内部媒体认证适配
api/terminal_socket.py         终端 WebSocket 网络读写
services/terminals.py          注册替换、完整动作与 step 业务
services/terminal_session.py   最新帧、匹配确认、超时及释放取消
services/media.py              发布授权、视频状态、会话归属和回收
domain/                       帧模型、动作校验、媒体路径和 ICE 规则
infrastructure/               UDP、MediaMTX HTTP 访问和资源生命周期
static/                       页面结构、样式、API、播放器与控制模块
```

请求依次经过 API 适配器、业务服务，再调用领域规则与网络基础设施。`config.py`、`discovery.py`、`protocol.py`、`registry.py` 保留为旧导入路径的兼容入口，不重复实现业务逻辑。`client.py` 是供外部训练程序使用的轻量 SDK。

## Python 训练接入

`center.client.CenterClient` 提供异步的 `terminals()`、`observe()`、`action()`、`step()` 和 `release()`。`Observation.jpeg` 为原始 JPEG 字节，可转换为 PIL 图像或模型张量。

```python
import asyncio
import os
from dotenv import load_dotenv
from center.client import CenterClient

load_dotenv(".env")  # 在 center 目录运行时读取配对口令。

async def main():
    async with CenterClient("http://127.0.0.1:8000", os.environ["CENTER_TOKEN"]) as client:
        terminal_id = "game-pc"
        try:
            observation = await client.observe(terminal_id)
            observation, ack = await client.step(terminal_id, {
                "steering": 0.0, "throttle": 0.2, "duration_ms": 100,
            })
            print(observation.sequence, len(observation.jpeg), ack["ok"])
        finally:
            await client.release(terminal_id)

asyncio.run(main())
```

在本目录运行中立动作示例（通过 `examples` 扩展安装 Pillow 图像解码，`--env-file` 为示例进程加载口令，不会主动加油）：

```powershell
uv run --env-file .env --extra examples python -m center.examples.train_client --terminal-id game-pc --steps 10
```

训练程序与监看页面共享同一个终端手柄，应安排好控制时段。释放会取消此前尚未结束的操作；释放后新提交的动作仍会正常执行。

## 终端协议与测试

终端通过带 Bearer 认证的 `/ws/terminal` 注册，`hello.protocol=1`，随后中心发送 `welcome`，其中 `video` 包含硬件视频发布配置。AI 观察消息仍为二进制：`ADF1` + 网络字节序 float64 UNIX 采集时间 + JPEG，最大 8 MiB。终端每 5 秒发送 heartbeat，报告采集、手柄和视频状态；中心下发带唯一 ID 与过期时间的 action，终端执行后 ack。中心不会重放未确认的旧动作。

在本目录执行：

```powershell
uv run python -m unittest discover -s tests -v
```

页面支持按住鼠标或聚焦按钮后按住 Enter；启用键盘后可使用 WASD / 方向键。Space 始终释放输入，包括滑块或终端选择框获得焦点时。滑块获得焦点后，方向键仍用于调整滑块数值。

前端事件回归使用 Node 标准库，在本目录执行 `node --test tests/test_dashboard.cjs`，覆盖全部控制按钮、组合输入、焦点变化、释放和会话恢复。
