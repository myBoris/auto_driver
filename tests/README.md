# 道路与方向演示发送器

在仓库根目录运行，无需安装额外依赖：

```powershell
python tests/send_perception.py
```

脚本默认读取 `center/.env` 的配对口令，连接 `http://127.0.0.1:8000`，选择唯一在线终端，每秒发送 5 次随机道路轮廓、中心线和方向预测。打开管理页面即可看到标注为“演示数据”的叠加；`Ctrl+C` 停止，2 秒后自动清除。

有多个在线终端时，明确指定目标；也可调整地址、发送频率和运行时长：

```powershell
python tests/send_perception.py --terminal-id DESKTOP-23UGVE3
python tests/send_perception.py --center http://192.168.1.10:8000 --fps 10 --duration 30
python tests/send_perception.py --help
```

`--env-file` 可指定其他配置文件；`CENTER_URL`、`CENTER_TOKEN`、`AI_TERMINAL_ID` 环境变量覆盖文件值，命令行参数优先。自动选定终端后会保持该终端，离线时等待重连，不会改发到其他终端。脚本只调用终端列表和 `perception` 接口，不发送驾驶操作或释放命令。
