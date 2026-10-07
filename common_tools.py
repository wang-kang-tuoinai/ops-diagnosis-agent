"""正式诊断 Agent 与学习示例共用的时间工具。"""
from datetime import datetime


def get_current_time() -> str:
    """
    获取当前系统的本地时间。
    Returns:
    包含年月日时分秒的字符串，例如 "2026-08-03 14:47:19"
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
