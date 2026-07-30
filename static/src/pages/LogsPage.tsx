// 日志页：用 SSE(/api/logs/stream) 实时推送日志行，虚拟滚动避免卡顿。
// 支持级别过滤(all/error/warn/ok/info)、关键字搜索、行号、自动滚到底、下载。
import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { Card, Space, Input, Select, Checkbox, Button, Tag, Typography } from 'antd'
import { FixedSizeList as List } from 'react-window'
import { apiUrl, api } from '../api'
import { classifyLogLevel, LOG_LEVEL_COLOR, highlight, type LogLevel } from '../utils'

const { Text } = Typography

interface LogLine {
  no: number
  text: string
}

const ROW_HEIGHT = 20
const MAX_LINES = 5000

export default function LogsPage() {
  const [lines, setLines] = useState<LogLine[]>([])
  const [total, setTotal] = useState(0)
  const [level, setLevel] = useState<string>('all')
  const [qInput, setQInput] = useState('')
  const [q, setQ] = useState('')
  const [autoScroll, setAutoScroll] = useState(true)
  const [connected, setConnected] = useState(false)
  const [everConnected, setEverConnected] = useState(false)
  const sinceRef = useRef(0)
  const listRef = useRef<List>(null)
  const esRef = useRef<EventSource | null>(null)
  const linesRef = useRef<LogLine[]>([])
  linesRef.current = lines
  const autoScrollRef = useRef(autoScroll)
  autoScrollRef.current = autoScroll
  const reconnectTimer = useRef<any>(null)
  const reconnectDelay = useRef(600)

  // 客户端过滤：级别 + 关键字
  const filtered = useMemo(() => {
    return lines.filter((l) => {
      if (level !== 'all' && classifyLogLevel(l.text) !== level) return false
      if (q && !(l.text || '').toLowerCase().includes(q.toLowerCase())) return false
      return true
    })
  }, [lines, level, q])

  // 统计
  const counts = useMemo(() => {
    const c = { error: 0, warn: 0, ok: 0, info: 0 }
    for (const l of lines) c[classifyLogLevel(l.text)]++
    return c
  }, [lines])

  const connect = useCallback((since: number) => {
    try {
      if (esRef.current) {
        esRef.current.close()
        esRef.current = null
      }
      const url = apiUrl(`/api/logs/stream?since=${since}`)
      const es = new EventSource(url)
      esRef.current = es
      es.onopen = () => {
        setConnected(true)
        setEverConnected(true)
        reconnectDelay.current = 600 // 重置退避
      }
      es.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data)
          if (d.total != null) {
            sinceRef.current = d.total
            setTotal(d.total)
          }
          if (d.n != null && d.text != null) {
            setLines((prev) => {
              // 去重(以 no 为 key)
              const map = new Map<number, LogLine>()
              for (const l of prev) map.set(l.no, l)
              map.set(d.n, { no: d.n, text: d.text })
              // 上限：保留最后 MAX_LINES 行
              let arr = Array.from(map.values()).sort((a, b) => a.no - b.no)
              if (arr.length > MAX_LINES) arr = arr.slice(arr.length - MAX_LINES)
              return arr
            })
          }
        } catch {
          /* 非 JSON 行忽略 */
        }
      }
      es.addEventListener('end', () => {
        // 服务端主动结束(10 分钟上限)，重连拿增量
        es.close()
        setConnected(false)
        scheduleReconnect()
      })
      es.onerror = () => {
        setConnected(false)
        es.close()
        scheduleReconnect()
      }
    } catch {
      // EventSource 不支持时兜底：轮询 /api/logs
      setConnected(false)
      pollFallback()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const scheduleReconnect = useCallback(() => {
    if (reconnectTimer.current) return
    const delay = reconnectDelay.current
    reconnectDelay.current = Math.min(delay * 2, 30000) // 指数退避,上限 30s
    reconnectTimer.current = setTimeout(() => {
      reconnectTimer.current = null
      connect(sinceRef.current)
    }, delay)
  }, [connect])

  // 兜底轮询（EventSource 不可用时）
  const pollFallback = useCallback(async () => {
    try {
      const r = await api<any>(`/api/logs?since=${sinceRef.current}&limit=1000&max_lines=${MAX_LINES}&level=all`, {
        silent: true,
      })
      if (r.lines) {
        const nos = r.line_nos || []
        setLines((prev) => {
          const map = new Map<number, LogLine>()
          for (const l of prev) map.set(l.no, l)
          r.lines.forEach((t: string, i: number) => map.set(nos[i] || 0, { no: nos[i] || 0, text: t }))
          let arr = Array.from(map.values()).sort((a, b) => a.no - b.no)
          if (arr.length > MAX_LINES) arr = arr.slice(arr.length - MAX_LINES)
          return arr
        })
      }
      if (r.total != null) {
        sinceRef.current = r.total
        setTotal(r.total)
      }
    } catch {
      /* */
    }
  }, [])

  useEffect(() => {
    connect(0)
    return () => {
      if (esRef.current) esRef.current.close()
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
    }
  }, [connect])

  // 自动滚到底
  useEffect(() => {
    if (autoScroll && filtered.length > 0 && listRef.current) {
      listRef.current.scrollToItem(filtered.length - 1, 'end')
    }
  }, [filtered.length, autoScroll])

  const searchTimer = useRef<any>(null)
  const onSearchInput = (v: string) => {
    setQInput(v)
    clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => setQ(v.trim()), 300)
  }

  const Row = ({ index, style, data }: any) => {
    const l = data[index] as LogLine
    const lvl = classifyLogLevel(l.text)
    const color = LOG_LEVEL_COLOR[lvl as LogLevel] || LOG_LEVEL_COLOR.info
    return (
      <div style={{ ...style, display: 'flex', alignItems: 'center' }}>
        <span className="log-line-no" style={{ flexShrink: 0, width: 60, textAlign: 'right' }}>
          {String(l.no).padStart(6, ' ')}
        </span>
        <span style={{ color, whiteSpace: 'pre-wrap', wordBreak: 'break-all', paddingLeft: 8 }}>
          {highlight(l.text, q)}
        </span>
      </div>
    )
  }

  const listHeight = Math.max(300, window.innerHeight - 300)

  return (
    <Card
      title="实时日志"
      extra={
        <Space wrap>
          <Tag color={connected ? 'green' : 'orange'}>
            {connected ? '● 已连接' : everConnected ? '○ 重连中' : '○ 连接中'}
          </Tag>
          <Input
            size="small"
            allowClear
            placeholder="搜索关键字..."
            value={qInput}
            onChange={(e) => onSearchInput(e.target.value)}
            style={{ width: 160 }}
          />
          <Select
            size="small"
            value={level}
            onChange={setLevel}
            style={{ width: 90 }}
            options={[
              { value: 'all', label: '全部' },
              { value: 'info', label: '信息' },
              { value: 'ok', label: '成功' },
              { value: 'warn', label: '警告' },
              { value: 'error', label: '错误' },
            ]}
          />
          <Checkbox checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)}>
            自动滚到底
          </Checkbox>
          <Button size="small" onClick={() => listRef.current?.scrollToItem(filtered.length - 1, 'end')}>
            跳到最新
          </Button>
          <Button
            size="small"
            onClick={() => {
              setLines([])
              sinceRef.current = 0
              connect(0)
            }}
          >
            清屏
          </Button>
          <a href={apiUrl('/api/logs/download')} download>
            <Button size="small">下载</Button>
          </a>
        </Space>
      }
    >
      <div
        className="dark-scroll"
        style={{
          background: '#0f172a',
          borderRadius: 6,
          padding: 8,
          height: listHeight,
        }}
      >
        {filtered.length === 0 ? (
          <div style={{ color: '#64748b', fontSize: 12, padding: 16 }}>（无匹配行）</div>
        ) : (
          <List
            ref={listRef}
            height={listHeight - 16}
            itemCount={filtered.length}
            itemSize={ROW_HEIGHT}
            width="100%"
            itemData={filtered}
            style={{ color: '#e2e8f0', fontSize: 12, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
          >
            {Row}
          </List>
        )}
      </div>
      <div style={{ marginTop: 8, fontSize: 12, color: '#94a3b8' }}>
        服务器总行数 <b>{total}</b> · 已加载 <b>{lines.length}</b>{' '}
        <span style={{ color: '#22c55e' }}>[完成 {counts.ok}]</span>{' '}
        <span style={{ color: '#f59e0b' }}>[警告 {counts.warn}]</span>{' '}
        <span style={{ color: '#ef4444' }}>[错误 {counts.error}]</span>{' '}
        <span>[信息 {counts.info}]</span>
        {q ? <> · 搜索: <i>{q}</i></> : null} · 过滤: {level}
      </div>
    </Card>
  )
}
