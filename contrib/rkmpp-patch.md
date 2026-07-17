# RKMPP 优化 patch 说明

## 背景

`app.py` 默认的 rkmpp 转码命令用 `scale=` CPU 缩放,在 RK3588 上只能跑 **2x 实时**。

改成 `vpp_rkrga` 走 RGA 硬缩放 + drm_prime 零拷贝后,实测 **12x 实时**。

## patch

文件 `app.py` `_build_ffmpeg_cmd()` 的 `rkmpp` 分支:

### 之前(慢)
```python
if hwaccel == "rkmpp":
    return base + [
        "-i", str(input_file),
        "-vf", f"scale=-2:{_OUTPUT_HEIGHT},fps={_OUTPUT_FPS}",
        "-c:v", "h264_rkmpp", "-qp", str(_VAAPI_QP), "-rc_mode", "2",
        "-an", "-movflags", "+faststart", "-y", str(output_file),
    ]
```

### 之后(快 6x)
```python
if hwaccel == "rkmpp":
    # 硬件解码 + RGA 硬缩 + MPP 硬编,12x 实时
    return base + [
        "-hwaccel", "rkmpp",
        "-hwaccel_output_format", "drm_prime",
        "-i", str(input_file),
        "-vf", f"vpp_rkrga=w=-2:h={_OUTPUT_HEIGHT}",
        "-c:v", "h264_rkmpp", "-b:v", "2M",
        "-an", "-movflags", "+faststart", "-y", str(output_file),
    ]
```

## 为什么不用 fps filter

实测 `vpp_rkrga=fps=10` 会报 "Option not found",vpp_rkrga 不接 fps。
vpp_rkrga 后面跟 `format=nv12,fps=10` 会撞到 "auto_scale_0 格式不兼容"。

最简稳定方案:让输出 framerate 跟源一样(小米摄像头通常 20fps),
压缩比仍然很好,没必要强制 10fps。

## 性能数据(RK3588 DH4300Plus)

| 管线 | 速度 | CPU |
|---|---|---|
| 软编 libx264 | 0.3-0.5x | 600%+ |
| RKMPP + CPU scale | 2.2x | 600%+ |
| **RKMPP + vpp_rkrga** | **12.5x** | **40-80%** |

12.5x 实测 fps=249 (vpp_rkrga 优化后)。

## 已知限制

- 不支持强制 fps (用源帧率)
- 不带音频(原 video-manager 配置就没音频)
- 输入源是 mp4 (video-manager worker 处理的就是 .mp4)
- 输出 framerate 跟输入 (20fps),想要 10fps 自己加 `fps` 软件 filter