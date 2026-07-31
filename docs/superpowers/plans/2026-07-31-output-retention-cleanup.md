# Output 目录自动清理(保留策略)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 output 压缩目录按文件名时间戳自动清理功能,默认保留 365 天,每天由 scheduler 后台循环触发一次,input 原片不受影响。

**Architecture:** 新建 `server/cleanup.py` 模块实现清理逻辑(正则提取文件名 14 位时间戳判断过期);在 `server/scheduler.py` 的 `_scheduler_tick` 加每日清理检查;在 `server/handler.py` 加 `GET/POST /api/cleanup-settings` 端点;在 `static/src/pages/ConfigPage.tsx` 加保留天数配置 UI。

**Tech Stack:** Python 3 (标准库 os/re/datetime/json)、SQLite settings 表、React + Ant Design (InputNumber/Button/Card)。

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `server/cleanup.py` | 新建 | 清理逻辑:扫描 output 目录、解析文件名时间戳、删除过期文件、返回统计 |
| `server/scheduler.py` | 修改 | 在 `_scheduler_tick` 开头加每日清理检查 |
| `server/handler.py` | 修改 | 加 `GET/POST /api/cleanup-settings` 端点 |
| `static/src/pages/ConfigPage.tsx` | 修改 | 加 `CleanupForm` 组件,渲染保留天数输入框 |
| `tests/test_cleanup.py` | 新建 | cleanup 模块的单元测试 |

---

### Task 1: 新建 cleanup.py 清理逻辑模块

**Files:**
- Create: `server/cleanup.py`

- [ ] **Step 1: 编写 cleanup.py**

创建 `server/cleanup.py`,包含文件名时间戳解析和清理逻辑:

```python
# -*- coding: utf-8 -*-
"""
output 目录自动清理:按文件名时间戳删除过期文件。
文件名格式:{通道}_{YYYYMMDDHHMMSS}_{YYYYMMDDHHMMSS}.ext
解析策略:用正则在文件名中匹配第一个 14 位数字串作为开始时间。
"""
import os
import re
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
        log(f"cleanup: 无法读取 output 目录: {e}", level=__import__('logging').WARNING)
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
                log(f"cleanup: 删除失败 {name}: {e}", level=__import__('logging').WARNING)

    log(f"cleanup: 完成,扫描 {result['scanned']},删除 {result['deleted']},"
        f"跳过 {result['skipped']},释放 {result['freed_bytes']} 字节")
    return result
```

- [ ] **Step 2: 验证模块可导入**

Run: `cd /workspace && python3 -c "from server.cleanup import cleanup_output, parse_filename_date; print('import OK')"`
Expected: 输出 `import OK`

- [ ] **Step 3: Commit**

```bash
cd /workspace && git add server/cleanup.py && git commit -m "feat: 新增 cleanup.py output 目录自动清理模块"
```

---

### Task 2: 编写 cleanup 单元测试

**Files:**
- Create: `tests/test_cleanup.py`

- [ ] **Step 1: 编写测试**

创建 `tests/test_cleanup.py`,测试文件名解析和清理逻辑:

```python
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
```

- [ ] **Step 2: 运行测试,验证全部通过**

Run: `cd /workspace && python3 -m pytest tests/test_cleanup.py -v`
Expected: 10 个测试全部 PASS

- [ ] **Step 3: Commit**

```bash
cd /workspace && git add tests/test_cleanup.py && git commit -m "test: 添加 cleanup 模块单元测试"
```

---

### Task 3: 在 scheduler.py 加每日清理检查

**Files:**
- Modify: `server/scheduler.py:125-154` (`_scheduler_tick` 函数)

- [ ] **Step 1: 修改 _scheduler_tick,在压缩任务触发之前加清理检查**

在 `server/scheduler.py` 的 `_scheduler_tick` 函数开头(第 126 行 `from .worker import start_run` 之后)插入清理检查逻辑:

将 `_scheduler_tick` 函数从:

```python
def _scheduler_tick():
    # 延迟导入,避免 scheduler <-> worker 循环(worker 不依赖 scheduler,但保持对称)
    from .worker import start_run
    now = datetime.now().replace(second=0, microsecond=0)
    with db() as conn, _db_lock:
        schedules = [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE enabled=1"
        )]
```

