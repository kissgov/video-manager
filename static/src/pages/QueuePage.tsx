// 队列页：任务列表，状态筛选、搜索、排序、分页、批量重试/删除、同步、回填时长
import { useEffect, useState, useCallback, useRef } from 'react'
import { Card, Table, Tag, Input, Button, Space, Segmented, Statistic, Row, Col, App, Tooltip, Typography } from 'antd'
import { ReloadOutlined, RedoOutlined, DeleteOutlined, SyncOutlined, FieldTimeOutlined } from '@ant-design/icons'
import { api } from '../api'
import { fmtBytes, formatDurationSec } from '../utils'

const { Text } = Typography

interface QItem {
  id: number
  rel_path: string
  size?: number
  output_size?: number
  ratio?: number
  duration_sec?: number
  attempts?: number
  status: string
  last_error?: string
  ended_at?: string
}
interface QResult {
  items: QItem[]
  total: number
  limit: number
  offset: number
}
interface QStats {
  pending?: number
  running?: number
  done?: number
  failed?: number
  skipped?: number
}

const PAGE_SIZE = 100
const STATUS_TAGS: Record<string, { color: string; text: string }> = {
  pending: { color: 'blue', text: '待处理' },
  running: { color: 'orange', text: '处理中' },
  done: { color: 'green', text: '已完成' },
  skipped: { color: 'default', text: '跳过' },
  failed: { color: 'red', text: '失败' },
}

