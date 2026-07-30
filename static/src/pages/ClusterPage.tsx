// 集群页：本机身份 + Peer 管理 + 视图切换(状态/文件) + 节点卡片 + 集群文件聚合
import { useEffect, useState, useCallback, useRef } from 'react'
import {
  Card, Form, Input, Button, Space, Tag, Table, Segmented, Empty, Spin, Typography,
  Row, Col, Statistic, App, Tooltip,
} from 'antd'
import { PlusOutlined, ReloadOutlined, DeleteOutlined } from '@ant-design/icons'
import { api, apiUrl } from '../api'
import { humanSize } from '../utils'
import VideoModal from '../components/VideoModal'

const { Text } = Typography

interface SelfInfo {
  id?: string
  name?: string
  url?: string
  state?: any
}
interface PeerInfo {
  id?: string
  name?: string
  url?: string
  ok?: boolean
  fetched_at?: string
  error?: string
  state?: any
}
interface ClusterPeers {
  self?: SelfInfo
  peers?: PeerInfo[]
  last_refresh?: string
}
interface FileItem {
  path: string
  size?: number
  size_h?: string
  mtime?: string
}
interface PeerFiles {
  ok?: boolean
  name?: string
  id?: string
  url?: string
  error?: string
  files?: {
    items?: FileItem[]
    count?: number
    total_size_h?: string
    total_size?: number
    page?: number
    page_size?: number
    total_pages?: number
    error?: string
  }
}
interface ClusterFiles {
  self?: PeerFiles
  peers?: PeerFiles[]
}

