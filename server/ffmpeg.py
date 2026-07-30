# -*- coding: utf-8 -*-
"""
ffmpeg 相关:二进制探测、硬件加速检测、命令构造、单文件执行、
编码参数(QP/码率上限/强制重压)读取。
"""
import os
import logging
import subprocess
from pathlib import Path

from .config import (
    log,
    _OUTPUT_HEIGHT, _OUTPUT_FPS, _OUTPUT_WIDTH,
    _SOFT_CODEC, _SOFT_PRESET, _SOFT_CRF, _VAAPI_QP, _NICE_LEVEL,
)
from .db import _get_setting_int, _get_setting_bool
from .state import _state_lock, _S


# jellyfin-ffmpeg 自带 .so(librockchip_mpp 等)在 /usr/lib/jellyfin-ffmpeg/,
# 默认 ldconfig 搜不到,每次起 ffmpeg 子进程时注入 LD_LIBRARY_PATH,否则:
# - -hide_banner 阶段初始化失败直接退 → 探测不到编码器 → 误判软编
# - worker 里跑压片也会同样崩
def _ffmpeg_env() -> dict:
    extra = "/usr/lib/jellyfin-ffmpeg"
    env = os.environ.copy()
    cur = env.get("LD_LIBRARY_PATH", "")
    if cur:
        if extra not in cur.split(":"):
            env["LD_LIBRARY_PATH"] = f"{extra}:{cur}"
    else:
        env["LD_LIBRARY_PATH"] = extra
    return env


def _get_rkmpp_qp() -> int:
    """从 settings 读 RKMPP QP,默认 28。范围 18-36。"""
    v = _get_setting_int("rkmpp_qp", 28)
    return max(18, min(36, v))

def _get_rkmpp_bitrate_cap() -> int:
    """从 settings 读 RKMPP 上限码率(kbps),默认 4000。0=不限。"""
    v = _get_setting_int("rkmpp_bitrate_cap", 4000)
    return max(0, v)

def _get_force_recompress() -> bool:
    """UI 开关：on 时重新压缩所有 input(覆盖旧 output)。"""
    return _get_setting_bool("force_recompress", False)

def _get_keep_audio() -> bool:
    """是否保留音频。默认 True：监控录像大多带声，浏览器播放 AAC 兼容无门槛。
    关了可省 8-15% 空间，但回放无声。
    """
    return _get_setting_bool("keep_audio", True)

def _get_audio_bitrate_k() -> int:
    """音频码率(kbps),默认 96(人声/监控足够),范围 32-192。"""
    v = _get_setting_int("audio_bitrate_k", 96)
    return max(32, min(192, v))

def _resolve_ffmpeg_bin() -> str:
    for path in [
        "/usr/local/bin/ffmpeg-rkmpp",
        "/usr/local/rkmpp/ffmpeg",
        "/ugreen/@appstore/com.ugreen.transcode/lib/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]:
        try:
            p = Path(path)
            if p.is_file() and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return "/usr/bin/ffmpeg"

def _probe_encoders(ffmpeg_bin: str) -> str:
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
            env=_ffmpeg_env(),
        )
        return r.stdout
    except Exception as e:
        log(f"枚举编码器失败: {e}", level=logging.ERROR)
        return ""

def _probe_hwaccel_ok(ffmpeg_bin: str, mode: str) -> bool:
    """运行一个微编码测试,退出码为 0 表示硬件可用。"""
    if mode == "rkmpp":
        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=10",
               "-c:v", "h264_rkmpp", "-qp", "28", "-rc_mode", "2",
               "-frames:v", "1", "-f", "null", "-"]
    elif mode == "vaapi":
        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-init_hw_device", "vaapi=foo:/dev/dri/renderD128",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=10",
               "-frames:v", "1", "-f", "null", "-"]
    else:
        return False
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30, env=_ffmpeg_env())
        return r.returncode == 0
    except Exception:
        return False

def _detect_hwaccel() -> str:
    """镜像 compress_video.sh 的 detect_hwaccel 逻辑。"""
    ffmpeg_bin = _resolve_ffmpeg_bin()
    encs = _probe_encoders(ffmpeg_bin)

    # 0. RKMPP
    if Path("/dev/mpp_service").exists():
        if "hevc_rkmpp" in encs:
            if _probe_hwaccel_ok(ffmpeg_bin, "rkmpp"):
                return "rkmpp"
            log("MPP 设备在但 h264_rkmpp 探测失败", level=logging.WARNING)
        else:
            log("FFMPEG_BIN 缺少 hevc_rkmpp 编码器,可能用错二进制", level=logging.WARNING)

    # 1. 没 GPU 设备就软
    if not Path("/dev/dri/renderD128").exists() and not Path("/dev/dri/card0").exists():
        return "soft"

    # 2. VAAPI
    if "hevc_vaapi" in encs:
        if _probe_hwaccel_ok(ffmpeg_bin, "vaapi"):
            return "vaapi"
        log("VAAPI 设备存在但初始化失败", level=logging.WARNING)

    # 3. V4L2
    if "hevc_v4l2m2m" in encs:
        try:
            r = subprocess.run("ls /dev/video* 2>/dev/null | head -1",
                               shell=True, capture_output=True, text=True, timeout=3)
            dev = r.stdout.strip()
            if dev:
                pr = subprocess.run(
                    [ffmpeg_bin, "-hide_banner", "-f", "v4l2",
                     "-list_formats", "all", "-i", dev],
                    capture_output=True, text=True, timeout=10,
                    env=_ffmpeg_env(),
                )
                if "HEVC" in (pr.stdout + pr.stderr):
                    return "v4l2m2m"
        except Exception as e:
            log(f"v4l2 探测异常: {e}", level=logging.WARNING)

    # 4. QSV
    if "hevc_qsv" in encs:
        return "qsv"

    return "soft"

