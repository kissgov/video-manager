# -*- coding: utf-8 -*-
"""
脚本配置区读写 + Ofelia 定时任务管理。
read_script_config / update_script_config / read_ofelia_jobs /
update_ofelia_jobs / restart_ofelia。
"""
import re
import logging
import subprocess
import configparser

from .config import log, SCRIPT_PATH, OFELIA_INI, OFELIA_BAK, CONFIG_KEYS
from .state import _config_lock, _ofelia_lock


def read_script_config():
    """解析配置区变量。"""
    with _config_lock:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        result = {}
        for line in text.splitlines():
            line_strip = line.strip()
            for k in CONFIG_KEYS:
                if line_strip.startswith(k + "="):
                    raw = line_strip.split("=", 1)[1]
                    # 去掉行内注释
                    raw = raw.split("#", 1)[0].strip()
                    # 去引号
                    if (raw.startswith('"') and raw.endswith('"')) or \
                       (raw.startswith("'") and raw.endswith("'")):
                        raw = raw[1:-1]
                    result[k] = raw
                    break
        return result, text

def update_script_config(updates: dict):
    """替换脚本里对应的变量赋值,保留其他内容。"""
    with _config_lock:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        lines = text.splitlines()
        out = []
        for line in lines:
            replaced = False
            for k, v in updates.items():
                if k in CONFIG_KEYS and re.match(rf"^\s*{k}\s*=", line):
                    if isinstance(v, str) and re.search(r"\s", v):
                        new_line = re.sub(rf"^(\s*){k}\s*=.*$", rf'\1{k}="{v}"', line)
                    else:
                        new_line = re.sub(rf"^(\s*){k}\s*=.*$", rf"\1{k}={v}", line)
                    out.append(new_line)
                    replaced = True
                    break
            if not replaced:
                out.append(line)
        new_text = "\n".join(out) + ("\n" if text.endswith("\n") else "")
        # 备份
        bak = SCRIPT_PATH.with_suffix(".sh.bak.manager")
        bak.write_text(text, encoding="utf-8")
        SCRIPT_PATH.write_text(new_text, encoding="utf-8")
        log(f"脚本配置已更新,备份到 {bak}")
        return True

# ============== Ofelia 配置管理 ==============
def read_ofelia_jobs():
    """用 configparser 解析 ofelia.ini 中的 [job-exec "xxx"] 段。"""
    if not OFELIA_INI.exists():
        return []
    cfg = configparser.ConfigParser()
    # 保留大小写
    cfg.optionxform = str
    try:
        cfg.read(OFELIA_INI, encoding="utf-8")
    except Exception as e:
        log(f"读 ofelia.ini 失败: {e}", level=logging.ERROR)
        return []
    jobs = []
    for section in cfg.sections():
        if section.startswith("job-exec") or section.startswith("job-run") or section.startswith("job-local"):
            sec = cfg[section]
            # 从段标题解析名字,例:job-exec "compress-surveillance" -> compress-surveillance
            m = re.match(r'^job-\w+\s+["\']([^"\']+)["\']', section)
            derived_name = m.group(1) if m else section
            jobs.append({
                "section":   section,
                "name":      sec.get("name") or derived_name,
                "schedule":  sec.get("schedule", ""),
                "container": sec.get("container", ""),
                "command":   sec.get("command", ""),
            })
    return jobs

def update_ofelia_jobs(jobs: list):
    """重写 ofelia.ini 中的所有 job-exec 段,保留其他内容(注释等)。"""
    with _ofelia_lock:
        # 备份
        if OFELIA_INI.exists():
            OFELIA_BAK.write_text(OFELIA_INI.read_text(encoding="utf-8"), encoding="utf-8")
        # 重建:把文件按段拆分,只替换 [job-exec ...] 段
        text = OFELIA_INI.read_text(encoding="utf-8") if OFELIA_INI.exists() else ""
        # 简单处理:行级扫描,把 job-exec 段替换成新内容
        lines = text.splitlines()
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("[job-") and stripped.endswith("]"):
                # 跳到段尾
                i += 1
                while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith("["):
                    i += 1
                continue
            out.append(line)
            i += 1
        # 删除所有原 job- 段后的空行(连续空行合并)
        # 简化:直接重写文件,只保留头部注释
        header_lines = []
        for ln in out:
            if ln.strip().startswith("["):
                break
            header_lines.append(ln)
        # 去除尾部空行
        while header_lines and not header_lines[-1].strip():
            header_lines.pop()

        new_text = "\n".join(header_lines) + "\n\n"
        for job in jobs:
            sec_name = job.get("section") or f'job-exec "{job.get("name","job")}"'
            new_text += f"[{sec_name}]\n"
            if job.get("name"):
                new_text += f"name      = {job['name']}\n"
            new_text += f"schedule  = {job.get('schedule','')}\n"
            new_text += f"container = {job.get('container','')}\n"
            new_text += f"command   = {job.get('command','')}\n\n"

        OFELIA_INI.write_text(new_text, encoding="utf-8")
        log(f"ofelia.ini 已重写,任务数: {len(jobs)}")
        return True

def restart_ofelia():
    """通过 sudo 免密重启 ofelia 容器。需要 sudoers 配置 NOPASSWD。"""
    cmd = ["sudo", "-n", "docker", "restart", "ofelia-scheduler"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, f"ofelia 已重启: {r.stdout.strip() or 'OK'}"
        msg = (r.stderr or r.stdout or "").strip() or f"退出码 {r.returncode}"
        if r.returncode != 0 and "password" in msg.lower():
            msg += "\n提示: 需 sudoers 配置免密: kxrdyf ALL=(root) NOPASSWD: /usr/bin/docker restart ofelia-scheduler"
        return False, f"重启失败: {msg}"
    except FileNotFoundError:
        return False, "未找到 sudo 或 docker，请手动执行: docker restart ofelia-scheduler"
    except Exception as e:
        return False, f"重启失败: {e}"
