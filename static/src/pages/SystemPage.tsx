// 系统页：系统信息(ffmpeg/路径等) + 磁盘用量
import { useEffect, useState, useCallback } from 'react'
import { Card, Descriptions, Progress, Empty, Spin, Space, Tag } from 'antd'
import { api } from '../api'

interface SysData {
  ffmpeg?: string
  ffmpeg_version?: string
  hints?: string[]
  input_dir?: string
  output_dir?: string
  script?: string
  script_log?: string
}
interface DiskItem {
  used_h?: string
  total_h?: string
  percent?: number
  used?: number
  total?: number
  free?: number
  error?: string
}

export default function SystemPage() {
  const [sys, setSys] = useState<SysData | null>(null)
  const [disk, setDisk] = useState<Record<string, DiskItem>>({})
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([api<SysData>('/api/system', { silent: true }), api('/api/disk', { silent: true })])
      setSys(s)
      setDisk(d || {})
    } catch {
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  return (
    <Spin spinning={loading}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card title="系统信息">
          {sys && (
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label="ffmpeg 路径">{sys.ffmpeg || '—'}</Descriptions.Item>
              <Descriptions.Item label="版本">{sys.ffmpeg_version || '—'}</Descriptions.Item>
              <Descriptions.Item label="输入目录">
                <code>{sys.input_dir}</code>
              </Descriptions.Item>
              <Descriptions.Item label="输出目录">
                <code>{sys.output_dir}</code>
              </Descriptions.Item>
              <Descriptions.Item label="脚本路径">
                <code>{sys.script}</code>
              </Descriptions.Item>
              <Descriptions.Item label="日志路径">
                <code>{sys.script_log}</code>
              </Descriptions.Item>
              <Descriptions.Item label="硬件提示">
                {(sys.hints || []).length ? (
                  sys.hints!.map((h, i) => (
                    <Tag key={i} color="blue">
                      {h}
                    </Tag>
                  ))
                ) : (
                  '—'
                )}
              </Descriptions.Item>
            </Descriptions>
          )}
        </Card>
        <Card title="磁盘用量">
          {Object.keys(disk).length === 0 ? (
            <Empty description="无磁盘信息" />
          ) : (
            Object.entries(disk).map(([k, v]) => {
              if (v.error)
                return (
                  <div key={k}>
                    <div style={{ fontWeight: 500 }}>{k}</div>
                    <Tag color="red">{v.error}</Tag>
                  </div>
                )
              const p = v.percent || 0
              const color = p > 90 ? '#ef4444' : p > 75 ? '#f59e0b' : '#3b82f6'
              return (
                <div key={k} style={{ marginBottom: 16 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                    <span style={{ fontWeight: 500 }}>{k}</span>
                    <span style={{ fontSize: 12, color: '#64748b' }}>
                      {v.used_h || '—'} / {v.total_h || '—'} ({p}%)
                    </span>
                  </div>
                  <Progress percent={p} strokeColor={color} showInfo={false} />
                </div>
              )
            })
          )}
        </Card>
      </Space>
    </Spin>
  )
}
