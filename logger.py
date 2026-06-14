"""日志模块 - 同时输出到控制台和文件"""
import logging
import os
from datetime import datetime


def setup_logger(symbol: str, log_dir: str = "logs") -> logging.Logger:
    """为指定交易对创建独立 logger，每次运行生成带时间戳的日志文件"""
    os.makedirs(log_dir, exist_ok=True)
    logger_name = f"sp_{symbol}"
    logger = logging.getLogger(logger_name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台 - 显示 INFO 及以上（关键事件）
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件 - 完整日志（DEBUG级别，包含所有状态行）
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"{symbol}_{timestamp}.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger
