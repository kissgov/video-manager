// 文件页：输入 / 输出目录浏览，点击视频可播放（/api/files/stream 支持 Range）
import { useEffect, useState, useCallback } from 'react'
import { Card, Table, Input, Button, Space, Empty, Spin, Tag, Typography, Col, Row } from 'antd'
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { api, apiUrl } from '../api'
import { fmtBytes } from '../utils'
import VideoModal from '../components/VideoModal'

const { Text } = Typography

interface FileItem {
  path: string
  size: number
  size_h?: string
  mtime: string
}
interface FilesResult {
  exists?: boolean
  error?: string
  count?: number
  total_size_h?: string
  items?: FileItem[]
}

function DirCard({ dir, title, icon }: { dir: string; title: string; icon: string }) {
  const [data, setData] = useState<FilesResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [qInput, setQInput] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api<{ files: FilesResult }>(`/api/files/${dir}`, { silent: true })
      setData(r.files || {})
    } catch {
      /* */
    } finally {
      setLoading(false)
    }
  }, [dir])

  useEffect(() => {
    load()
  }, [load])

  const items = (data?.items || []).filter((it) => (q ? (it.path || '').toLowerCase().includes(q.toLowerCase()) : true))

  const [modal, setModal] = useState<{ open: boolean; url: string; title: string }>({
    open: false,
    url: '',
    title: '',
  })

  const columns = [
    {
      title: '路径',
      dataIndex: 'path',
      ellipsis: true,
      render: (v: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{v}</span>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      width: 90,
      align: 'right' as const,
      render: (v: number, r: FileItem) => (r.size_h || fmtBytes(v)),
    },
    { title: '修改时间', dataIndex: 'mtime', width: 150, render: (v: string) => <Text type="secondary" code>{v}</Text> },
    {
      title: '操作',
      width: 80,
      render: (_: any, r: FileItem) => (
        <a
          onClick={() =>
            setModal({
              open: true,
              url: apiUrl(`/api/files/stream?dir=${dir}&path=${encodeURIComponent(r.path)}`),
              title: r.path,
            })
          }
        >
          播放
        </a>
      ),
    },
  ]

  return (
    <Card
      title={`${icon} ${title}`}
      extra={
        <Space>
          <Input
            size="small"
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索文件名"
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            onPressEnter={() => setQ(qInput.trim())}
            style={{ width: 160 }}
          />
          <Button size="small" onClick={() => setQ(qInput.trim())}>
            搜索
          </Button>
          <Button size="small" icon={<ReloadOutlined />} onClick={load} />
        </Space>
      }
    >
      <Spin spinning={loading}>
        {!data?.exists ? (
          <Empty description="目录不存在" />
        ) : data?.error ? (
          <Text type="danger">读取失败: {data.error}</Text>
        ) : (
          <>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
              共 {data?.count ?? items.length} 个文件 · 总大小 {data?.total_size_h || '—'}
              {q ? ` · 搜索 "${q}"` : ''}
            </Text>
            <Table
              size="small"
              rowKey="path"
              columns={columns}
              dataSource={items}
              pagination={{ pageSize: 50, size: 'small', showSizeChanger: false }}
              scroll={{ y: 460 }}
              locale={{ emptyText: <Empty description="空目录" /> }}
            />
          </>
        )}
      </Spin>
      <VideoModal
        open={modal.open}
        streamUrl={modal.url}
        title={modal.title}
        onClose={() => setModal((m) => ({ ...m, open: false }))}
      />
    </Card>
  )
}

export default function FilesPage() {
  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} lg={12}>
        <DirCard dir="input" title="输入目录" icon="📥" />
      </Col>
      <Col xs={24} lg={12}>
        <DirCard dir="output" title="输出目录" icon="📤" />
      </Col>
    </Row>
  )
}
