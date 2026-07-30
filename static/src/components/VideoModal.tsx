// 视频播放弹窗：点击文件/集群视频行打开，支持 Range 流播放 + 下载。
import { useEffect, useRef } from 'react'
import { Modal } from 'antd'

interface Props {
  open: boolean
  streamUrl: string // 已是完整 URL（含 apiUrl 前缀）
  title: string
  info?: string
  onClose: () => void
}

export default function VideoModal({ open, streamUrl, title, info, onClose }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    if (open && streamUrl) {
      v.src = streamUrl
      v.load()
      const p = v.play()
      if (p) p.catch(() => {})
    } else {
      try {
        v.pause()
      } catch {}
      v.removeAttribute('src')
      v.load()
    }
  }, [open, streamUrl])

  // 下载链接：把 /stream? 换成 /download?
  const downloadUrl = streamUrl ? streamUrl.replace('/stream?', '/download?') : ''

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={1000}
      title={
        <span style={{ fontSize: 14 }} title={title}>
          ▶ {title}
        </span>
      }
      destroyOnClose
    >
      <div style={{ background: '#000', borderRadius: 6, overflow: 'hidden' }}>
        <video
          ref={videoRef}
          controls
          autoPlay
          playsInline
          preload="metadata"
          style={{ width: '100%', maxHeight: '70vh', display: 'block', background: '#000' }}
        />
      </div>
      <div style={{ marginTop: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: 12, color: '#94a3b8' }}>{info}</span>
        {downloadUrl && (
          <a href={downloadUrl} download target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
            下载
          </a>
        )}
      </div>
    </Modal>
  )
}