export default function QueuePage() {
  const { message, modal } = App.useApp()
  const [stats, setStats] = useState<QStats>({})
  const [items, setItems] = useState<QItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<string>('all')
  const [qInput, setQInput] = useState('')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [sortBy, setSortBy] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [selected, setSelected] = useState<number[]>([])
  const [actionLoading, setActionLoading] = useState(false)
  const searchTimer = useRef<any>(null)

  const loadStats = useCallback(async () => {
    try {
      const s = await api<QStats>('/api/queue/stats', { silent: true })
      setStats(s)
    } catch {}
  }, [])

  const loadList = useCallback(async () => {
    setLoading(true)
    try {
      const params: Record<string, any> = {
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }
      if (filter !== 'all') params.status = filter
      if (q) params.q = q
      if (sortBy) {
        params.sort_by = sortBy
        params.sort_dir = sortDir
      }
      const sp = new URLSearchParams()
      for (const [k, v] of Object.entries(params)) sp.set(k, String(v))
      const r = await api<QResult>(`/api/queue?${sp}`, { silent: true })
      setItems(r.items || [])
      setTotal(r.total || 0)
    } catch {
      /* api 提示 */
    } finally {
      setLoading(false)
    }
  }, [filter, q, page, sortBy, sortDir])

  useEffect(() => {
    loadStats()
    loadList()
  }, [loadStats, loadList])

  // 定时刷新统计（队列页可见时）
  useEffect(() => {
    const t = setInterval(() => {
      loadStats()
    }, 3000)
    return () => clearInterval(t)
  }, [loadStats])

  const onSearchInput = (v: string) => {
    setQInput(v)
    clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => {
      setQ(v.trim())
      setPage(1)
    }, 300)
  }

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 70,
      sorter: true,
      render: (v: number) => <Text type="secondary" code>{v}</Text>,
    },
    {
      title: '文件路径',
      dataIndex: 'rel_path',
      ellipsis: true,
      sorter: true,
      render: (v: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{v}</span>,
    },
    {
      title: '输入',
      dataIndex: 'size',
      width: 80,
      align: 'right' as const,
      sorter: true,
      render: (v?: number) => (v != null ? fmtBytes(v) : <Text type="secondary">—</Text>),
    },
    {
      title: '输出',
      dataIndex: 'output_size',
      width: 80,
      align: 'right' as const,
      sorter: true,
      render: (v?: number) => (v != null ? fmtBytes(v) : <Text type="secondary">—</Text>),
    },
    {
      title: '压缩比',
      dataIndex: 'ratio',
      width: 90,
      align: 'right' as const,
      sorter: true,
      render: (v?: number) => {
        if (v == null) return <Text type="secondary">—</Text>
        const pct = (v * 100).toFixed(1)
        const color = v < 0.3 ? '#16a34a' : v < 0.6 ? '#d97706' : '#dc2626'
        return <span style={{ fontFamily: 'monospace', color }}>{pct}%</span>
      },
    },
    {
      title: '用时',
      dataIndex: 'duration_sec',
      width: 90,
      align: 'right' as const,
      sorter: true,
      render: (v?: number) => (v != null ? <span style={{ fontFamily: 'monospace' }}>{formatDurationSec(v)}</span> : <Text type="secondary">—</Text>),
    },
    {
      title: '尝试',
      dataIndex: 'attempts',
      width: 60,
      align: 'center' as const,
      sorter: true,
      render: (v?: number) => v || 0,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      sorter: true,
      render: (v: string) => {
        const t = STATUS_TAGS[v] || { color: 'default', text: v }
        return <Tag color={t.color}>{t.text}</Tag>
      },
    },
    {
      title: '最后错误',
      dataIndex: 'last_error',
      ellipsis: true,
      render: (v?: string) =>
        v ? (
          <Tooltip title={v}>
            <span style={{ fontSize: 12, color: '#dc2626' }}>
              {v.length > 40 ? v.slice(0, 40) + '…' : v}
            </span>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '结束时间',
      dataIndex: 'ended_at',
      width: 160,
      sorter: true,
      render: (v?: string) => (v ? <Text type="secondary" code>{v}</Text> : '—'),
    },
  ]

  async function retrySelected() {
    if (!selected.length) return
    modal.confirm({
      title: `确认重试选中的 ${selected.length} 个任务？`,
      content: '会删除对应的输出文件，重新标记为待处理。',
      okText: '重试',
      okType: 'danger',
      onOk: async () => {
        setActionLoading(true)
        try {
          const r = await api<any>('/api/queue/retry', { body: { ids: selected } })
          message.success(`重试 ${r.result.reset} 个任务(删除 ${r.result.deleted_outputs} 个输出)`)
          setSelected([])
          await loadList()
          await loadStats()
        } catch {
        } finally {
          setActionLoading(false)
        }
      },
    })
  }

  async function deleteSelected() {
    if (!selected.length) return
    modal.confirm({
      title: `确认删除选中的 ${selected.length} 个任务？`,
      content: '只是从队列表中移除记录，不会删除 /input 或 /output 里的实际文件。正在跑的任务会被跳过。',
      okText: '删除',
      okType: 'danger',
      onOk: async () => {
        setActionLoading(true)
        try {
          const r = await api<any>('/api/queue/delete', { method: 'POST', body: { ids: selected } })
          let msg = `删除 ${r.result.deleted} 个任务`
          if (r.result.rejected) msg += `，跳过 ${r.result.rejected} 个正在跑的任务`
          message.success(msg)
          setSelected([])
          await loadList()
          await loadStats()
        } catch {
        } finally {
          setActionLoading(false)
        }
      },
    })
  }

  async function rescan() {
    modal.confirm({
      title: '重新扫描输入目录并同步到队列？',
      content: '仅添加新文件 / 标记已完成的输出，不会改动正在处理的项。',
      okText: '同步',
      onOk: async () => {
        setActionLoading(true)
        try {
          const r = await api<any>('/api/queue/sync', { method: 'POST' })
          const s = r.synced
          message.success(
            `同步完成: 待处理 +${s.added_input} / 已完成 +${s.added_done} / 标记 +${s.updated_done} / 调和 ${s.reconciled || 0}`
          )
          await loadList()
          await loadStats()
        } catch {
        } finally {
          setActionLoading(false)
        }
      },
    })
  }

  async function backfill() {
    setActionLoading(true)
    try {
      const r = await api<any>('/api/queue/backfill_durations', { method: 'POST' })
      message.success('回填完成: ' + JSON.stringify(r.result))
      await loadList()
    } catch {
    } finally {
      setActionLoading(false)
    }
  }

  return (
    <Card>
      {/* 统计条 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col><Statistic title="待处理" value={stats.pending ?? 0} valueStyle={{ color: '#1677ff' }} /></Col>
        <Col><Statistic title="处理中" value={stats.running ?? 0} valueStyle={{ color: '#f59e0b' }} /></Col>
        <Col><Statistic title="已完成" value={stats.done ?? 0} valueStyle={{ color: '#22c55e' }} /></Col>
        <Col><Statistic title="跳过" value={stats.skipped ?? 0} valueStyle={{ color: '#94a3b8' }} /></Col>
        <Col><Statistic title="失败" value={stats.failed ?? 0} valueStyle={{ color: '#ef4444' }} /></Col>
        <Col flex="auto" />
        <Col>
          <Space>
            <Button icon={<SyncOutlined />} onClick={rescan} loading={actionLoading}>重新扫描</Button>
            <Button icon={<FieldTimeOutlined />} onClick={backfill} loading={actionLoading}>回填时长</Button>
            <Button
              type="primary"
              icon={<RedoOutlined />}
              onClick={retrySelected}
              disabled={!selected.length}
              loading={actionLoading}
            >
              重试{selected.length ? ` (${selected.length})` : ''}
            </Button>
            <Button danger icon={<DeleteOutlined />} onClick={deleteSelected} disabled={!selected.length} loading={actionLoading}>
              删除{selected.length ? ` (${selected.length})` : ''}
            </Button>
          </Space>
        </Col>
      </Row>

      {/* 筛选 + 搜索 */}
      <Space wrap style={{ marginBottom: 12 }}>
        <Segmented
          value={filter}
          onChange={(v) => {
            setFilter(v as string)
            setPage(1)
          }}
          options={[
            { label: '全部', value: 'all' },
            { label: '待处理', value: 'pending' },
            { label: '处理中', value: 'running' },
            { label: '失败', value: 'failed' },
            { label: '已完成', value: 'done' },
            { label: '跳过', value: 'skipped' },
          ]}
        />
        <Input
          allowClear
          prefix={<ReloadOutlined rotate={90} />}
          placeholder="搜索文件名..."
          value={qInput}
          onChange={(e) => onSearchInput(e.target.value)}
          style={{ width: 220 }}
        />
      </Space>

      <Table
        size="small"
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={items}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (keys) => setSelected(keys.map((k) => Number(k))),
        }}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showTotal: (t, [from, to]) => `${from}-${to} / ${t}`,
          onChange: (p) => setPage(p),
          showSizeChanger: false,
        }}
        onChange={(_pag, _fil, sorter: any) => {
          if (!sorter || !sorter.field) {
            setSortBy(null)
          } else {
            setSortBy(sorter.field)
            setSortDir(sorter.order === 'ascend' ? 'asc' : 'desc')
          }
          setPage(1)
        }}
        scroll={{ x: 1000 }}
      />
    </Card>
  )
}
