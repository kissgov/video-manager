// 回放页：监控录像回放（事件列表 + 播放器 + 缩略图进度条 + 自动播放下一段）
// 1:1 复刻原 app.js 的回放逻辑。跨节点聚合用 /api/cluster/files。
import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { Card, DatePicker, Checkbox, Button, Segmented, Space, Spin, Empty, Tag, Typography, App, Select } from 'antd'
import dayjs from 'dayjs'
import { ReloadOutlined } from '@ant-design/icons'
import { api, apiUrl } from '../api'
import { humanSize, pbParseName, pbFmtDur, type PbMeta } from '../utils'

const { Text } = Typography

interface PbFile {
  path: string
  size?: number
  size_h?: string
  mtime: string
  meta: PbMeta | null
  dir: string
  type: string
  streamUrl: string
  thumbUrl: string
  peerId: string
  peerName: string
  isSelf: boolean
  hasAudio?: boolean | null // true=有音轨, false=无音轨, null/undefined=尚未探测
}

const VOL_KEY = 'pb:vol'
const MUTED_KEY = 'pb:muted'

export default function PlaybackPage() {
  const { message } = App.useApp()
  const [pbDir, setPbDir] = useState<'output' | 'input' | 'all'>('all')
  const [date, setDate] = useState<string>('') // YYYY-MM-DD
  const [files, setFiles] = useState<PbFile[]>([])
  const [index, setIndex] = useState(-1)
  const [autoplay, setAutoplay] = useState(true)
  const [loop, setLoop] = useState(false)
  const [autoNextDay, setAutoNextDay] = useState(false)
  const [peerFilter, setPeerFilter] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [muted, setMuted] = useState<boolean>(() => {
    try { return localStorage.getItem(MUTED_KEY) === '1' } catch { return false }
  })
  const [progress, setProgress] = useState('—')
  const [counter, setCounter] = useState('0 / 0')
  const [nextHint, setNextHint] = useState('下一个: —')
  const [curTitle, setCurTitle] = useState('—')
  const [curInfo, setCurInfo] = useState('')
  const [diagnosis, setDiagnosis] = useState<{ html: string; warn?: boolean; err?: boolean }>({ html: '' })

  const playerRef = useRef<HTMLVideoElement>(null)
  const indexRef = useRef(-1)
  indexRef.current = index
  const filesRef = useRef<PbFile[]>([])
  filesRef.current = files
  const pbDirRef = useRef(pbDir)
  pbDirRef.current = pbDir
  const dateRef = useRef(date)
  dateRef.current = date
  const autoplayRef = useRef(autoplay)
  autoplayRef.current = autoplay
  const loopRef = useRef(loop)
  loopRef.current = loop
  const autoNextDayRef = useRef(autoNextDay)
  autoNextDayRef.current = autoNextDay
  const peerFilterRef = useRef(peerFilter)
  peerFilterRef.current = peerFilter
  const mutedRef = useRef(muted)
  mutedRef.current = muted

  // 同步用户的静音/音量：每次播放前写入到 video.muted/volume + 持久化
  const persistMuted = useCallback((m: boolean) => {
    setMuted(m)
    mutedRef.current = m
    try { localStorage.setItem(MUTED_KEY, m ? '1' : '0') } catch {}
    const v = playerRef.current
    if (v) v.muted = m
  }, [])
  const persistVol = useCallback((vol: number) => {
    const c = Math.max(0, Math.min(1, vol))
    try { localStorage.setItem(VOL_KEY, String(c)) } catch {}
    const v = playerRef.current
    if (v) v.volume = c
  }, [])

  const _fileKey = (f: PbFile) => `${f.peerId}|${f.dir}|${f.path}`

  // video metadata 加载完成:探测音轨数/分辨率,输出诊断 + 回写到文件列表(用于 ⚠️/🎵 标签)
  const onVideoLoadedMeta = useCallback(() => {
    const v = playerRef.current
    const idx = indexRef.current
    const f = filesRef.current[idx]
    if (!v || !f) return
    const w = v.videoWidth || 0
    const h = v.videoHeight || 0
    const dur = isFinite(v.duration) ? v.duration : 0
    // 1) 优先用标准 audioTracks;某些浏览器不暴露则 fallback null,随后会调后端接口判定。
    let audioN: number | null = null
    if (typeof (v as any).audioTracks !== 'undefined') {
      audioN = (v as any).audioTracks.length
    } else if (typeof (v as any).msAudioTracks !== 'undefined') {
      audioN = (v as any).msAudioTracks.length
    }

    const applyDiagnosis = (has: boolean | undefined) => {
      let html = ''
      let warn = false, err = false
      const head: string[] = []
      if (w && h) head.push(`📼 ${w}×${h}`)
      if (dur > 0) head.push(`⏱ ${pbFmtDur(dur)}`)
      if (has === undefined) {
        warn = true
        html = `ℹ️ 暂时无法判定是否含音轨。若原生音量按钮为灰色(不可点),就表示该文件没有音轨。`
      } else if (!has) {
        err = true
        html = `⚠️ <b>该文件不含音频轨</b>——所以浏览器原生音量控件显示为灰色、不可点击。${f.dir === 'output' ? '这多半是旧版 -an 压出的历史片,请用 POST /api/enc-settings {"recompress_no_audio":true,"keep_audio":true} 开温和补压,或 force_recompress=true 全量重压即可恢复声音。' : '输入源本身就没有音轨。'}`
      } else {
        html = `🎵 含音频轨,浏览器可出声音。若暂时无声:点右上角 🔊/🔇 解除自动播放策略的临时静音,或直接点播放器原生 🔈 按钮调音量。`
      }
      html = [...head, html].filter(Boolean).join(' · ')
      setDiagnosis({ html, warn, err })
      // 回写 hasAudio 到文件列表
      if (has !== undefined) {
        const cur = filesRef.current[idx]
        if (cur && cur.hasAudio !== has) {
          const next = filesRef.current.slice()
          next[idx] = { ...next[idx], hasAudio: has }
          setFiles(next)
          filesRef.current = next
        }
      }
    }

    if (audioN !== null) {
      applyDiagnosis(audioN > 0)
      return
    }
    // 兜底:后端用 ffprobe 直接扫 track types(不 decode,秒回),跨节点也支持
    applyDiagnosis(undefined)
    const dir = f.dir || pbDirRef.current || 'output'
    const q = new URLSearchParams({ dir, path: f.path })
    if (!f.isSelf && f.peerId) q.set('peer', f.peerId)
    const probeUrl = f.isSelf
      ? `/api/files/has-audio?${q.toString()}`
      : `/api/cluster/has-audio?${q.toString()}`
    // 如果没实现 cluster/has-audio,退而求其次:本地代理的 /api/files/has-audio 也支持 peer= 参数,所以走它也能
    const realUrl = f.isSelf ? probeUrl : `/api/files/has-audio?${q.toString()}`
    api<any>(realUrl, { silent: true }).then((r) => {
      if (!r || r.ok === false) return
      if (r.has_audio === true) applyDiagnosis(true)
      else if (r.has_audio === false) applyDiagnosis(false)
    }).catch(() => {})
  }, [])

  // 防过期事件:React 把 video 挂到 DOM 时,它可能已 readyState>=1(metadata 早已加载)
  // 这时 onLoadedMetadata 不会再触发,导致诊断横幅/无声标签永远不出现。
  useEffect(() => {
    let cancelled = false
    let to: number | null = null
    const tryFire = () => {
      if (cancelled) return
      const v = playerRef.current
      if (!v) { to = window.setTimeout(tryFire, 50); return }
      if (v.readyState >= 1) {
        onVideoLoadedMeta()
      } else {
        // 还在加载:等 loadedmetadata 或再延 200ms 最后兜底检查一次
        const once = () => { v.removeEventListener('loadedmetadata', once); onVideoLoadedMeta() }
        v.addEventListener('loadedmetadata', once)
        to = window.setTimeout(() => { v.removeEventListener('loadedmetadata', once); onVideoLoadedMeta() }, 1500)
      }
    }
    tryFire()
    return () => { cancelled = true; if (to !== null) window.clearTimeout(to) }
  }, [index, onVideoLoadedMeta])

  // ---- 首次打开:默认本机节点 + 最新有视频的日期 ----
  const defaultedRef = useRef(false)
  useEffect(() => {
    if (defaultedRef.current) return
    defaultedRef.current = true
    // 1) 取本机节点 id,设为 peerFilter 默认(避免跨节点拉取,加载更快)
    api<any>('/api/cluster/peers', { silent: true }).then((r) => {
      if (r?.self?.id) setPeerFilter(r.self.id)
    }).catch(() => {})
    // 2) 取 output 目录里有视频的日期列表,用最新一个(日期降序取 dates[-1] 或 dates[0]? dates 是升序所以取最后一个)
    api<any>('/api/files/dates?dir=output', { silent: true }).then((r) => {
      const dates: string[] = (r?.dates || []).slice().sort()
      if (dates.length > 0) setDate(dates[dates.length - 1])
    }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---- 加载事件列表(本机 + 所有 peer)----
  const loadList = useCallback(async () => {
    setLoading(true)
    try {
      const dirs = pbDir === 'all' ? ['output', 'input'] : [pbDir]
      const out: PbFile[] = []
      for (const d of dirs) {
        const r = await api<any>(`/api/cluster/files?dir=${d}&sort=path&order=asc&page_size=0`, { silent: true })
        // 本机
        if (r.self && r.self.ok) {
          const pid = r.self.id || ''
          const pname = r.self.name || '本机'
          for (const f of r.self.files?.items || []) {
            const meta = pbParseName(f.path)
            if (date) {
              const want = date.replace(/-/g, '')
              if (!meta || meta.date !== want) continue
            }
            out.push({
              path: f.path,
              size: f.size,
              size_h: f.size_h,
              mtime: f.mtime,
              meta,
              dir: d,
              type: d === 'output' ? '压缩' : '原片',
              peerId: pid,
              peerName: pname,
              isSelf: true,
              streamUrl: apiUrl(`/api/files/stream?dir=${d}&path=${encodeURIComponent(f.path)}`),
              thumbUrl: apiUrl(`/api/files/thumb?dir=${d}&path=${encodeURIComponent(f.path)}`),
            })
          }
        }
        // 远端 peers
        for (const peer of r.peers || []) {
          if (!peer.ok || !peer.files) continue
          const pid = peer.id || ''
          const pname = peer.name || pid
          for (const f of peer.files.items || []) {
            const meta = pbParseName(f.path)
            if (date) {
              const want = date.replace(/-/g, '')
              if (!meta || meta.date !== want) continue
            }
            out.push({
              path: f.path,
              size: f.size,
              size_h: f.size_h,
              mtime: f.mtime,
              meta,
              dir: d,
              type: d === 'output' ? '压缩' : '原片',
              peerId: pid,
              peerName: pname,
              isSelf: false,
              streamUrl: apiUrl(`/api/cluster/stream?peer=${encodeURIComponent(pid)}&dir=${d}&path=${encodeURIComponent(f.path)}`),
              thumbUrl: apiUrl(`/api/cluster/thumb?peer=${encodeURIComponent(pid)}&dir=${d}&path=${encodeURIComponent(f.path)}`),
            })
          }
        }
      }
      out.sort((a, b) => {
        const sa = a.meta ? a.meta.start : a.mtime
        const sb = b.meta ? b.meta.start : b.mtime
        return sa.localeCompare(sb)
      })
      setFiles(out)
      setIndex(-1)
    } catch (e: any) {
      message.error('加载失败: ' + e.message)
    } finally {
      setLoading(false)
    }
  }, [pbDir, date, message])

  useEffect(() => {
    loadList()
  }, [loadList])

  // ---- 节点筛选 ----
  // 从已加载文件里枚举出有文件的节点(本机 + peers)
  const nodeOptions = useMemo(() => {
    const opts: { label: string; value: string }[] = [{ label: '🌐 全部节点', value: '' }]
    const seen = new Set<string>()
    for (const f of files) {
      if (seen.has(f.peerId)) continue
      seen.add(f.peerId)
      opts.push({ label: (f.isSelf ? '📍 ' : '🔗 ') + f.peerName, value: f.peerId })
    }
    return opts
  }, [files])

  const visibleFiles = useMemo(
    () => (peerFilter ? files.filter((f) => f.peerId === peerFilter) : files),
    [files, peerFilter],
  )

  // ---- 统计 ----
  const totalDur = visibleFiles.reduce((s, f) => s + (f.meta ? f.meta.dur : 0), 0)
  const totalSize = visibleFiles.reduce((s, f) => s + (f.size || 0), 0)

  // 按小时分组(保留原始索引 _idx 用于播放定位)
  const groups: Record<string, PbFile[]> = {}
  files.forEach((f, i) => {
    if (peerFilter && f.peerId !== peerFilter) return
    const dt = f.meta ? f.meta.start.substring(0, 13) : f.mtime.substring(0, 13)
    if (!groups[dt]) groups[dt] = []
    const entry = { ...f }
    ;(entry as any)._idx = i
    groups[dt].push(entry)
  })

  // ---- 播放 ----
  const play = useCallback(
    (idx: number) => {
      if (idx < 0 || idx >= filesRef.current.length) return
      setIndex(idx)
      setDiagnosis({ html: '' }) // 切片:清掉上一段诊断,避免灰按钮信息残留
      const f = filesRef.current[idx]
      const v = playerRef.current
      if (v) {
        // 恢复音量/静音(用户偏好)
        try {
          const stored = localStorage.getItem(VOL_KEY)
          if (stored) {
            const n = parseFloat(stored)
            if (isFinite(n)) v.volume = Math.max(0, Math.min(1, n))
          }
        } catch {}
        v.muted = mutedRef.current
        v.src = f.streamUrl
        v.load()
        const p = v.play()
        if (p) {
          p.catch((err) => {
            // 浏览器 autoplay 策略:带声播放被拒 -> 临时静音重试,提示用户点🔊解锁
            if (err && (err.name === 'NotAllowedError' || /autoplay/i.test(err.message || ''))) {
              if (!v.muted) {
                v.muted = true
                v.play().catch(() => {})
                message.info('🔇 浏览器策略阻止带声自动播放，已静音；点击🔊开启声音')
              }
            }
          })
        }
      }
      const startT = f.meta ? f.meta.start : f.mtime
      const endT = f.meta ? f.meta.end : ''
      setCurTitle(f.path)
      const peerTag = f.isSelf ? '' : ` · 🔗 ${f.peerName}`
      setCurInfo(`${startT}${endT ? ' → ' + endT : ''} · ${f.size_h || ''}${peerTag}`)
      setCounter(`${idx + 1} / ${filesRef.current.length}`)
      const next = filesRef.current[idx + 1]
      if (next) {
        const ns = next.meta ? next.meta.start.substring(11, 19) : '—'
        const np = next.isSelf ? '' : ` · 🔗 ${next.peerName}`
        setNextHint(`下一个 (→): ${ns} · ${next.path}${np}`)
      } else {
        setNextHint(loopRef.current ? '下一个 (→): 循环到第一个' : '已是最后一段')
      }
    },
    [message]
  )

  // 判断文件是否通过当前节点筛选(无筛选时全部通过)
  const passesFilter = useCallback((f: PbFile) => {
    const pf = peerFilterRef.current
    return !pf || f.peerId === pf
  }, [])

  const next = useCallback(() => {
    const files = filesRef.current
    const idx = indexRef.current
    if (idx < 0) {
      const first = files.findIndex(passesFilter)
      if (first >= 0) play(first)
      return
    }
    for (let i = idx + 1; i < files.length; i++) {
      if (passesFilter(files[i])) { play(i); return }
    }
    if (autoNextDayRef.current && dateRef.current) {
      goNextDay()
      return
    }
    if (loopRef.current) {
      const first = files.findIndex(passesFilter)
      if (first >= 0) play(first)
      return
    }
    message.info('已是最后一段')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [play, message, passesFilter])

  const prev = useCallback(() => {
    const files = filesRef.current
    const idx = indexRef.current
    if (idx < 0) {
      for (let i = files.length - 1; i >= 0; i--) {
        if (passesFilter(files[i])) { play(i); return }
      }
      message.info('已是第一段')
      return
    }
    for (let i = idx - 1; i >= 0; i--) {
      if (passesFilter(files[i])) { play(i); return }
    }
    message.info('已是第一段')
  }, [play, passesFilter, message])

  async function goNextDay() {
    try {
      const r = await api<any>(`/api/files/dates?dir=${pbDirRef.current}`, { silent: true })
      const dates = r.dates || []
      const idx = dates.indexOf(dateRef.current)
      if (idx < 0 || idx + 1 >= dates.length) {
        if (loopRef.current) play(0)
        else message.info('已是最后一天最后一段')
        return
      }
      const nxt = dates[idx + 1]
      setDate(nxt)
      message.info(`自动跳到 ${nxt}`)
      // 等 files 更新后播放第一个
      setTimeout(() => {
        if (filesRef.current.length > 0) play(0)
      }, 1200)
    } catch (e: any) {
      message.error('跳到下一天失败: ' + e.message)
    }
  }

  function replay() {
    const v = playerRef.current
    if (v) {
      v.currentTime = 0
      v.play()
    }
  }
  function fullscreen() {
    const el = playerRef.current?.parentElement
    if (document.fullscreenElement) document.exitFullscreen()
    else if (el?.requestFullscreen) el.requestFullscreen()
  }

  // ---- 视频事件 ----
  useEffect(() => {
    const v = playerRef.current
    if (!v) return
    const onEnded = () => {
      if (autoplayRef.current) next()
    }
    const onTime = () => {
      if (v.duration && !isNaN(v.duration)) {
        const pct = ((v.currentTime / v.duration) * 100).toFixed(1)
        setProgress(`${pbFmtDur(v.currentTime)} / ${pbFmtDur(v.duration)} (${pct}%)`)
      }
    }
    const onError = () => {
      const err = v.error
      const codeMap: Record<number, string> = { 1: 'ABORTED', 2: 'NETWORK', 3: 'DECODE', 4: 'SRC_NOT_SUPPORTED' }
      message.error('播放失败: ' + (err ? codeMap[err.code] || 'unknown' : 'unknown'))
    }
    v.addEventListener('ended', onEnded)
    v.addEventListener('timeupdate', onTime)
    v.addEventListener('error', onError)
    return () => {
      v.removeEventListener('ended', onEnded)
      v.removeEventListener('timeupdate', onTime)
      v.removeEventListener('error', onError)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [next])

  // ---- 列表缩略图懒加载(进入视口或 hover 才触发 /api/*/thumb ffmpeg 抽帧,避免首屏 N 个并发阻塞视频 IO)----
  const thumbObserverRef = useRef<IntersectionObserver | null>(null)
  useEffect(() => {
    // 单例 IntersectionObserver
    if (!thumbObserverRef.current) {
      try {
        thumbObserverRef.current = new IntersectionObserver(
          (entries) => {
            for (const e of entries) {
              const el = e.target as HTMLImageElement
              if (e.isIntersecting) {
                // 进入视口:再等 80ms stable(快速滚动就不触发抽帧)
                const ds = el as any
                if (ds.__pbThumbT) clearTimeout(ds.__pbThumbT)
                ds.__pbThumbT = window.setTimeout(() => {
                  const u = el.getAttribute('data-thumb-src')
                  if (u && el.getAttribute('src') !== u) {
                    el.setAttribute('src', u)
                    el.style.background = ''
                  }
                }, 80)
              } else {
                const ds = el as any
                if (ds.__pbThumbT) { clearTimeout(ds.__pbThumbT); ds.__pbThumbT = 0 }
              }
            }
          },
          { root: null, rootMargin: '80px 0px', threshold: 0.01 }
        )
      } catch {
        thumbObserverRef.current = null
      }
    }
    // observe 所有带 data-thumb-src 且未加载的 img
    const imgs = document.querySelectorAll<HTMLImageElement>('img[data-thumb-src]')
    imgs.forEach((el) => {
      if (el.getAttribute('data-thumb-obs') === '1') return
      el.setAttribute('data-thumb-obs', '1')
      // hover:立即触发
      el.addEventListener?.('mouseenter', () => {
        const u = el.getAttribute('data-thumb-src')
        if (u && el.getAttribute('src') !== u) { el.setAttribute('src', u); el.style.background = '' }
      })
      thumbObserverRef.current?.observe(el)
    })
    return () => {
      // 组件每次 render 后 files 变了都重新 observe,这里不 disconnect,保持单例复用
    }
  })

  // ---- 键盘快捷键 ----
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase()
      if (['input', 'textarea', 'select'].includes(tag)) return
      if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault()
        const v = playerRef.current
        if (v) {
          if (v.paused) v.play()
          else v.pause()
        }
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        next()
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        prev()
      } else if (e.key === 'r' || e.key === 'R') {
        e.preventDefault()
        replay()
      } else if (e.key === 'f' || e.key === 'F') {
        e.preventDefault()
        fullscreen()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [next, prev])

  const listHeight = Math.max(360, window.innerHeight - 280)

  return (
    <div style={{ display: 'flex', gap: 16, minHeight: 'calc(100vh - 160px)' }}>
      {/* 侧边栏：事件列表 */}
      <div style={{ width: 320, flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
        <Card size="small" style={{ flexShrink: 0 }} bodyStyle={{ padding: 12 }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Segmented
              block
              value={pbDir}
              onChange={(v) => setPbDir(v as any)}
              options={[
                { label: '📦 压缩', value: 'output' },
                { label: '🎥 原片', value: 'input' },
                { label: '🔀 全部', value: 'all' },
              ]}
            />
            <Space.Compact style={{ width: '100%' }}>
              <DatePicker
                size="small"
                value={date ? dayjs(date) : null}
                onChange={(d) => setDate(d ? d.format('YYYY-MM-DD') : '')}
                placeholder="选择日期"
                style={{ flex: 1 }}
                allowClear
              />
            </Space.Compact>
            <Select
              size="small"
              value={peerFilter}
              onChange={(v) => setPeerFilter(v)}
              options={nodeOptions}
              style={{ width: '100%' }}
              placeholder="选择节点"
            />
            <Checkbox checked={autoplay} onChange={(e) => setAutoplay(e.target.checked)}>
              自动播放下一段
            </Checkbox>
            <Checkbox checked={loop} onChange={(e) => setLoop(e.target.checked)}>
              到末尾循环
            </Checkbox>
            <Checkbox checked={autoNextDay} onChange={(e) => setAutoNextDay(e.target.checked)}>
              看完成天自动跳下一天
            </Checkbox>
            <Button block icon={<ReloadOutlined />} onClick={loadList} loading={loading}>
              刷新列表
            </Button>
          </Space>
        </Card>
        <div style={{ fontSize: 12, color: '#64748b', padding: '8px 12px', borderBottom: '1px solid #f0f0f0' }}>
          📊 <b>{visibleFiles.length}</b> 个事件 · ⏱ 总时长 <b>{pbFmtDur(totalDur)}</b> · 💾 <b>{humanSize(totalSize)}</b>
          {date ? <div>📅 {date}</div> : <div>📅 全部日期</div>}
        </div>
        <Spin spinning={loading}>
          <div style={{ overflowY: 'auto', maxHeight: listHeight }} className="dark-scroll">
            {visibleFiles.length === 0 ? (
              <div style={{ textAlign: 'center', padding: 24, color: '#94a3b8', fontSize: 13 }}>
                没有事件
                <br />
                <span style={{ fontSize: 11 }}>(换个日期试试)</span>
              </div>
            ) : (
              Object.keys(groups)
                .sort()
                .map((dt) => (
                  <div key={dt}>
                    <div style={{ padding: '4px 12px', fontSize: 12, fontWeight: 500, color: '#64748b', background: '#f8fafc', borderBottom: '1px solid #f1f5f9', position: 'sticky', top: 0, zIndex: 1 }}>
                      📅 {dt}
                    </div>
                    {groups[dt].map((f) => {
                      const idx = (f as any)._idx as number
                      const startT = f.meta ? f.meta.start.substring(11, 19) : f.mtime.substring(11, 19)
                      const dur = f.meta ? pbFmtDur(f.meta.dur) : '—'
                      const isCur = idx === index
                      return (
                        <div
                          key={idx}
                          onClick={() => play(idx)}
                          style={{
                            padding: '8px 12px',
                            borderBottom: '1px solid #f1f5f9',
                            cursor: 'pointer',
                            background: isCur ? '#dbeafe' : undefined,
                            borderLeft: isCur ? '4px solid #3b82f6' : undefined,
                          }}
                        >
                          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                            {(() => {
                              // 按时间字符串 hash 出一个稳定的占位渐变(不发任何 HTTP,不调用 ffmpeg)
                              const seed = ((f.meta?.start || f.path || '').split('').reduce((a, c) => a + c.charCodeAt(0), 0)) & 0xfff
                              const c1 = `hsl(${seed % 360}, 30%, 22%)`
                              const c2 = `hsl(${(seed * 7) % 360}, 40%, 12%)`
                              return (
                                <img
                                  data-thumb-src={f.thumbUrl}
                                  loading="lazy"
                                  style={{
                                    width: 80, height: 48, objectFit: 'cover', borderRadius: 4, flexShrink: 0,
                                    border: 'none', display: 'block',
                                    background: `linear-gradient(135deg, ${c1}, ${c2})`,
                                  }}
                                  onError={(e) => {
                                    const t = e.currentTarget as HTMLImageElement
                                    t.removeAttribute('src')
                                    t.style.visibility = 'visible'
                                  }}
                                  alt=""
                                />
                              )
                            })()}
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                                <span>{isCur ? '▶' : '🎬'}</span>
                                <span style={{ fontFamily: 'monospace', fontSize: 13, fontWeight: isCur ? 600 : 400, color: isCur ? '#1d4ed8' : '#1e293b' }}>{startT}</span>
                                {f.type === '压缩' ? <Tag color="green" style={{ fontSize: 10, lineHeight: '16px', margin: 0 }}>📦</Tag> : <Tag color="orange" style={{ fontSize: 10, lineHeight: '16px', margin: 0 }}>🎥</Tag>}
                                {!f.isSelf && <Tag color="blue" style={{ fontSize: 10, lineHeight: '16px', margin: 0 }} title={f.peerId}>🔗 {f.peerName}</Tag>}
                                {f.hasAudio === false ? <Tag color="red" style={{ fontSize: 10, lineHeight: '16px', margin: 0 }} title="该文件不含音频轨,原生音量按钮会显示灰色">⚠️ 无声</Tag> : null}
                                {f.hasAudio === true ? <Tag color="geekblue" style={{ fontSize: 10, lineHeight: '16px', margin: 0 }} title="含音频轨">🎵 有声</Tag> : null}
                              </div>
                              <div style={{ fontSize: 11, color: '#64748b' }}>
                                ⏱ {dur} · {f.size_h || ''}
                              </div>
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                ))
            )}
          </div>
        </Spin>
        <div style={{ padding: 8, fontSize: 11, color: '#94a3b8', borderTop: '1px solid #f0f0f0' }}>
          快捷键: Space 播放/暂停 · ←/→ 上下段 · F 全屏
        </div>
      </div>

      {/* 主播放器 */}
      <div style={{ flex: 1, background: '#0f172a', borderRadius: 8, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 400 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: 12, borderBottom: '1px solid #334155', flexWrap: 'wrap' }}>
          <span style={{ color: '#cbd5e1', fontSize: 13 }}>▶</span>
          <span style={{ color: '#fff', fontSize: 13, fontWeight: 500, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={curTitle}>
            {curTitle}
          </span>
          <Text style={{ color: '#94a3b8', fontSize: 12, whiteSpace: 'nowrap' }}>{curInfo}</Text>
          <Space size={4}>
            <Button
              size="small"
              onClick={() => {
                const nextMuted = !mutedRef.current
                persistMuted(nextMuted)
                if (!nextMuted) {
                  // 用户点击后可以解除浏览器策略的临时静音
                  const v = playerRef.current
                  if (v && v.paused) v.play().catch(() => {})
                }
              }}
              title={muted ? '取消静音' : '静音'}
            >
              {muted ? '🔇 静音' : '🔊 有声'}
            </Button>
            <Button size="small" onClick={prev}>⏮ 上一段</Button>
            <Button size="small" onClick={next}>下一段 ⏭</Button>
            <Button size="small" onClick={replay}>↻ 重播</Button>
            <Button size="small" onClick={fullscreen}>⛶ 全屏</Button>
          </Space>
        </div>
        <div style={{ flex: 1, width: '100%', height: '100%', background: '#000', minHeight: 300, position: 'relative', overflow: 'hidden' }}>
          <video
            ref={playerRef}
            controls
            autoPlay
            playsInline
            preload="metadata"
            onLoadedMetadata={onVideoLoadedMeta}
            style={{ width: '100%', height: '100%', objectFit: 'contain', background: '#000', display: 'block' }}
          />
        </div>
        {/* 媒体诊断:告诉用户 原生声音按钮灰色 到底是什么原因 */}
        {diagnosis.html ? (
          <div
            style={{
              padding: '8px 12px',
              fontSize: 12,
              borderTop: '1px solid #334155',
              background: diagnosis.err ? '#450a0a' : diagnosis.warn ? '#713f12' : '#0c4a6e',
              color: diagnosis.err ? '#fecaca' : diagnosis.warn ? '#fde68a' : '#e0f2fe',
              whiteSpace: 'normal',
              lineHeight: 1.5,
            }}
            dangerouslySetInnerHTML={{ __html: diagnosis.html }}
          />
        ) : null}
        <div style={{ padding: 8, borderTop: '1px solid #334155', display: 'flex', alignItems: 'center', gap: 12, fontSize: 12, flexWrap: 'wrap' }}>
          <span style={{ color: '#cbd5e1' }}>{counter}</span>
          <span style={{ color: '#64748b' }}>|</span>
          <span style={{ color: '#cbd5e1' }}>{nextHint}</span>
          <span style={{ marginLeft: 'auto', color: '#64748b' }}>{progress}</span>
        </div>
      </div>
    </div>
  )
}