改为:

```python
def _scheduler_tick():
    # 延迟导入,避免 scheduler <-> worker 循环(worker 不依赖 scheduler,但保持对称)
    from .worker import start_run
    now = datetime.now().replace(second=0, microsecond=0)
    _check_daily_cleanup()
    with db() as conn, _db_lock:
        schedules = [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE enabled=1"
        )]
```

- [ ] **Step 2: 在 _scheduler_tick 之前添加 _check_daily_cleanup 函数**

在 `server/scheduler.py` 的 `_scheduler_tick` 函数定义之前(第 124 行 `raise ValueError` 之后)插入:

```python
def _check_daily_cleanup():
    """每日清理检查:跨天且 retention_days>0 时触发 output 目录清理。"""
    from .db import _get_setting, _set_setting
    from .cleanup import cleanup_output
    today = datetime.now().strftime("%Y-%m-%d")
    last_run = _get_setting("cleanup.last_run_date", "")
    if last_run == today:
        return
    retention_days = 365
    try:
        retention_days = int(_get_setting("cleanup.output_retention_days", "365"))
    except (ValueError, TypeError):
        retention_days = 365
    if retention_days <= 0:
        _set_setting("cleanup.last_run_date", today)
        return
    try:
        cleanup_output(retention_days)
    except Exception as e:
        log(f"cleanup error: {e}", level=logging.WARNING)
    _set_setting("cleanup.last_run_date", today)

```

- [ ] **Step 3: 验证语法**

Run: `cd /workspace && python3 -c "import server.scheduler; print('syntax OK')"`
Expected: 输出 `syntax OK`

- [ ] **Step 4: Commit**

```bash
cd /workspace && git add server/scheduler.py && git commit -m "feat: scheduler 每日自动清理 output 过期文件"
```

---

### Task 4: 在 handler.py 加 cleanup-settings API 端点

**Files:**
- Modify: `server/handler.py:843-851` (GET 区, `/api/enc-settings` 之后)
- Modify: `server/handler.py:1072-1100` (POST 区, `/api/enc-settings` 之后)

- [ ] **Step 1: 在 GET 区加 /api/cleanup-settings**

在 `server/handler.py` 第 851 行(`/api/enc-settings` 的 GET return 之后、`/api/settings` GET 之前)插入:

```python
        if path == "/api/cleanup-settings":
            from .db import _get_setting
            raw = _get_setting("cleanup.output_retention_days", "365")
            try:
                retention_days = int(raw)
            except (ValueError, TypeError):
                retention_days = 365
            return json_response(self, 200, {
                "output_retention_days": retention_days,
            })

```

- [ ] **Step 2: 在 POST 区加 /api/cleanup-settings**

在 `server/handler.py` 第 1100 行(`/api/enc-settings` 的 POST return 之后、`/api/service/restart` 之前)插入:

```python
        if path == "/api/cleanup-settings":
            days = int(data.get("output_retention_days", 365))
            days = max(0, days)
            _set_setting("cleanup.output_retention_days", str(days))
            return json_response(self, 200, {
                "ok": True,
                "output_retention_days": days,
                "note": f"保留 {days} 天" if days > 0 else "已关闭自动清理",
            })

```

- [ ] **Step 3: 验证语法**

Run: `cd /workspace && python3 -c "import server.handler; print('syntax OK')"`
Expected: 输出 `syntax OK`

- [ ] **Step 4: Commit**

```bash
cd /workspace && git add server/handler.py && git commit -m "feat: 添加 /api/cleanup-settings 端点"
```

---

### Task 5: 在 ConfigPage.tsx 加保留天数配置 UI

**Files:**
- Modify: `static/src/pages/ConfigPage.tsx:196` (EncForm 之后) 和 `:353-363` (ConfigPage 组件)

- [ ] **Step 1: 在 ConfigPage.tsx 中 EncForm 组件之后添加 CleanupForm 组件**

在 `server/static/src/pages/ConfigPage.tsx` 第 196 行(EncForm 函数的闭合 `}` 之后)插入新组件:

