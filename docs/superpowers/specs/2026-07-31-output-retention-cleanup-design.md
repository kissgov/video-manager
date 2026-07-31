# Output 目录自动清理(保留策略)

## 背景

video-manager 目前没有任何视频保留/自动清理功能。现有的文件删除操作都只跟单次压缩任务绑定(压缩成功删输入、失败删残缺产物、重压删旧 output),没有「按视频存在时长自动清理」的机制。用户需要监控视频(output 压缩产物)保存 1 年,超过的自动删除。

## 功能概述

在 video-manager 中新增「output 压缩目录按文件名时间戳自动清理、默认保留 365 天」功能,集成到现有 `server/scheduler.py` 后台循环,每天执行一次。input 原片不受影响。

## 需求决策

| 决策项 | 选择 |
|---|---|
| 清理范围 | 仅 output(压缩后),input 原片永久保留 |
| 时间依据 | 文件名时间戳(非 mtime) |
| 清理频率 | 每天一次,集成到 scheduler 后台循环 |
| 保留天数 | 可配置,默认 365 天,0 = 关闭 |
| 实现方案 | 方案 A:集成 scheduler.py |

## 文件名格式

output 目录中的文件名格式为:

```
{通道}_{开始时间戳}_{结束时间戳}.ext
```

示例:`00_20260731094443_20260731095216.mp4`

- 通道:通道编号(如 `00`),长度不固定
- 开始时间戳:14 位数字 `YYYYMMDDHHMMSS`
- 结束时间戳:14 位数字 `YYYYMMDDHHMMSS`

解析策略:用正则 `(\d{14})` 在文件名(去掉扩展名后)中匹配第一个 14 位数字串作为开始时间。该策略兼容各种通道号前缀(如 `00`、`cam1` 等)。

## 架构

### 1. 配置层

**新增 settings key:**

- `cleanup.output_retention_days`:保留天数,默认 `365`,`0` 表示关闭清理
- `cleanup.last_run_date`:上次清理日期(ISO 格式 `YYYY-MM-DD`),用于跨天判断

存储在现有 `settings` 表(`server/db.py`),通过 `get_setting` / `set_setting` 读写。不加入 `config.py` 的 `CONFIG_KEYS` 白名单(那是 compress_video.sh 专用配置)。

**前端 ConfigPage.tsx:**

新增「自动清理」配置区:
- 数字输入框:「output 保留天数(天)」
- 说明文案:「设为 0 关闭自动清理;仅清理 output 压缩目录中超过保留期的文件,input 原片不受影响」
- 保存按钮:写入 `cleanup.output_retention_days` 到 settings

### 2. 清理逻辑(新建 `server/cleanup.py`)

```python
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
```

处理流程:
1. 读取 `output_dir` 配置
2. 遍历 output_dir 下所有文件(不递归子目录)
3. 对每个文件,用正则 `(\d{14})` 从文件名提取开始时间戳
4. 解析失败 → 跳过,计入 `skipped`
5. 开始时间距今超过 `retention_days` 天 → 删除文件,计入 `deleted`,累加 `freed_bytes`
6. 删除失败 → 记录错误信息到 `errors`,继续处理下一个
7. 返回统计结果

### 3. 调度层(`server/scheduler.py`)

在 `_scheduler_tick()` 中新增清理检查逻辑:

1. 读取 `cleanup.last_run_date`
2. 若与今天日期不同(跨天)且当前不在压缩任务运行中:
3. 读取 `cleanup.output_retention_days`
4. 若 > 0:调用 `cleanup_output(retention_days)`
5. 记录清理日志(扫描数、删除数、释放空间)
6. 更新 `cleanup.last_run_date` 为今天

清理检查在压缩任务触发之前执行,避免与压缩 IO 竞争。

### 4. 日志

清理操作通过现有日志系统(`server/state.py` 的 `add_log`)记录:
- 清理开始:「开始清理 output 目录,保留 {retention_days} 天」
- 清理完成:「清理完成:扫描 {scanned} 个文件,删除 {deleted} 个,跳过 {skipped} 个,释放 {freed_h}」
- 单个删除失败:警告级别日志

## 错误处理

| 场景 | 处理 |
|---|---|
| output_dir 不存在 | 跳过清理,记录警告日志 |
| 文件名解析失败(无 14 位数字) | 跳过该文件,计入 skipped |
| 删除失败(权限/IO 错误) | 记录错误,继续处理其他文件 |
| retention_days = 0 | 不执行清理 |
| retention_days 为负数 | 视同 0,不执行清理 |

## 数据流

```
scheduler tick (每30秒)
  → 检查 cleanup.last_run_date 是否跨天
  → 是 → 读取 cleanup.output_retention_days
  → >0 → 调用 cleanup_output(retention_days)
  → 记录日志
  → 更新 cleanup.last_run_date
```

## 测试计划

### 单元测试

1. **文件名时间戳解析**:
   - 标准格式 `00_20260731094443_20260731095216.mp4` → 解析为 2026-07-31 09:44:43
   - 无下划线 `20260731094443.mp4` → 解析为 2026-07-31 09:44:43
   - 短文件名 `abc.mp4` → 解析失败,跳过
   - 无效字符 `00_20ab0731094443.mp4` → 不匹配 14 位连续数字,跳过
   - 多个数字段 `00_123_20260731094443_20260731095216.mp4` → 匹配第一个 14 位数字

2. **清理逻辑**:
   - 创建模拟 output 目录,放不同日期的文件
   - retention_days=365:只删超过 365 天的文件,保留近期文件
   - retention_days=0:不删任何文件
   - 不符合格式的文件不被删除

## 影响范围

### 新增文件
- `server/cleanup.py`:清理逻辑模块

### 修改文件
- `server/scheduler.py`:在 `_scheduler_tick` 加每日清理检查
- `static/src/pages/ConfigPage.tsx`:新增保留天数配置 UI
- `server/handler.py`:新增 `GET/POST /api/cleanup-settings` 端点(与现有 `/api/enc-settings` 模式一致,底层用 `_get_setting`/`_set_setting` 读写 `cleanup.output_retention_days`)

### 不受影响
- input 目录及其文件
- 压缩任务工作流
- 集群文件管理
