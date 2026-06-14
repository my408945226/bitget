"""状态持久化模块 - PKL 格式 (栈式设计)

状态结构参考 OKX 策略，使用 stack_top 而非 layers 数组。
核心字段：
  - stack_top: 栈顶入场价 (float)
  - opens: 累计加仓次数 (int)
  - closes: 累计平仓次数 (int)
  - pending_sell_ord_id, pending_sell_px: 当前挂单的 SELL 单
  - pending_buys: {ord_id: {entry_px, target_px}} 所有待成交的 BUY 单映射
"""
import pickle
import time
import os
from pathlib import Path
from typing import Optional, Dict, Any

# P2: 跨平台文件锁支持
try:
    # Windows
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

try:
    # Linux/Mac
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


class State(dict):
    """状态字典"""
    def __init__(self, symbol: str):
        super().__init__()
        self["symbol"] = symbol
        self["stack_top"] = 0.0
        self["opens"] = 0
        self["closes"] = 0
        self["pending_sell_ord_id"] = None
        self["pending_sell_px"] = None
        self["pending_buys"] = {}
        self["last_action_time"] = 0.0


def state_path(symbol: str) -> Path:
    """返回状态文件路径 (PKL 格式)"""
    return Path(f"state_short_pyramid_{symbol}.pkl")


def load_state(symbol: str) -> State:
    """加载 PKL 状态，不存在则创建新的"""
    path = state_path(symbol)
    if path.exists():
        try:
            with open(path, "rb") as f:
                state_dict = pickle.load(f)
            # 确保所有字段都存在（向后兼容）
            state = State(symbol)
            state.update(state_dict)
            return state
        except Exception as e:
            print(f"加载 state 失败 {path}: {e}, 创建新的")
    return State(symbol)


def save_state(state: State) -> None:
    """保存状态（带文件锁）"""
    path = state_path(state["symbol"])
    with open(path, "wb") as f:
        if HAS_FCNTL:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        elif HAS_MSVCRT:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        pickle.dump(dict(state), f)
        f.flush()
        os.fsync(f.fileno())
        if HAS_FCNTL:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        elif HAS_MSVCRT:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def delete_state(symbol: str) -> None:
    """删除状态文件"""
    path = state_path(symbol)
    if path.exists():
        path.unlink()
