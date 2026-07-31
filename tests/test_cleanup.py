# -*- coding: utf-8 -*-
"""cleanup 模块单元测试。"""
import os
import sys
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

# 让 tests/ 能 import server 包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from server.cleanup import parse_filename_date, cleanup_output


class TestParseFilenameDate:
    def test_standard_format(self):
        """标准格式: 00_20260731094443_20260731095216.mp4"""
        d = parse_filename_date("00_20260731094443_20260731095216.mp4")
        assert d == datetime(2026, 7, 31, 9, 44, 43)

    def test_no_prefix(self):
        """无通道前缀: 20260731094443.mp4"""
        d = parse_filename_date("20260731094443.mp4")
        assert d == datetime(2026, 7, 31, 9, 44, 43)

    def test_short_name(self):
        """短文件名无 14 位数字: abc.mp4 → None"""
        assert parse_filename_date("abc.mp4") is None

    def test_invalid_digits(self):
        """非日期的 14 位数字: 99999999999999 → None (strptime 失败)"""
        assert parse_filename_date("00_99999999999999.mp4") is None

    def test_multiple_segments(self):
        """多个数字段: 00_123_20260731094443_20260731095216.mp4 → 取第一个 14 位"""
        d = parse_filename_date("00_123_20260731094443_20260731095216.mp4")
        assert d == datetime(2026, 7, 31, 9, 44, 43)

    def test_other_extension(self):
        """其他扩展名: 00_20260101120000_20260101121000.mkv"""
        d = parse_filename_date("00_20260101120000_20260101121000.mkv")
        assert d == datetime(2026, 1, 1, 12, 0, 0)


class TestCleanupOutput:
    def _make_dir_with_files(self, files):
        """创建临时目录并写入指定文件名(空内容)。返回目录路径。"""
        tmpdir = tempfile.mkdtemp()
        for name in files:
            with open(os.path.join(tmpdir, name), 'w') as f:
                f.write('x')
        return tmpdir

    def test_deletes_expired_only(self):
        """retention_days=365: 只删超过 365 天的文件"""
        old_date = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d%H%M%S")
        new_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d%H%M%S")
        files = [f"00_{old_date}_{old_date}.mp4", f"00_{new_date}_{new_date}.mp4"]
        tmpdir = self._make_dir_with_files(files)

        with patch('server.cleanup.config.OUTPUT_DIR', tmpdir):
            result = cleanup_output(365)

        assert result["scanned"] == 2
        assert result["deleted"] == 1
        assert result["skipped"] == 0
        remaining = os.listdir(tmpdir)
        assert len(remaining) == 1
        assert f"00_{new_date}_{new_date}.mp4" in remaining[0]
        shutil.rmtree(tmpdir)

    def test_retention_zero_no_delete(self):
        """retention_days=0: 不删任何文件"""
        old_date = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d%H%M%S")
        files = [f"00_{old_date}_{old_date}.mp4"]
        tmpdir = self._make_dir_with_files(files)

        with patch('server.cleanup.config.OUTPUT_DIR', tmpdir):
            result = cleanup_output(0)

        assert result["deleted"] == 0
        assert len(os.listdir(tmpdir)) == 1
        shutil.rmtree(tmpdir)

    def test_skips_unparseable(self):
        """不符合格式的文件不被删除,计入 skipped"""
        old_date = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d%H%M%S")
        files = [f"00_{old_date}_{old_date}.mp4", "readme.txt", "abc.mp4"]
        tmpdir = self._make_dir_with_files(files)

        with patch('server.cleanup.config.OUTPUT_DIR', tmpdir):
            result = cleanup_output(365)

        assert result["scanned"] == 3
        assert result["deleted"] == 1
        assert result["skipped"] == 2
        remaining = os.listdir(tmpdir)
        assert "readme.txt" in remaining
        assert "abc.mp4" in remaining
        shutil.rmtree(tmpdir)

    def test_nonexistent_dir(self):
        """output_dir 不存在: 返回空统计,无报错"""
        with patch('server.cleanup.config.OUTPUT_DIR', '/nonexistent/path/xyz'):
            result = cleanup_output(365)
        assert result["scanned"] == 0
        assert result["deleted"] == 0
        assert result["errors"] == []
