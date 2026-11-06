"""实验与终端会话的绑定规则，防止重连后的帧混入旧实验。"""

from src.integrations.center import CenterClient


async def resolve_terminal(client: CenterClient, requested_id: str | None = None) -> dict:
    """选出一个在线终端；有多个候选时要求明确 ID，避免把操作发往错误对象。"""
    online = [item for item in await client.list_terminals() if item.get("connected") is True]
    if requested_id:
        selected = next((item for item in online if item["terminal_id"] == requested_id), None)
        if selected is None:
            raise RuntimeError(f"终端 {requested_id} 不在线，请先启动对应 terminal")
        return selected
    if len(online) != 1:
        raise RuntimeError("需要恰好一个在线终端，或通过 --terminal-id / AI_TERMINAL_ID 指定终端")
    return online[0]


async def ensure_session(client: CenterClient, terminal: dict) -> None:
    """采集及动作前后检查连接代次，断线或连接替换时终止本次实验。

    Center 的帧编号不能替代会话标识；同一台电脑重启 terminal 后，其游戏状态
    可能已经改变。这里选择结束数据片段，由调用者另开实验，避免悄悄拼接时间序列。
    """
    current = await client.get_terminal(terminal["terminal_id"])
    if not current.get("connected") or current.get("session_id") != terminal.get("session_id"):
        raise RuntimeError("终端连接已改变，本次数据片段结束；请重新选择终端并开始新片段")
