// 概览页：顶部状态卡 + 进度 + 队列概览 + 磁盘 + 最近失败 + 最近任务历史
import { useEffect, useState, useCallback } from 'react'
import { Row, Col, Card, Statistic, Progress, Table, Tag, Empty, Spin, Space, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { useStore } from '../store'
import { fmtBytes, formatDuration, formatDurationPlain } from '../utils'

const { Text } = Typography

interface QueueStats {
  pending?: number
  running?: number
  done?: number
  failed?: number
  skipped?: number
  total?: number
}
interface StatsData {
  today?: { runs?: number; total?: number; success?: number; skipped?: number; failed?: number }
  total?: {
    total?: number
    runs?: number
    success?: number
    failed?: number
    recent_avg_dur_sec?: number
    recent_throughput_per_sec?: number
    duration_sec?: number
  }
  recent?: any[]
}
interface DiskItem {
  used?: number
  total?: number
  free?: number
  percent?: number
  used_h?: string
  total_h?: string
  error?: string
}

export default function OverviewPage() {
  const { status } = useStore()
  const navigate = useNavigate()
  const [stats, setStats] = useState<StatsData | null>(null)
  const [qs, setQs] = useState<QueueStats | null>(null)
  const [disk, setDisk] = useState<Record<string, DiskItem> | null>(null)
  const [failed, setFailed] = useState<any[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [s, q, d, fr] = await Promise.all([
        api<StatsData>('/api/stats', { silent: true }),
        api<QueueStats>('/api/queue/stats', { silent: true }),
        api<Record<string, DiskItem>>('/api/disk', { silent: true }),
        api<any>('/api/queue?status=failed&limit=5&sort_by=id&sort_dir=desc', { silent: true }),
      ])
      setStats(s)
      setQs(q)
      setDisk(d)
      setFailed(fr?.items || [])
    } catch {
      /* 静默 */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [load])

  const pending = qs?.pending || 0
  const running = qs?.running || 0
  const done = qs?.done || 0
  const failedN = qs?.failed || 0
  const skipped = qs?.skipped || 0
  const total = qs?.total || 0
  const finished = done + failedN + skipped
  const pct = total > 0 ? Math.round((finished * 100) / total) : 0

  // 预估剩余
  let eta = ''
  if (running > 0 && stats?.total?.recent_avg_dur_sec) {
    const avg = stats.total.recent_avg_dur_sec
    const tp = stats.total.recent_throughput_per_sec || 1
    if (avg > 0 && tp > 0) eta = '· 预估剩余 ' + formatDurationPlain((avg * pending) / tp)
  }

  const bars: { label: string; n: number; color: string }[] = [
    { label: '待处理', n: pending, color: '#3b82f6' },
    { label: '处理中', n: running, color: '#f59e0b' },
    { label: '已完成', n: done, color: '#22c55e' },
    { label: '跳过', n: skipped, color: '#94a3b8' },
    { label: '失败', n: failedN, color: '#ef4444' },
  ]

  const historyCols = [
    { title: '#', dataIndex: 'id', width: 60 },
    { title: '开始', dataIndex: 'started_at', render: (v: string) => <Text code>{v || '—'}</Text> },
    { title: '结束', dataIndex: 'ended_at', render: (v: string) => <Text code>{v || '进行中…'}</Text> },
    {
      title: '触发',
      dataIndex: 'trigger',
      render: (v: string) => <Tag>{v || '-'}</Tag>,
    },
    { title: '成功', dataIndex: 'success', align: 'right' as const, render: (v: number) => <Text type="success">{v || 0}</Text> },
    { title: '跳过', dataIndex: 'skipped', align: 'right' as const, render: (v: number) => <Text type="secondary">{v || 0}</Text> },
    { title: '失败', dataIndex: 'failed', align: 'right' as const, render: (v: number) => <Text type="danger">{v || 0}</Text> },
    { title: '总数', dataIndex: 'total', align: 'right' as const },
    {
      title: '耗时',
      render: (_: any, r: any) =>
        r.started_at && r.ended_at ? formatDuration(r.started_at, r.ended_at) : '—',
    },
  ]

  return (
    <Spin spinning={loading && !stats}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {/* 顶部 4 卡 */}
        <Row gutter={[16, 16]}>
          <Col xs={24} sm={12} md={6}>
            <Card>
              <Statistic
                title="当前状态"
                value={status?.running ? '运行中' : '空闲'}
                valueStyle={{ color: status?.running ? '#22c55e' : '#64748b' }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {status?.pid ? `pid=${status.pid}${status.external ? ' (外部)' : ''}` : ''}
              </Text>
            </Card>
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Card>
              <Statistic title="正在处理" value={status?.current_file || '—'} valueStyle={{ fontSize: 14, fontFamily: 'monospace', wordBreak: 'break-all' }} />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {status?.started_at ? `开始于 ${status.started_at}` : ''}
              </Text>
            </Card>
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Card>
              <div style={{ fontSize: 12, color: '#64748b', marginBottom: 4 }}>今日任务 / 文件</div>
              <div>
                <span style={{ fontSize: 24, fontWeight: 700 }}>{stats?.today?.runs || 0}</span>
                <span style={{ fontSize: 12, color: '#94a3b8' }}> 次 </span>
                <span style={{ fontSize: 24, fontWeight: 700, color: '#1677ff', marginLeft: 8 }}>{stats?.today?.total || 0}</span>
                <span style={{ fontSize: 12, color: '#94a3b8' }}> 文件</span>
              </div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                成功 {stats?.today?.success || 0} · 跳过 {stats?.today?.skipped || 0} · 失败 {stats?.today?.failed || 0}
              </Text>
            </Card>
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Card>
              <div style={{ fontSize: 12, color: '#64748b', marginBottom: 4 }}>累计</div>
              <div>
                <span style={{ fontSize: 24, fontWeight: 700 }}>{stats?.total?.total || 0}</span>
                <span style={{ fontSize: 12, color: '#94a3b8' }}> 文件</span>
              </div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                运行 {stats?.total?.runs || 0} 次 · 成功 {stats?.total?.success || 0} · 失败 {stats?.total?.failed || 0}
              </Text>
            </Card>
          </Col>
        </Row>

        {/* 进度 + 队列概览 */}
        <Row gutter={[16, 16]}>
          <Col xs={24} md={12}>
            <Card title="当前任务进度" extra={<a onClick={() => navigate('/queue')}>查看队列 →</a>}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {running > 0
                  ? `进行中 ${finished}/${total} (${pct}%) · 正在跑 ${running} 个`
                  : total > 0
                  ? `总进度 ${finished}/${total} (${pct}%)`
                  : '队列为空'}
              </Text>
              <Progress percent={pct} style={{ marginTop: 8 }} />
              <Text type="secondary" style={{ fontSize: 12 }}>
                完成 {done} · 跳过 {skipped} · 失败 {failedN} · 待处理 {pending} {eta}
              </Text>
            </Card>
          </Col>
          <Col xs={24} md={12}>
            <Card title="队列概览" extra={<a onClick={() => navigate('/queue')}>查看详情 →</a>}>
              {bars.map((b) => {
                const w = total > 0 ? Math.round((b.n * 100) / total) : 0
                return (
                  <div key={b.label} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                    <span style={{ width: 56, fontSize: 12, color: '#64748b' }}>{b.label}</span>
                    <div style={{ flex: 1, background: '#f1f5f9', borderRadius: 4, height: 8, overflow: 'hidden' }}>
                      <div style={{ width: `${w}%`, height: '100%', background: b.color }} />
                    </div>
                    <span style={{ width: 40, textAlign: 'right', fontFamily: 'monospace', fontSize: 12 }}>{b.n}</span>
                  </div>
                )
              })}
              <Text type="secondary" style={{ fontSize: 12, marginTop: 8, display: 'block' }}>
                总计 {total} 个任务
              </Text>
            </Card>
          </Col>
        </Row>

        {/* 磁盘 + 最近失败 */}
        <Row gutter={[16, 16]}>
          <Col xs={24} md={12}>
            <Card title="磁盘用量" extra={<a onClick={() => navigate('/files')}>浏览文件 →</a>}>
              {disk &&
                Object.entries(disk).map(([k, v]) => {
                  if (!v || v.error) return null
                  const p = v.percent || 0
                  const color = p > 85 ? '#ef4444' : p > 70 ? '#f59e0b' : '#3b82f6'
                  return (
                    <div key={k} style={{ marginBottom: 12 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                        <span style={{ fontFamily: 'monospace' }}>{k}</span>
                        <span style={{ color: '#64748b' }}>
                          {v.used_h || fmtBytes(v.used)} / {v.total_h || fmtBytes(v.total)} ({p}%)
                        </span>
                      </div>
                      <Progress percent={p} strokeColor={color} showInfo={false} size="small" />
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        剩余 {v.free != null ? fmtBytes(v.free) : '—'}
                      </Text>
                    </div>
                  )
                })}
              {!disk || Object.keys(disk).length === 0 ? <Empty description="无磁盘信息" /> : null}
            </Card>
          </Col>
          <Col xs={24} md={12}>
            <Card title="最近失败" extra={<a onClick={() => navigate('/queue')}>查看全部 →</a>}>
              {failed.length === 0 ? (
                <Empty description="无失败任务 ✓" />
              ) : (
                failed.map((it) => (
                  <div
                    key={it.id}
                    style={{ display: 'flex', gap: 8, padding: '4px 0', borderBottom: '1px solid #f1f5f9' }}
                  >
                    <Tag color="error" style={{ flexShrink: 0 }}>失败</Tag>
                    <span style={{ fontFamily: 'monospace', fontSize: 12, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={it.rel_path}>
                      {it.rel_path}
                    </span>
                    <Text type="secondary" style={{ fontSize: 12 }}>尝试 {it.attempts}</Text>
                  </div>
                ))
              )}
            </Card>
          </Col>
        </Row>

        {/* 最近任务 */}
        <Card title="最近任务" extra={<a onClick={load}>刷新</a>}>
          <Table
            size="small"
            rowKey="id"
            columns={historyCols}
            dataSource={stats?.recent || []}
            pagination={false}
            scroll={{ x: 700 }}
            locale={{ emptyText: <Empty description="暂无数据" /> }}
          />
        </Card>
      </Space>
    </Spin>
  )
}
