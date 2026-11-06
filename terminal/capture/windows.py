"""只定位可见游戏 HWND；不激活窗口，也不创建控制器。"""

def _find_window(window_title: str) -> int | None:
    """优先精确标题，避免在游戏尚未打开时反复调用原生 SDK。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    matches: list[tuple[bool, int]] = []
    desired = window_title.casefold()

    @callback_type
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value.casefold()
            if desired in title:
                # Steam 串流启动窗口与本地游戏不同；默认标题不能误匹配该窗口。
                if "[streaming]" in title and "[streaming]" not in desired:
                    return True
                matches.append((title != desired, int(hwnd)))
        return True

    user32.EnumWindows(visit, 0)
    return min(matches)[1] if matches else None