export default function ClusterPage() {
  const { message, modal } = App.useApp()

  // ---- 本机身份 ----
  const [self, setSelf] = useState<SelfInfo>({})
  const [selfForm] = Form.useForm()
  const [selfMsg, setSelfMsg] = useState('')

  // ---- peers ----
  const [peers, setPeers] = useState<PeerInfo[]>([])
  const [peersMsg, setPeersMsg] = useState('')
  const [lastRefresh, setLastRefresh] = useState('—')

  // ---- 视图 ----
  const [view, setView] = useState<'status' | 'files'>('status')

  // ---- 集群文件 ----
  const [cfDir, setCfDir] = useState('output')
  const [cfQ, setCfQ] = useState('')
  const [cfQInput, setCfQInput] = useState('')
  const [cfSort, setCfSort] = useState('mtime')
  const [cfOrder, setCfOrder] = useState<'asc' | 'desc'>('desc')
  const [cfPage, setCfPage] = useState(1)
  const [cfPageSize, setCfPageSize] = useState(50)
  const [cfData, setCfData] = useState<ClusterFiles | null>(null)
  const [cfLoading, setCfLoading] = useState(false)
  const cfSearchTimer = useRef<any>(null)
  const [videoModal, setVideoModal] = useState<{ open: boolean; url: string; title: string; info: string }>({ open: false, url: '', title: '', info: '' })

  const loadCluster = useCallback(async () => {
    try {
      const r = await api<ClusterPeers>('/api/cluster/peers', { silent: true })
      if (r.self) {
        setSelf(r.self)
        selfForm.setFieldsValue({ id: r.self.id, name: r.self.name, url: r.self.url })
      }
      setPeers(
        (r.peers || []).map((p) => ({
          id: p.id || p.name || '',
          name: p.name || '',
          url: (p.url || '').replace(/\/+$/, ''),
          ok: p.ok,
          fetched_at: p.fetched_at,
          error: p.error,
          state: p.state,
        }))
      )
      setLastRefresh(r.last_refresh || '—')
    } catch {}
  }, [selfForm])

  useEffect(() => {
    loadCluster()
    const t = setInterval(loadCluster, 6000)
    return () => clearInterval(t)
  }, [loadCluster])

  // ---- 集群文件加载 ----
  const loadFiles = useCallback(async () => {
    setCfLoading(true)
    try {
      const params: Record<string, any> = { dir: cfDir }
      if (cfQ) params.q = cfQ
      if (cfSort) params.sort = cfSort
      if (cfOrder) params.order = cfOrder
      if (cfPage > 1) params.page = cfPage
      if (cfPageSize) params.page_size = cfPageSize
      const sp = new URLSearchParams()
      for (const [k, v] of Object.entries(params)) sp.set(k, String(v))
      const r = await api<ClusterFiles>(`/api/cluster/files?${sp}`, { silent: true })
      setCfData(r)
    } catch {
    } finally {
      setCfLoading(false)
    }
  }, [cfDir, cfQ, cfSort, cfOrder, cfPage, cfPageSize])

  useEffect(() => {
    if (view === 'files') loadFiles()
  }, [view, loadFiles])

  // ---- 本机身份保存 ----
  async function saveSelf(v: any) {
    try {
      await api('/api/cluster/self/update', { method: 'POST', body: { id: v.id, name: v.name, url: v.url } })
      setSelfMsg('已保存 ' + new Date().toLocaleTimeString())
      loadCluster()
    } catch (e: any) {
      setSelfMsg('❌ ' + e.message)
    }
  }

  // ---- peers 编辑 ----
  function addPeer() {
    setPeers((p) => [...p, { id: '', name: '', url: '' }])
  }
  function removePeer(i: number) {
    setPeers((p) => p.filter((_, idx) => idx !== i))
  }
  function setPeerField(i: number, k: keyof PeerInfo, v: string) {
    setPeers((p) => p.map((peer, idx) => (idx === i ? { ...peer, [k]: v } : peer)))
  }
  async function savePeers() {
    try {
      const r = await api<any>('/api/cluster/peers/upsert', { method: 'POST', body: { peers } })
      if (r.ok) {
        setPeersMsg('已保存,正在后台拉取状态...')
        setTimeout(loadCluster, 1500)
      } else setPeersMsg('❌ ' + r.error)
    } catch (e: any) {
      setPeersMsg('❌ ' + e.message)
    }
  }
  async function refreshCluster() {
    try {
      await api('/api/cluster/refresh', { method: 'POST' })
      loadCluster()
    } catch {}
  }

  // ---- 排序 ----
  function cfSortBy(col: string) {
    if (cfSort === col) {
      setCfOrder((o) => (o === 'asc' ? 'desc' : 'asc'))
    } else {
      setCfSort(col)
      setCfOrder(col === 'name' || col === 'path' ? 'asc' : 'desc')
    }
    setCfPage(1)
  }
  function sortArrow(col: string) {
    if (cfSort !== col) return <span style={{ color: '#cbd5e1', marginLeft: 4 }}>↕</span>
    return cfOrder === 'asc' ? <span style={{ color: '#1677ff', marginLeft: 4 }}>↑</span> : <span style={{ color: '#1677ff', marginLeft: 4 }}>↓</span>
  }

  function streamUrlFor(peer: PeerFiles, path: string, selfFlag?: boolean) {
    if (!selfFlag && peer.id && peer.url) {
      // 远端 peer 节点:通过本节点代理
      return apiUrl(`/api/cluster/stream?peer=${encodeURIComponent(peer.id)}&dir=${cfDir}&path=${encodeURIComponent(path)}`)
    }
    // 本机(显式标记或与 self.id 匹配时)直接走本机接口,避免无意义 proxy
    return apiUrl(`/api/files/stream?dir=${cfDir}&path=${encodeURIComponent(path)}`)
  }
  function downloadUrlFor(peer: PeerFiles, path: string, selfFlag?: boolean) {
    if (!selfFlag && peer.id && peer.url) {
      return apiUrl(`/api/cluster/download?peer=${encodeURIComponent(peer.id)}&dir=${cfDir}&path=${encodeURIComponent(path)}`)
    }
    return apiUrl(`/api/files/download?dir=${cfDir}&path=${encodeURIComponent(path)}`)
  }

  async function deleteFile(path: string) {
    modal.confirm({
      title: `确定要删除 ${cfDir}/${path} 吗?`,
      content: '(跨节点删除需在节点本机操作,这里只删本机)',
      okText: '删除',
      okType: 'danger',
      onOk: async () => {
        try {
          const r = await api<any>('/api/files/delete', { method: 'POST', body: { path, dir: cfDir } })
          if (r.ok) {
            message.success(`已删除 (${r.size} 字节)`)
            loadFiles()
          } else message.error('删除失败: ' + r.error)
        } catch {}
      },
    })
  }

  // 节点卡片
  function NodeCard({ p, isSelf }: { p: PeerInfo; isSelf: boolean }) {
    const st = p.state || {}
    const q = st.queue || {}
    const ok = isSelf ? true : p.ok
    const run = st.run || {}
    const ff = st.ffmpeg ? String(st.ffmpeg).split('/').pop() : '—'
    return (
      <Card size="small" style={{ borderColor: ok ? '#86efac' : '#fca5a5' }} bodyStyle={{ padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
          <Space>
            <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: ok ? '#22c55e' : '#ef4444' }} />
            <Text strong>{p.name || p.id || 'unknown'}</Text>
            {isSelf && <Tag color="blue">本机</Tag>}
          </Space>
          <Text type="secondary" code>
            {p.id || ''}
          </Text>
        </div>
        <Text type="secondary" style={{ fontSize: 12, fontFamily: 'monospace' }}>
          {p.url || ''}
        </Text>
        <Row gutter={8} style={{ marginTop: 8, textAlign: 'center' }}>
          <Col span={6}><Statistic title="待处理" value={q.pending ?? '—'} valueStyle={{ color: '#1677ff', fontSize: 18 }} /></Col>
          <Col span={6}><Statistic title="运行中" value={q.running ?? '—'} valueStyle={{ color: '#f59e0b', fontSize: 18 }} /></Col>
          <Col span={6}><Statistic title="已完成" value={q.done ?? '—'} valueStyle={{ color: '#22c55e', fontSize: 18 }} /></Col>
          <Col span={6}><Statistic title="失败" value={q.failed ?? '—'} valueStyle={{ color: '#ef4444', fontSize: 18 }} /></Col>
        </Row>
        <div style={{ marginTop: 8, fontSize: 12 }}>
          <div>当前文件: <code>{run.current_file || '—'}</code></div>
          <div>ffmpeg: <code>{ff}</code></div>
          {run.started_at && <div>运行开始: {run.started_at}</div>}
        </div>
        {p.fetched_at && <Text type="secondary" style={{ fontSize: 11 }}>检测于 {p.fetched_at}</Text>}
        {p.error && <div style={{ fontSize: 11, color: '#ef4444', marginTop: 4 }}>{p.error}</div>}
      </Card>
    )
  }

  // 集群文件 peer 卡片
  function PeerFilesCard({ p, isSelf }: { p: PeerFiles; isSelf?: boolean }) {
    const name = p.name || p.id || 'unknown'
    const selfIsThis = Boolean(isSelf) || (self.id && p.id && self.id === p.id)
    if (!p.ok) {
      return (
        <Card size="small" style={{ borderColor: '#fca5a5' }}>
          <Space>
            <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: '#ef4444' }} />
            <Text strong>{name}</Text>
            {selfIsThis && <Tag color="blue">本机</Tag>}
          </Space>
          <div style={{ fontSize: 12, color: '#ef4444', marginTop: 4 }}>不可达: {p.error || ''}</div>
        </Card>
      )
    }
    const f = p.files || { items: [], count: 0, total_size_h: '0 B' }
    const items = f.items || []
    const page = f.page || 1
    const pageSize = f.page_size || items.length || 1
    const total = f.count || 0
    const totalPages = f.total_pages || 1
    const startIdx = items.length > 0 ? (page - 1) * pageSize + 1 : 0
    const endIdx = items.length > 0 ? startIdx + items.length - 1 : 0

    const columns = [
      {
        title: (
          <span style={{ cursor: 'pointer' }} onClick={() => cfSortBy('path')}>
            🎬 文件名 {sortArrow('path')}
          </span>
        ),
        dataIndex: 'path',
        ellipsis: true,
        render: (v: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>🎬 {v}</span>,
      },
      {
        title: <span style={{ cursor: 'pointer' }} onClick={() => cfSortBy('size')}>大小 {sortArrow('size')}</span>,
        dataIndex: 'size_h',
        width: 90,
        align: 'right' as const,
        render: (v: string) => <span style={{ fontSize: 12 }}>{v || ''}</span>,
      },
      {
        title: <span style={{ cursor: 'pointer' }} onClick={() => cfSortBy('mtime')}>修改时间 {sortArrow('mtime')}</span>,
        dataIndex: 'mtime',
        width: 150,
        render: (v: string) => <Text type="secondary" code>{v}</Text>,
      },
      {
        title: '操作',
        width: 120,
        align: 'right' as const,
        render: (_: any, r: FileItem) => (
          <Space size={4}>
            <a
              onClick={() =>
                setVideoModal({ open: true, url: streamUrlFor(p, r.path), title: r.path, info: name })
              }
            >
              播放
            </a>
            <a href={downloadUrlFor(p, r.path)} target="_blank" rel="noreferrer">
              下载
            </a>
            {isSelf ? (
              <Button type="link" danger size="small" icon={<DeleteOutlined />} onClick={() => deleteFile(r.path)} />
            ) : (
              <Tooltip title="跨节点删除需在节点本机操作">
                <span style={{ color: '#cbd5e1' }}>×</span>
              </Tooltip>
            )}
          </Space>
        ),
      },
    ]

    return (
      <Card
        size="small"
        title={
          <Space>
            <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: isSelf ? '#3b82f6' : '#22c55e' }} />
            <Text strong>{name}</Text>
            {isSelf && <Tag color="blue">本机</Tag>}
            <Text type="secondary" style={{ fontSize: 12, fontFamily: 'monospace' }}>{p.url || ''}</Text>
          </Space>
        }
      >
        <Table
          size="small"
          rowKey="path"
          columns={columns}
          dataSource={items}
          pagination={
            totalPages > 1
              ? {
                  current: page,
                  total,
                  pageSize,
                  showTotal: () => `${startIdx}-${endIdx} / ${total} 个 · ${f.total_size_h || '0 B'}`,
                  onChange: (pg) => setCfPage(pg),
                  showSizeChanger: false,
                }
              : false
          }
          locale={{ emptyText: <Empty description="空" /> }}
        />
      </Card>
    )
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {/* 本机身份 */}
      <Card title="本机身份" extra={<Button size="small" icon={<ReloadOutlined />} onClick={loadCluster} />}>
        <Form form={selfForm} layout="inline" onFinish={saveSelf}>
          <Form.Item name="id" label="节点 ID">
            <Input placeholder="nas1" style={{ width: 120, fontFamily: 'monospace' }} />
          </Form.Item>
          <Form.Item name="name" label="显示名">
            <Input placeholder="NAS-1" style={{ width: 140 }} />
          </Form.Item>
          <Form.Item name="url" label="URL">
            <Input placeholder="http://100.x.0.11:8765" style={{ width: 240, fontFamily: 'monospace' }} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit">
              保存身份
            </Button>
          </Form.Item>
        </Form>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {selfMsg}
        </Text>
      </Card>

      {/* Peer 管理 */}
      <Card
        title="Peer 节点列表"
        extra={
          <Space>
            <Button icon={<PlusOutlined />} onClick={addPeer}>新增</Button>
            <Button onClick={refreshCluster}>立即刷新</Button>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          其他 video-manager 实例的地址，后台每 30 秒拉取一次状态。
        </Text>
        <div style={{ marginTop: 12 }}>
          {peers.length === 0 ? (
            <Empty description="还没有 peer" />
          ) : (
            peers.map((p, i) => (
              <Row key={i} gutter={8} style={{ marginBottom: 8 }} align="middle">
                <Col>
                  <Input placeholder="id" value={p.id || ''} onChange={(e) => setPeerField(i, 'id', e.target.value)} style={{ width: 120, fontFamily: 'monospace' }} />
                </Col>
                <Col>
                  <Input placeholder="name" value={p.name || ''} onChange={(e) => setPeerField(i, 'name', e.target.value)} style={{ width: 140 }} />
                </Col>
                <Col flex="auto">
                  <Input placeholder="http://100.x.0.12:8765" value={p.url || ''} onChange={(e) => setPeerField(i, 'url', e.target.value)} style={{ fontFamily: 'monospace' }} />
                </Col>
                <Col>
                  <Button danger icon={<DeleteOutlined />} onClick={() => removePeer(i)} />
                </Col>
              </Row>
            ))
          )}
        </div>
        <Space style={{ marginTop: 8 }}>
          <Button type="primary" onClick={savePeers}>
            保存 Peers
          </Button>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {peersMsg}
          </Text>
          <Text type="secondary" style={{ fontSize: 12, marginLeft: 'auto' }}>
            上次刷新: {lastRefresh}
          </Text>
        </Space>
      </Card>

      {/* 视图切换 */}
      <Segmented
        value={view}
        onChange={(v) => setView(v as 'status' | 'files')}
        options={[
          { label: '📊 状态', value: 'status' },
          { label: '📁 文件', value: 'files' },
        ]}
      />

      {/* 节点状态卡片 */}
      {view === 'status' && (
        <Row gutter={[16, 16]}>
          <Col xs={24} md={12}>
            <NodeCard p={{ ...self, id: self.id, name: self.name, url: self.url }} isSelf />
          </Col>
          {peers.map((p, i) => (
            <Col key={i} xs={24} md={12}>
              <NodeCard p={p} isSelf={false} />
            </Col>
          ))}
        </Row>
      )}

      {/* 集群文件 */}
      {view === 'files' && (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Card size="small">
            <Space wrap>
              <span>📂</span>
              <code>video-manager://</code>
              <Segmented
                size="small"
                value={cfDir}
                onChange={(v) => {
                  setCfDir(v as string)
                  setCfPage(1)
                }}
                options={[
                  { label: 'output', value: 'output' },
                  { label: 'input', value: 'input' },
                ]}
              />
              <span style={{ marginLeft: 16 }}>每页</span>
              <select
                value={cfPageSize}
                onChange={(e) => {
                  setCfPageSize(Number(e.target.value))
                  setCfPage(1)
                }}
                style={{ border: '1px solid #d1d5db', borderRadius: 4, padding: '2px 4px' }}
              >
                <option value={20}>20</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
                <option value={0}>全部</option>
              </select>
              <Input
                placeholder="🔍 搜索文件名"
                value={cfQInput}
                onChange={(e) => {
                  setCfQInput(e.target.value)
                  clearTimeout(cfSearchTimer.current)
                  cfSearchTimer.current = setTimeout(() => {
                    setCfQ(e.target.value.trim())
                    setCfPage(1)
                  }, 300)
                }}
                style={{ width: 220 }}
                allowClear
              />
            </Space>
          </Card>
          <Spin spinning={cfLoading}>
            {cfData?.self && <div style={{ marginBottom: 12 }}><PeerFilesCard p={cfData.self} /></div>}
            {cfData?.peers?.map((p, i) => (
              <div key={i} style={{ marginBottom: 12 }}>
                <PeerFilesCard p={p} />
              </div>
            ))}
            {!cfData && <Empty />}
          </Spin>
        </Space>
      )}

      <VideoModal
        open={videoModal.open}
        streamUrl={videoModal.url}
        title={videoModal.title}
        info={videoModal.info}
        onClose={() => setVideoModal((m) => ({ ...m, open: false }))}
      />
    </Space>
  )
}
