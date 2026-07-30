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
}

export default function PlaybackPage() {
  const { message } = App.useApp()
  const [pbDir, setPbDir] = useState<'output' | 'input' | 'all'>('output')
  const [date, setDate] = useState<string>('') // YYYY-MM-DD
  const [files, setFiles] = useState<PbFile[]>([])
  const [index, setIndex] = useState(-1)
  const [autoplay, setAutoplay] = useState(true)
  const [loop, setLoop] = useState(false)
  const [autoNextDay, setAutoNextDay] = useState(false)
  const [peerFilter, setPeerFilter] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [progress, setProgress] = useState('—')
  const [counter, setCounter] = useState('0 / 0')
  const [nextHint, setNextHint] = useState('下一个: —')
  const [curTitle, setCurTitle] = useState('—')
  const [curInfo, setCurInfo] = useState('')

  // 缩略图
  const [thumbsEnabled, setThumbsEnabled] = useState(true)
  const [thumbsData, setThumbsData] = useState<{ duration: number; thumbs: { i: number; t: number; url: string }[] } | null>(null)
  const [thumbsStatus, setThumbsStatus] = useState('')
  const [thumbsLoading, setThumbsLoading] = useState(false)

  const playerRef = useRef<HTMLVideoElement>(null)
  const stripRef = useRef<HTMLDivElement>(null)
  const thumbsReqRef = useRef(0)
  const thumbsFileRef = useRef<string | null>(null)
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
  const thumbsDataRef = useRef(thumbsData)
  thumbsDataRef.current = thumbsData

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
      setThumbsData(null)
      thumbsFileRef.current = null
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

  // ---- 加载缩略图 ----
  const loadThumbs = useCallback(async (file: PbFile) => {
    if (!thumbsEnabled) {
      setThumbsData(null)
      setThumbsStatus('')
      return
    }
    const fileKey = `${file.peerId}|${file.dir}|${file.path}`
    if (thumbsFileRef.current === fileKey && thumbsDataRef.current) return
    thumbsReqRef.current++
    const myReq = thumbsReqRef.current
    thumbsFileRef.current = fileKey
    setThumbsData(null)
    setThumbsLoading(true)
    setThumbsStatus('正在提取缩略图...')
    const dir = file.dir || pbDirRef.current || 'output'
    // peer 文件走本地代理转发,避免直连 peer 造成 Mixed Content / CORS
    const thumbsUrl = file.isSelf
      ? `/api/pb/thumbs?dir=${encodeURIComponent(dir)}&path=${encodeURIComponent(file.path)}&count=24`
      : `/api/cluster/pb/thumbs?peer=${encodeURIComponent(file.peerId)}&dir=${encodeURIComponent(dir)}&path=${encodeURIComponent(file.path)}&count=24`
    try {
      const r = await api<any>(thumbsUrl, { silent: true })
      if (myReq !== thumbsReqRef.current) return
      if (r.error) {
        setThumbsStatus('缩略图提取失败: ' + r.error)
        return
      }
      setThumbsData(r)
      setThumbsStatus(`${(r.thumbs || []).length} 帧 / 总时长 ${pbFmtDur(r.duration)}`)
    } catch (e: any) {
      if (myReq !== thumbsReqRef.current) return
      setThumbsStatus('缩略图加载失败: ' + e.message)
    } finally {
      if (myReq === thumbsReqRef.current) setThumbsLoading(false)
    }
  }, [thumbsEnabled])

  // ---- 播放 ----
  const play = useCallback(
    (idx: number) => {
      if (idx < 0 || idx >= filesRef.current.length) return
      setIndex(idx)
      const f = filesRef.current[idx]
      const v = playerRef.current
      if (v) {
        v.src = f.streamUrl
        v.load()
        const p = v.play()
        if (p) p.catch(() => {})
      }
      loadThumbs(f)
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
    [loadThumbs]
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
      // 同步播放头
      updatePlayhead()
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

  function updatePlayhead() {
    const strip = stripRef.current
    const v = playerRef.current
    if (!strip || !v || !thumbsDataRef.current) return
    const head = strip.querySelector('.pb-thumb-playhead') as HTMLDivElement | null
    if (!head) return
    const t = v.currentTime || 0
    const dur = thumbsDataRef.current.duration || v.duration || 0
    if (dur <= 0) {
      head.style.display = 'none'
      return
    }
    const pct = Math.max(0, Math.min(1, t / dur))
    head.style.display = 'block'
    head.style.left = `calc(${pct * 100}%)`
    // 高亮当前 thumb
    const thumbs = thumbsDataRef.current.thumbs
    const closest = thumbs.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a))
    strip.querySelectorAll('.pb-thumb.pb-thumb-current').forEach((el) => el.classList.remove('pb-thumb-current'))
    const curEl = strip.querySelector(`.pb-thumb[data-i="${closest.i}"]`)
    if (curEl) curEl.classList.add('pb-thumb-current')
  }

  function seekToThumb(t: number) {
    const v = playerRef.current
    if (v && isFinite(t)) {
      v.currentTime = t
      updatePlayhead()
    }
  }

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
                            <img
                              src={f.thumbUrl}
                              loading="lazy"
                              style={{ width: 80, height: 48, objectFit: 'cover', borderRadius: 4, background: '#e2e8f0', flexShrink: 0 }}
                              onError={(e) => ((e.target as HTMLImageElement).style.visibility = 'hidden')}
                              alt=""
                            />
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                                <span>{isCur ? '▶' : '🎬'}</span>
                                <span style={{ fontFamily: 'monospace', fontSize: 13, fontWeight: isCur ? 600 : 400, color: isCur ? '#1d4ed8' : '#1e293b' }}>{startT}</span>
                                {f.type === '压缩' ? <Tag color="green" style={{ fontSize: 10, lineHeight: '16px', margin: 0 }}>📦</Tag> : <Tag color="orange" style={{ fontSize: 10, lineHeight: '16px', margin: 0 }}>🎥</Tag>}
                                {!f.isSelf && <Tag color="blue" style={{ fontSize: 10, lineHeight: '16px', margin: 0 }} title={f.peerId}>🔗 {f.peerName}</Tag>}
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
            style={{ width: '100%', height: '100%', objectFit: 'contain', background: '#000', display: 'block' }}
          />
        </div>
        {/* 缩略图进度条 */}
        <div style={{ padding: 8, borderTop: '1px solid #334155' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <span style={{ fontSize: 12, color: '#94a3b8' }}>🖼 缩略图进度</span>
            <span style={{ fontSize: 12, color: '#64748b' }}>{thumbsStatus}</span>
            <Checkbox style={{ marginLeft: 'auto', color: '#94a3b8' }} checked={thumbsEnabled} onChange={(e) => setThumbsEnabled(e.target.checked)}>
              <span style={{ color: '#94a3b8' }}>启用</span>
            </Checkbox>
          </div>
          <div
            ref={stripRef}
            style={{ position: 'relative', overflowX: 'auto', overflowY: 'hidden', background: '#0f172a', borderRadius: 4, padding: 4, height: 72, whiteSpace: 'nowrap' }}
          >
            {!thumbsEnabled ? (
              <div style={{ textAlign: 'center', fontSize: 12, color: '#64748b', padding: 24 }}>已禁用缩略图进度</div>
            ) : thumbsLoading ? (
              <div style={{ textAlign: 'center', fontSize: 12, color: '#64748b', padding: 24 }}>正在提取缩略图...</div>
            ) : !thumbsData ? (
              <div style={{ textAlign: 'center', fontSize: 12, color: '#64748b', padding: 24 }}>选择左侧视频后加载缩略图</div>
            ) : !thumbsData.thumbs || thumbsData.thumbs.length === 0 ? (
              <div style={{ textAlign: 'center', fontSize: 12, color: '#64748b', padding: 24 }}>无法获取缩略图(可能是 0 时长视频)</div>
            ) : (
              <>
                {thumbsData.thumbs.map((thumb) => (
                  <img
                    key={thumb.i}
                    className="pb-thumb pb-thumb-loading"
                    data-i={thumb.i}
                    data-t={thumb.t}
                    src={thumb.url}
                    alt={`${thumb.t.toFixed(1)}s`}
                    title={pbFmtDur(thumb.t)}
                    onLoad={(e) => e.currentTarget.classList.remove('pb-thumb-loading')}
                    onClick={() => seekToThumb(thumb.t)}
                  />
                ))}
                <div className="pb-thumb-playhead" style={{ display: 'none' }} />
              </>
            )}
          </div>
        </div>
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
