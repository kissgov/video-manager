// 定时页：ofelia 容器定时任务 CRUD + UI 调度(Python 后台) + 最近触发记录
import { useEffect, useState, useCallback } from 'react'
import {
  Card, Button, Space, Input, Tag, Table, Statistic, Row, Col, Modal, Form, Switch,
  Checkbox, Empty, Spin, Typography, App, Divider,
} from 'antd'
import { PlusOutlined, ReloadOutlined, ThunderboltOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons'
import { api } from '../api'
import { formatDuration } from '../utils'

const { Text } = Typography

interface CronJob {
  section?: string
  name?: string
  schedule?: string
  container?: string
  command?: string
  next_run?: string
}
interface Schedule {
  id: string
  name: string
  cron_expr: string
  enabled: boolean
  last_run?: string
  last_status?: string
  next_run?: string
  trigger_payload?: any
}
interface RunRecord {
  id: number
  started_at?: string
  ended_at?: string
  trigger?: string
  success?: number
  failed?: number
  skipped?: number
  total?: number
}

const OFELIA_STATE_MAP: Record<string, { color: string; text: string }> = {
  running: { color: 'green', text: '🟢 运行中' },
  exited: { color: 'default', text: '⚪ 已退出' },
  absent: { color: 'amber', text: '❓ 未发现' },
  docker_unavailable: { color: 'red', text: '⚠️ docker 不可用' },
}

export default function CronPage() {
  const { message, modal } = App.useApp()
  const [jobs, setJobs] = useState<CronJob[]>([])
  const [iniPath, setIniPath] = useState('—')
  const [ofeliaState, setOfeliaState] = useState('unknown')
  const [nextRuns, setNextRuns] = useState<CronJob[]>([])
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [recent, setRecent] = useState<RunRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [savingJobs, setSavingJobs] = useState(false)

  // schedule 编辑弹窗
  const [schedOpen, setSchedOpen] = useState(false)
  const [schedForm] = Form.useForm()
  const [editingId, setEditingId] = useState<string | undefined>()
  const [preview, setPreview] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [cronR, statusR, statsR, schedR] = await Promise.all([
        api<any>('/api/cron', { silent: true }),
        api<any>('/api/cron/status', { silent: true }),
        api<any>('/api/stats', { silent: true }),
        api<any>('/api/schedules', { silent: true }),
      ])
      setJobs(cronR.jobs || [])
      setIniPath(cronR.ini_path || '—')
      setOfeliaState(statusR.state)
      setNextRuns((cronR.jobs || []).filter((j: CronJob) => j.next_run && j.next_run !== 'invalid' && j.next_run !== ''))
      setRecent(statsR.recent || [])
      setSchedules(schedR.schedules || [])
    } catch {
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // ---- ofelia jobs ----
  function addJob() {
    setJobs((j) => [
      ...j,
      { section: 'job-exec "new-task"', name: 'new-task', schedule: '0 3 * * *', container: 'ffmpeg-worker', command: 'bash /scripts/compress_video.sh' },
    ])
  }
  function delJob(i: number) {
    setJobs((j) => j.filter((_, idx) => idx !== i))
  }
  function setJobField(i: number, k: keyof CronJob, v: string) {
    setJobs((j) => j.map((job, idx) => (idx === i ? { ...job, [k]: v } : job)))
  }
  async function saveJobs() {
    setSavingJobs(true)
    try {
      const r = await api<any>('/api/cron', { method: 'POST', body: { jobs } })
      if (r.ok) {
        setJobs(r.jobs || [])
        message.success('已保存,记得点 "重启 ofelia" 让配置生效')
      }
    } catch {
    } finally {
      setSavingJobs(false)
    }
  }
  async function restartOfelia() {
    modal.confirm({
      title: '重启 ofelia 容器？',
      content: '(需要 docker 权限，失败时会给出手动命令)',
      okText: '重启',
      onOk: async () => {
        try {
          const r = await api<any>('/api/cron/restart', { method: 'POST' })
          if (r.ok) message.success(r.message)
          else message.error(r.message)
        } catch {}
      },
    })
  }
  async function runNow() {
    modal.confirm({
      title: '立即触发一次压缩任务?',
      okText: '触发',
      onOk: async () => {
        try {
          const r = await api<any>('/api/run', { method: 'POST', body: { trigger: 'manual' } })
          if (r.ok) message.success(r.message)
          else message.error(r.message)
          setTimeout(load, 2000)
        } catch {}
      },
    })
  }

  // ---- schedules ----
  function openAddSched() {
    setEditingId(undefined)
    schedForm.resetFields()
    schedForm.setFieldsValue({ enabled: true })
    setPreview('')
    setSchedOpen(true)
  }
  function openEditSched(s: Schedule) {
    setEditingId(s.id)
    schedForm.setFieldsValue({ name: s.name, cron_expr: s.cron_expr, enabled: s.enabled })
    setPreview('')
    setSchedOpen(true)
    doPreview(s.cron_expr)
  }
  async function doPreview(expr?: string) {
    const e = (expr ?? schedForm.getFieldValue('cron_expr') ?? '').trim()
    if (!e) return
    try {
      const r = await api<any>('/api/schedules/preview', { method: 'POST', body: { cron_expr: e } })
      if (r.ok) setPreview('下次运行: ' + r.runs.join(' / '))
      else setPreview('❌ ' + r.error)
    } catch (e: any) {
      setPreview('❌ ' + e.message)
    }
  }
  async function saveSched() {
    try {
      const v = await schedForm.validateFields()
      if (!v.name || !v.cron_expr) {
        message.error('名称和 cron 表达式都要填')
        return
      }
      const r = await api<any>('/api/schedules/upsert', {
        method: 'POST',
        body: { id: editingId, name: v.name, cron_expr: v.cron_expr, enabled: !!v.enabled, trigger_payload: { trigger: 'schedule' } },
      })
      if (r.ok) {
        message.success('已保存')
        setSchedOpen(false)
        load()
      } else message.error(r.error || '保存失败')
    } catch {}
  }
  async function delSched(id: string) {
    modal.confirm({
      title: '确定要删除这个调度吗?',
      okText: '删除',
      okType: 'danger',
      onOk: async () => {
        try {
          await api('/api/schedules/delete', { method: 'POST', body: { id } })
          message.success('已删除')
          load()
        } catch {}
      },
    })
  }
  async function fireSched(id: string) {
    try {
      const r = await api<any>('/api/schedules/fire', { method: 'POST', body: { id } })
      if (r.ok) message.success('已触发: ' + r.message)
      else message.error('失败: ' + r.message)
    } catch {}
  }
  async function toggleSched(s: Schedule, enabled: boolean) {
    try {
      await api('/api/schedules/upsert', {
        method: 'POST',
        body: { id: s.id, name: s.name, cron_expr: s.cron_expr, enabled, trigger_payload: s.trigger_payload || { trigger: 'schedule' } },
      })
      message.success(enabled ? '已启用' : '已禁用')
      load()
    } catch {}
  }

  const historyCols = [
    { title: '#', dataIndex: 'id', width: 60 },
    { title: '开始', dataIndex: 'started_at', render: (v: string) => <Text code>{v || '—'}</Text> },
    { title: '结束', dataIndex: 'ended_at', render: (v: string) => <Text code>{v || '—'}</Text> },
    {
      title: '触发',
      dataIndex: 'trigger',
      render: (v: string) => {
        const color = v === 'manual' ? 'blue' : v === 'cron' ? 'amber' : 'default'
        return <Tag color={color}>{v || '-'}</Tag>
      },
    },
    { title: '成功', dataIndex: 'success', align: 'right' as const, render: (v: number) => <Text type="success">{v || 0}</Text> },
    { title: '失败', dataIndex: 'failed', align: 'right' as const, render: (v: number) => <Text type="danger">{v || 0}</Text> },
    { title: '总数', dataIndex: 'total', align: 'right' as const },
    {
      title: '耗时',
      render: (_: any, r: RunRecord) => (r.started_at && r.ended_at ? formatDuration(r.started_at, r.ended_at) : r.ended_at ? '—' : '进行中…'),
    },
  ]

  const ofMap = OFELIA_STATE_MAP[ofeliaState] || { color: 'default', text: ofeliaState }

  return (
    <Spin spinning={loading}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {/* 状态 + 快捷 */}
        <Row gutter={[16, 16]}>
          <Col xs={24} md={8}>
            <Card>
              <div style={{ fontSize: 12, color: '#64748b', marginBottom: 4 }}>ofelia 容器</div>
              <Tag color={ofMap.color} style={{ fontSize: 16, padding: '4px 12px' }}>{ofMap.text}</Tag>
              <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>调度任务运行状态</div>
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card>
              <div style={{ fontSize: 12, color: '#64748b', marginBottom: 8 }}>手动触发</div>
              <Button type="primary" icon={<ThunderboltOutlined />} onClick={runNow}>
                立即运行压缩
              </Button>
              <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>直接调起 Python worker 跑</div>
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card title="下次运行" bodyStyle={{ paddingTop: 12 }}>
              {nextRuns.length === 0 ? (
                <Text type="secondary">无有效调度</Text>
              ) : (
                nextRuns.slice(0, 5).map((j, i) => (
                  <div key={i}>
                    <Text code>{j.name || j.section || '(unnamed)'}</Text>: <Text type="success">{j.next_run}</Text>
                  </div>
                ))
              )}
            </Card>
          </Col>
        </Row>

        {/* ofelia 任务 */}
        <Card
          title="定时任务（ofelia）"
          extra={
            <Space>
              <Button icon={<PlusOutlined />} onClick={addJob}>新增</Button>
              <Button onClick={restartOfelia}>↻ 重启 ofelia</Button>
            </Space>
          }
        >
          <Text type="secondary" style={{ fontSize: 12 }}>
            配置文件: <code>{iniPath}</code>。保存后会提示重启 ofelia 容器。
          </Text>
          <div style={{ marginTop: 12 }}>
            {jobs.length === 0 ? (
              <Empty description={'暂无定时任务,点 "新增" 添加一个'} />
            ) : (
              jobs.map((j, i) => (
                <div key={i} style={{ background: '#f8fafc', borderRadius: 8, padding: 16, border: '1px solid #e2e8f0', marginBottom: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                    <Text strong>📅 任务 #{i + 1}: <code>{j.name || j.section}</code></Text>
                    <Button type="link" danger size="small" icon={<DeleteOutlined />} onClick={() => delJob(i)}>删除</Button>
                  </div>
                  <Row gutter={12}>
                    <Col xs={24} md={12}>
                      <Text type="secondary" style={{ fontSize: 12 }}>名称</Text>
                      <Input size="small" value={j.name || ''} onChange={(e) => setJobField(i, 'name', e.target.value)} style={{ fontFamily: 'monospace' }} />
                    </Col>
                    <Col xs={24} md={12}>
                      <Text type="secondary" style={{ fontSize: 12 }}>容器</Text>
                      <Input size="small" value={j.container || ''} onChange={(e) => setJobField(i, 'container', e.target.value)} style={{ fontFamily: 'monospace' }} />
                    </Col>
                    <Col xs={24} style={{ marginTop: 8 }}>
                      <Text type="secondary" style={{ fontSize: 12 }}>调度表达式（cron 5 字段）</Text>
                      <Input size="small" value={j.schedule || ''} onChange={(e) => setJobField(i, 'schedule', e.target.value)} style={{ fontFamily: 'monospace' }} />
                      <Text type="secondary" style={{ fontSize: 12 }}>下次运行: <code>{j.next_run || '—'}</code></Text>
                    </Col>
                    <Col xs={24} style={{ marginTop: 8 }}>
                      <Text type="secondary" style={{ fontSize: 12 }}>命令</Text>
                      <Input size="small" value={j.command || ''} onChange={(e) => setJobField(i, 'command', e.target.value)} style={{ fontFamily: 'monospace' }} />
                    </Col>
                  </Row>
                </div>
              ))
            )}
          </div>
          <Button type="primary" onClick={saveJobs} loading={savingJobs} style={{ marginTop: 8 }}>保存</Button>
        </Card>

        {/* UI 调度 */}
        <Card
          title="UI 调度"
          extra={
            <Space>
              <Button icon={<PlusOutlined />} onClick={openAddSched}>新增调度</Button>
              <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
            </Space>
          }
        >
          <Text type="secondary" style={{ fontSize: 12 }}>
            Python 后台调度器（每 30 秒检查），独立于 ofelia。不需 docker，直接调起 video-manager 的 Python worker。
          </Text>
          <div style={{ marginTop: 12 }}>
            {schedules.length === 0 ? (
              <Empty description={'还没有调度，点"新增调度"添加'} />
            ) : (
              schedules.map((s) => (
                <div
                  key={s.id}
                  style={{ display: 'flex', alignItems: 'center', gap: 12, padding: 12, background: '#f8fafc', borderRadius: 8, border: '1px solid #e2e8f0', marginBottom: 8 }}
                >
                  <Switch checked={s.enabled} onChange={(v) => toggleSched(s, v)} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 500 }}>{s.name}</div>
                    <Text type="secondary" code>
                      {s.cron_expr}
                    </Text>
                  </div>
                  <div style={{ fontSize: 12, color: '#64748b', textAlign: 'right' }}>
                    <div>下次: <code>{s.next_run || '—'}</code></div>
                    <div>
                      上次: <code>{s.last_run || '—'}</code>{' '}
                      {s.last_status ? (s.last_status === 'fired' ? <Text type="success">✓ {s.last_status}</Text> : <Text type="warning">⚠ {s.last_status.slice(0, 40)}</Text>) : <Text type="secondary">未运行</Text>}
                    </div>
                  </div>
                  <Space>
                    <Button size="small" type="primary" onClick={() => fireSched(s.id)}>▶</Button>
                    <Button size="small" icon={<EditOutlined />} onClick={() => openEditSched(s)} />
                    <Button size="small" danger icon={<DeleteOutlined />} onClick={() => delSched(s.id)} />
                  </Space>
                </div>
              ))
            )}
          </div>
        </Card>

        {/* 运行历史 */}
        <Card title="最近触发记录" extra={<a onClick={load}>刷新</a>}>
          <Table size="small" rowKey="id" columns={historyCols} dataSource={recent} pagination={{ pageSize: 10, size: 'small' }} scroll={{ x: 700 }} />
        </Card>
      </Space>

      <Modal
        open={schedOpen}
        title={editingId ? '编辑调度' : '新增调度'}
        onCancel={() => setSchedOpen(false)}
        onOk={saveSched}
        okText="保存"
        cancelText="取消"
      >
        <Form form={schedForm} layout="vertical">
          <Form.Item label="名称" name="name" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="每日凌晨 2 点压缩" />
          </Form.Item>
          <Form.Item label="Cron 表达式（5 个字段: 分 时 日 月 周）" name="cron_expr" rules={[{ required: true, message: '请输入 cron 表达式' }]}>
            <Input placeholder="0 2 * * *" style={{ fontFamily: 'monospace' }} />
          </Form.Item>
          <Form.Item name="enabled" valuePropName="checked">
            <Checkbox>启用</Checkbox>
          </Form.Item>
          <Space>
            <Button onClick={() => doPreview()}>预览</Button>
            <Text type="secondary">{preview}</Text>
          </Space>
          <Divider style={{ margin: '12px 0' }} />
        </Form>
      </Modal>
    </Spin>
  )
}
