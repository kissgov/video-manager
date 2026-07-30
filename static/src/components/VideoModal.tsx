// 视频播放弹窗:点击文件/集群视频行打开,支持 Range 流播放 + 下载。
import { useEffect, useRef, useState } from 'react'
import { Modal } from 'antd'

interface Props {
  open: boolean
  streamUrl: string // 已是完整 URL(含 apiUrl 前缀)
  title: string
  info?: string
  onClose: () => void
}

export default function VideoModal({ open, streamUrl, title, info, onClose }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [tip, setTip] = useState<string>('')

  // 关键:Antd Modal 默认懒渲染(destroyOnClose 默认关闭也一样),open=true 时 videoRef.current 往往还在下一帧才出现。
  // 这里用 rAF + 短暂重试循环,确保拿到真实 <video> 后再 src/load/play。
  useEffect(() => {
    let raf = 0
    let tries = 0
    const done = (reason?: string) => {
      if (raf) cancelAnimationFrame(raf)
      raf = 0
      if (reason) setTip(reason)
    }
    const apply = () => {
      const v = videoRef.current
      if (!v) {
        tries++
        if (tries > 20) { done('⏳ 播放器初始化超时,请关闭弹窗重试'); return }
        raf = requestAnimationFrame(apply)
        return
      }
      if (open && streamUrl) {
        setTip('⏳ 正在加载视频...')
        try { v.pause() } catch {}
        v.removeAttribute('src')
        try { v.load() } catch {}
        v.src = streamUrl
        v.preload = 'auto'
        v.load()
        const p = v.play()
        if (p && typeof p.catch === 'function') {
          p.then(() => setTip(''))
            .catch((err: any) => {
              if (err && (err.name === 'NotAllowedError' || /autoplay/i.test(err.message || ''))) {
                // 浏览器策略: 带声 autoplay 被拒 -> 先静音再试,仍然失败就手动点▶
                if (!v.muted) {
                  v.muted = true
                  const p2 = v.play()
                  if (p2 && typeof p2.then === 'function') {
                    p2.then(() => setTip('🔇 浏览器阻止带声自动播放,已静音;点 🔈/🔊 开声音')).catch(() => {
                      setTip('▶ 点击视频左下角的 ▶ 按钮开始播放')
                    })
                    return
                  }
                }
                setTip('▶ 点击视频左下角的 ▶ 按钮开始播放')
              } else {
                const code = v.error ? (v.error.code || 0) : 0
                const map: Record<number, string> = { 1: '加载被取消', 2: '网络错误', 3: '解码失败', 4: '格式不支持/文件损坏' }
                setTip(`❌ 无法播放: ${map[code] || '未知错误'} (code=${code})`)
              }
            })
        }
      } else {
        setTip('')
        try { v.pause() } catch {}
        v.removeAttribute('src')
        try { v.load() } catch {}
      }
    }
    raf = requestAnimationFrame(apply)
    return () => { if (raf) cancelAnimationFrame(raf) }
  }, [open, streamUrl])

  // 下载链接:把 /stream? 换成 /download?
  const downloadUrl = streamUrl ? streamUrl.replace('/stream?', '/download?') : ''

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="92vw"
      style={{ maxWidth: 1600, top: 24 }}
      bodyStyle={{ padding: 12, background: '#0b1220' }}
      title={
        <span style={{ fontSize: 14 }} title={title}>
          ▶ {title}
        </span>
      }
      maskClosable
    >
      <div style={{ background: '#000', borderRadius: 8, overflow: 'hidden', boxShadow: '0 4px 24px rgba(0,0,0,.5)' }}>
        <video
          ref={videoRef}
          controls
          playsInline
          preload="auto"
          style={{
            width: '100%',
            height: 'auto',
            minHeight: 360,
            maxHeight: '80vh',
            display: 'block',
            background: '#000',
            objectFit: 'contain',
          }}
        />
      </div>
      <div style={{
        marginTop: 10,
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 8,
      }}>
        <span style={{ fontSize: 12, color: tip.startsWith('❌') ? '#fca5a5' : tip.startsWith('▶') || tip.startsWith('🔇') ? '#fde68a' : '#94a3b8' }}>
          {info}{tip ? (info ? ' · ' : '') + tip : ''}
        </span>
        {downloadUrl && (
          <a href={downloadUrl} download target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
            ⬇ 下载
          </a>
        )}
      </div>
    </Modal>
  )
}