def _build_ffmpeg_cmd(input_file: Path, output_file: Path, hwaccel: str, ffmpeg_bin: str) -> list:
    base = ["nice", "-n", str(_NICE_LEVEL),
            ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-err_detect", "ignore_err", "-fflags", "+discardcorrupt"]
    # 音频处理：默认保留，转 AAC(浏览器全兼容)，码率可配置。不想占空间可关 keep_audio 走 -an
    if _get_keep_audio():
        audio_args = ["-c:a", "aac", "-b:a", f"{_get_audio_bitrate_k()}k", "-ac", "1"]  # 监控用单声道即可，再省一半音频空间
    else:
        audio_args = ["-an"]
    # -movflags +faststart 将 moov atom 移到文件头,浏览器边下边播
    common_tail = audio_args + ["-movflags", "+faststart", "-y", str(output_file)]
    if hwaccel == "rkmpp":
        qp = _get_rkmpp_qp()
        cap = _get_rkmpp_bitrate_cap()
        cmd = [
            "-hwaccel", "rkmpp",
            "-hwaccel_output_format", "drm_prime",
            "-i", str(input_file),
            "-vf", f"vpp_rkrga=w=-2:h={_OUTPUT_HEIGHT}",
            "-c:v", "h264_rkmpp",
            "-qp", str(qp), "-rc_mode", "2",  # CQP 变码率
        ]
        if cap > 0:
            cmd += ["-b:v", f"{cap}k"]
        return base + cmd + common_tail
    if hwaccel == "vaapi":
        return base + [
            "-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
            "-vaapi_device", "/dev/dri/renderD128",
            "-i", str(input_file),
            "-vf", f"format=nv12|vaapi,hwupload,scale_vaapi=-2:{_OUTPUT_HEIGHT}:format=nv12,framerate=fps={_OUTPUT_FPS}",
            "-c:v", "hevc_vaapi", "-qp", str(_VAAPI_QP),
        ] + common_tail
    if hwaccel == "v4l2m2m":
        return base + [
            "-i", str(input_file),
            "-vf", f"scale=-2:{_OUTPUT_HEIGHT},fps={_OUTPUT_FPS}",
            "-c:v", "hevc_v4l2m2m", "-num_capture_buffers", "32",
            "-b:v", "1M", "-maxrate", "1.5M", "-bufsize", "2M",
        ] + common_tail
    if hwaccel == "qsv":
        return base + [
            "-hwaccel", "qsv", "-c:v", "h264_qsv",
            "-i", str(input_file),
            "-vf", f"scale_qsv=-2:{_OUTPUT_HEIGHT},vpp_qsv=framerate={_OUTPUT_FPS}",
            "-c:v", "hevc_qsv", "-global_quality", str(_VAAPI_QP), "-preset", "medium",
        ] + common_tail
    # soft
    return base + [
        "-threads", "0",
        "-i", str(input_file),
        "-vf", f"scale=-2:{_OUTPUT_HEIGHT},fps={_OUTPUT_FPS}",
        "-c:v", _SOFT_CODEC, "-crf", str(_SOFT_CRF), "-preset", _SOFT_PRESET,
        "-tune", "fastdecode",
    ] + common_tail

def _run_ffmpeg(input_file: Path, output_file: Path, hwaccel: str) -> tuple:
    """跑一个文件,返回 (exit_code, stderr_text)。"""
    ffmpeg_bin = _resolve_ffmpeg_bin()
    cmd = _build_ffmpeg_cmd(input_file, output_file, hwaccel, ffmpeg_bin)
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=_ffmpeg_env(),
        )
        with _state_lock:
            _S.ffmpeg_proc = proc
        try:
            _, err = proc.communicate(timeout=4 * 3600)
            return proc.returncode, (err or b"").decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
            return -1, "ffmpeg 单文件超时(>4h)"
    except Exception as e:
        return -1, f"ffmpeg 启动失败: {e}"
    finally:
        with _state_lock:
            _S.ffmpeg_proc = None

def detect_hwaccel_hint():
    """复用脚本里的探测逻辑(简化版),给前端一个 hint。"""
    hints = []
    if os.path.exists("/dev/mpp_service"):
        hints.append("rkmpp 设备存在")
    if os.path.exists("/dev/dri/renderD128"):
        hints.append("VAAPI 可用")
    for cand in ["/usr/local/bin/ffmpeg-rkmpp", "/usr/local/rkmpp/ffmpeg",
                 "/ugreen/@appstore/com.ugreen.transcode/lib/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            hints.append(f"ffmpeg: {cand}")
            return hints
    hints.append("ffmpeg 未找到")
    return hints

def ffmpeg_version():
    for cand in ["/usr/local/bin/ffmpeg-rkmpp", "/usr/local/rkmpp/ffmpeg",
                 "/ugreen/@appstore/com.ugreen.transcode/lib/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            try:
                r = subprocess.run([cand, "-version"], capture_output=True, text=True, timeout=5,
                                   env=_ffmpeg_env())
                return cand, r.stdout.splitlines()[0] if r.stdout else "(empty)"
            except Exception:
                return cand, "(运行失败)"
    return None, "未找到 ffmpeg"
