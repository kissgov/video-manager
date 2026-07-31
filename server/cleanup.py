# -*- coding: utf-8 -*-
"""
output 目录自动清理:按文件名时间戳删除过期文件。
文件名格式:{通道}_{YYYYMMDDHHMMSS}_{YYYYMMDDHHMMSS}.ext
解析策略:用正则在文件名中匹配第一个 14 位数字串作为开始时间。
"""
import os
import re
import logging
from datetime import datetime, timedelta
from . import config
from .config import log

# 匹配 14 位连续数字(YYYYMMDDHHMMSS)
_TS_RE = re.compile(r'(\d{14})')


def parse_filename_date(filename: str):
    """
    从文件名提取开始时间戳,返回 datetime 或 None。
    支持: 00_20260731094443_20260731095216.mp4 → datetime(2026,7,31,9,44,43)
          20260731094443.mp4                     → datetime(2026,7,31,9,44,43)
          abc.mp4                                → None
    """
    m = _TS_RE.search(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def cleanup_output(retention_days: int) -> dict:
    """
    扫描 output_dir,删除文件名时间戳超过 retention_days 天的文件。

    返回:
        {
            "scanned": int,       # 扫描的文件总数
            "deleted": int,       # 已删除的文件数
            "skipped": int,       # 跳过的文件数(文件名不符合格式)
            "freed_bytes": int,   # 释放的字节数
            "errors": list[str],  # 删除失败的错误信息
        }
    """
    result = {"scanned": 0, "deleted": 0, "skipped": 0, "freed_bytes": 0, "errors": []}
    if retention_days <= 0:
        return result

    output_dir = str(config.OUTPUT_DIR)
    if not os.path.isdir(output_dir):
        log(f"cleanup: output_dir 不存在,跳过清理: {output_dir}")
        return result

    cutoff = datetime.now() - timedelta(days=retention_days)
    log(f"cleanup: 开始清理 output 目录,保留 {retention_days} 天,截止日期 {cutoff.strftime('%Y-%m-%d')}")

    try:
        entries = os.listdir(output_dir)
    except OSError as e:
        log(f"cleanup: 无法读取 output 目录: {e}", level=logging.WARNING)
        result["errors"].append(str(e))
        return result

    for name in entries:
        filepath = os.path.join(output_dir, name)
        if not os.path.isfile(filepath):
            continue
        result["scanned"] += 1

        file_date = parse_filename_date(name)
        if file_date is None:
            result["skipped"] += 1
            continue

        if file_date < cutoff:
            try:
                size = os.path.getsize(filepath)
                os.remove(filepath)
                result["deleted"] += 1
                result["freed_bytes"] += size
                log(f"cleanup: 删除过期文件 {name} (日期 {file_date.strftime('%Y-%m-%d')}, {size} 字节)")
            except OSError as e:
                result["errors"].append(f"{name}: {e}")
                log(f"cleanup: 删除失败 {name}: {e}", level=logging.WARNING)

    log(f"cleanup: 完成,扫描 {result['scanned']},删除 {result['deleted']},"
        f"跳过 {result['skipped']},释放 {result['freed_bytes']} 字节")
    return result