```tsx
// ---- 自动清理配置 ----
function CleanupForm() {
  const { message } = App.useApp()
  const [days, setDays] = useState(365)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api<any>('/api/cleanup-settings', { silent: true })
      setDays(r.output_retention_days ?? 365)
    } catch {
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function save() {
    setSaving(true)
    try {
      const r = await api<any>('/api/cleanup-settings', {
        method: 'POST',
        body: { output_retention_days: days },
      })
      if (r.ok) message.success('清理设置已保存 · ' + r.note)
    } catch {
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card title="自动清理" extra={<Button size="small" icon={<ReloadOutlined />} onClick={load} />}>
      <Spin spinning={loading}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          仅清理 output 压缩目录中超过保留期的文件,input 原片不受影响。按文件名时间戳判断。
          设为 0 关闭自动清理,每天由 scheduler 自动执行一次。
        </Text>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 16 }}>
          <span>output 保留天数</span>
          <InputNumber
            min={0}
            max={3650}
            value={days}
            onChange={(v) => setDays(v ?? 365)}
            style={{ width: 120 }}
            addonAfter="天"
          />
          <Button type="primary" onClick={save} loading={saving}>保存</Button>
        </div>
        <div style={{ fontSize: 12, color: '#64748b', marginTop: 8 }}>
          默认 365 天(1年)。0 = 不清理。
        </div>
      </Spin>
    </Card>
  )
}
```

- [ ] **Step 2: 在 ConfigPage 组件中渲染 CleanupForm**

将 `ConfigPage` 组件(第 353-364 行)从:

```tsx
export default function ConfigPage() {
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title="服务操作" size="small">
        <RestartButton />
      </Card>
      <SettingsForm />
      <EncForm />
      <ConfigForm />
    </Space>
  )
}
```

改为:

```tsx
export default function ConfigPage() {
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title="服务操作" size="small">
        <RestartButton />
      </Card>
      <SettingsForm />
      <EncForm />
      <CleanupForm />
      <ConfigForm />
    </Space>
  )
}
```

- [ ] **Step 3: 确认 InputNumber 已在文件顶部导入**

检查 `static/src/pages/ConfigPage.tsx` 顶部 antd 导入是否包含 `InputNumber`。若没有则添加:

```tsx
import { InputNumber } from 'antd'
```

- [ ] **Step 4: 构建验证**

Run: `cd /workspace/static && npm run build 2>&1 | tail -10`
Expected: 0 错误,vite build 成功

- [ ] **Step 5: Commit**

```bash
cd /workspace && git add static/src/pages/ConfigPage.tsx static/dist && git commit -m "feat: ConfigPage 添加 output 保留天数配置 UI"
```

---

### Task 6: 端到端验证

**Files:**
- 无新文件

- [ ] **Step 1: Python 全文件语法检查**

Run: `cd /workspace && python3 -c "
import py_compile, glob
for f in glob.glob('server/*.py'):
    py_compile.compile(f, doraise=True)
print('Python syntax OK')
"`
Expected: `Python syntax OK`

- [ ] **Step 2: 运行 cleanup 单元测试**

Run: `cd /workspace && python3 -m pytest tests/test_cleanup.py -v`
Expected: 10 个测试全部 PASS

- [ ] **Step 3: 前端构建**

Run: `cd /workspace/static && npm run build 2>&1 | tail -10`
Expected: 0 错误

- [ ] **Step 4: 启动服务并验证 API**

Run: `cd /workspace && python3 -c "
import requests
r = requests.get('http://127.0.0.1:8765/api/cleanup-settings', timeout=5)
print('GET status:', r.status_code, 'body:', r.json())
r2 = requests.post('http://127.0.0.1:8765/api/cleanup-settings', json={'output_retention_days': 365}, timeout=5)
print('POST status:', r2.status_code, 'body:', r2.json())
"`
Expected: GET 返回 `{"output_retention_days": 365}`,POST 返回 `{"ok": true, "output_retention_days": 365, "note": "保留 365 天"}`

- [ ] **Step 5: Commit(若有构建产物变更)**

```bash
cd /workspace && git add -A && git status
# 若有变更:
git commit -m "chore: 端到端验证通过"
```
