# 自动驾驶：服务中心与地平线 5 终端

根目录新增两个独立业务目录，分别通过 uv 管理依赖、锁文件与启动入口，可以单独复制到不同电脑部署。

```text
auto-driver/
├── center/                  服务中心：监看页面、自动发现、控制与 AI API
│   ├── pyproject.toml       独立 uv 项目
│   ├── uv.lock
│   ├── Dockerfile          Docker 配置仅放在 center 中
│   ├── compose.yml
│   ├── .env.example
│   ├── client.py            Python 训练客户端
│   ├── examples/            API 接入示例
│   └── tests/
├── terminal/                Windows 游戏电脑：窗口采集与虚拟手柄
│   ├── pyproject.toml       独立 uv 项目
│   ├── uv.lock
│   ├── .env.example         与中心配对的本机配置
│   ├── capture.py
│   ├── control.py
│   ├── start.ps1            隐藏后台启动
│   ├── stop.ps1             优雅停止并释放输入
│   └── tests/
└── src/model/               原有模型代码
```

链路：终端通过 UDP 探测中心，认证后自动建立 WebSocket 连接；终端上传游戏 JPEG，中心显示画面并提供训练 API；AI 或监看页面的操作由中心发送给终端，再通过 `Python → vgamepad → ViGEmBus → 虚拟 Xbox 360 手柄 → 游戏` 执行。断线自动重连，指令到期、退出或断线时释放手柄输入。

## 中心电脑

在 **center 目录中**使用 Docker 部署。首次配置时复制示例文件，将 `.env` 中的 `CENTER_TOKEN` 改为自己的配对口令：

```powershell
cd center
Copy-Item .env.example .env
# 编辑 .env 中的 CENTER_TOKEN
docker compose up -d --build
docker compose ps
```

中心默认通过 `CENTER_HOST=0.0.0.0` 监听所有 IPv4 网卡；本机浏览器打开 `http://localhost:8000`，其他电脑使用中心的任一可达 IP。页面自动连接并显示在线终端和游戏画面，无需输入口令或点击连接。浏览器会话由中心通过同源 HttpOnly Cookie 自动建立；配对口令只用于终端和训练程序。中心不依赖终端目录或根目录的 PyTorch 环境，本机 uv 启动方式见 [center/README.md](center/README.md)。

## Windows 游戏电脑

日常使用可在 `terminal` 文件夹双击 `启动终端.cmd` 和 `关闭终端.cmd`；启动入口自动用 uv 准备环境、读取 `.env` 并在后台运行。

安装 ViGEmBus，启动《Forza Horizon 5》，再在终端目录执行：

```powershell
cd terminal
Copy-Item .env.example .env
# 编辑 .env，让 CENTER_TOKEN 与 center/.env 中的值一致
uv sync --locked
uv run auto-driver-terminal --terminal-id game-pc
```

两端启动入口会自动读取各自目录的 `.env`。默认将中心的 `CENTER_ADVERTISE_URL` 留空，终端通过 UDP 自动探测同网段中心，使用回复的来源 IP 建立连接，无需固定局域网地址或设置 `CENTER_URL`。广播受限或跨网段时可直接指定：

```powershell
uv run auto-driver-terminal --terminal-id game-pc --center http://中心电脑IP:8000
```

反向代理或 NAT 需要固定连接入口时，才设置 `CENTER_ADVERTISE_URL`。它只指定探测回复公布的入口，不影响中心监听地址或访问来源；不要将 `0.0.0.0` 作为连接或通告地址。

隐藏后台运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 --terminal-id game-pc
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stop.ps1
```

后台指采集控制程序没有前台窗口；游戏仍需运行在已登录用户桌面。推荐窗口或无边框模式。游戏自身在失焦、最小化或锁屏时可能暂停或停止渲染，虚拟手柄无法改变游戏的这些限制。

原生窗口采集运行在独立子进程中，只保留最新一帧；采集崩溃或卡住时自动重建子进程，网络连接与手柄超时释放继续运行。详细说明见 [terminal/README.md](terminal/README.md)。

## 无游戏联调与训练 API

中心启动后，可在终端目录运行模拟画面和模拟手柄，不依赖游戏与驱动：

```powershell
uv run auto-driver-terminal --mock --terminal-id game-pc --center http://127.0.0.1:8000
```

训练端可获取最新画面，发送完整手柄动作，或调用 `step` 等待动作持续时间结束后采集的新帧。训练程序通过 `Authorization: Bearer <CENTER_TOKEN>` 调用 API；监看页面使用自动建立的浏览器会话。

| 接口 | 用途 |
| --- | --- |
| `GET /api/terminals` | 终端列表和状态 |
| `GET /api/terminals/{id}/frame` | 最新 JPEG / 等待新帧 |
| `GET /api/terminals/{id}/stream` | MJPEG 视频流 |
| `POST /api/terminals/{id}/actions` | 执行动作并返回终端确认 |
| `POST /api/terminals/{id}/release` | 释放输入并取消先前未完成的操作 |
| `POST /api/terminals/{id}/step` | 动作 + 新鲜图像观察 |

动作示例：`{"steering":0.2,"throttle":0.5,"brake":0,"buttons":[],"duration_ms":200}`。每条动作替换全部输入，默认持续 200 毫秒，最多 5 秒。

在中心目录运行 Python 接入示例，使用 `--env-file` 将 `.env` 中的口令提供给示例进程：

```powershell
uv run --env-file .env --extra examples python -m center.examples.train_client --terminal-id game-pc --steps 10
```

详细配置、API、网络与 Docker 说明见 [center/README.md](center/README.md)；游戏采集、ViGEmBus 和后台启动说明见 [terminal/README.md](terminal/README.md)。默认需放行中心 TCP 8000 和 UDP 9999；两台电脑应开启系统时间同步。

## 验证

在各自目录执行，分别使用各自 uv 环境：

```powershell
uv run python -m unittest discover -s tests -v
```

## 原有模型环境

根目录的 `pyproject.toml`、`uv.lock` 和 `src/model/` 保留，用于原有 PyTorch / CUDA 模型开发。中心与终端启动时不会安装这组模型依赖。在根目录执行 `uv sync --locked`、`uv run python check_env.py` 可安装和检查原有 GPU 环境；模型接口见 [src/model/README.md](src/model/README.md)。
