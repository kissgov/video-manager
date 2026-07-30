// 配置页：路径设置(热加载) + 编码参数(RKMPP) + 脚本参数 + 服务重启
import { useEffect, useState, useCallback } from 'react'
import { Card, Form, Input, Button, Space, Slider, Checkbox, Tag, Divider, App, Spin, Typography, InputNumber } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { api, apiUrl } from '../api'

const { Text } = Typography

// 配置项 metadata（从原 app.js 迁移）
const CONFIG_META: Record<string, { label: string; type: string; hint?: string; options?: string[]; min?: number; max?: number; group: string; restart?: boolean }> = {
  OUTPUT_WIDTH: { label: '输出宽度', type: 'number', hint: '像素,推荐 1280', min: 320, max: 3840, group: '输出参数', restart: true },
  OUTPUT_HEIGHT: { label: '输出高度', type: 'number', hint: '像素,推荐 720', min: 240, max: 2160, group: '输出参数', restart: true },
  OUTPUT_FPS: { label: '帧率', type: 'number', hint: 'fps,推荐 10', min: 1, max: 60, group: '输出参数', restart: true },
  SOFT_CODEC: { label: '软编码器', type: 'select', options: ['libx264', 'libx265'], hint: 'libx264 快,libx265 压缩率高但慢', group: '编码参数', restart: false },
  SOFT_PRESET: { label: '编码预设', type: 'select', options: ['ultrafast', 'superfast', 'veryfast', 'fast', 'medium'], hint: '越慢压缩越好', group: '编码参数', restart: false },
  SOFT_CRF: { label: '软编码 CRF', type: 'number', hint: '质量(数字越大越糊)', min: 0, max: 51, group: '编码参数', restart: false },
  VAAPI_QP: { label: '硬编码 QP', type: 'number', hint: '硬编质量参数', min: 0, max: 51, group: '编码参数', restart: false },
  NICE_LEVEL: { label: '进程优先级', type: 'number', hint: 'nice 值,越大越不抢 CPU', min: -20, max: 19, group: '系统参数', restart: true },
  MAX_LOG_LINES: { label: '日志最大行数', type: 'number', hint: '超过自动截断', min: 100, max: 100000, group: '系统参数', restart: false },
  MIN_FILE_SIZE: { label: '最小输出字节', type: 'number', hint: '小于此值视为失败', min: 0, group: '系统参数', restart: false },
}
const CONFIG_GROUPS = [
  { name: '输出参数', desc: '输出视频的规格' },
  { name: '编码参数', desc: '软/硬编码质量与速度权衡' },
  { name: '系统参数', desc: 'CPU 优先级、日志、判定阈值' },
]

// ---- 路径设置 ----
function SettingsForm() {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [inInfo, setInInfo] = useState('—')
  const [outInfo, setOutInfo] = useState('—')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api<any>('/api/settings', { silent: true })
      form.setFieldsValue({ input_dir: r.input_dir, output_dir: r.output_dir })
      // 顺便拉磁盘占用
      try {
        const d = await api<any>('/api/disk', { silent: true })
        const iu = d.input && d.input.percent != null ? `${d.input.used_h} / ${d.input.total_h} (${d.input.percent}%)` : '—'
        const ou = d.output && d.output.percent != null ? `${d.output.used_h} / ${d.output.total_h} (${d.output.percent}%)` : '—'
        setInInfo(`${r.input_dir || '—'} · ${iu}`)
        setOutInfo(`${r.output_dir || '—'} · ${ou}`)
      } catch {
        setInInfo(r.input_dir || '—')
        setOutInfo(r.output_dir || '—')
      }
    } catch {
    } finally {
      setLoading(false)
    }
  }, [form])

  useEffect(() => {
    load()
  }, [load])

  async function save(v: any) {
    if (!v.input_dir || !v.output_dir) {
      message.error('两个路径都得填')
      return
    }
    setSaving(true)
    try {
      await api('/api/settings', { method: 'POST', body: { input_dir: v.input_dir.trim(), output_dir: v.output_dir.trim() } })
      message.success('路径设置已更新,已重新扫描输入目录')
      setTimeout(load, 500)
    } catch {
    } finally {
      setSaving(false)
    }
  }

  function resetDefault() {
    api<any>('/api/settings', { silent: true }).then((r) => {
      if (!r.defaults) return
      form.setFieldsValue({ input_dir: r.defaults.input_dir, output_dir: r.defaults.output_dir })
      message.info('已填入默认值，点保存生效')
    })
  }

  return (
    <Card
      title="路径设置"
      extra={
        <Space>
          <Button size="small" icon={<ReloadOutlined />} onClick={load} />
        </Space>
      }
    >
      <Spin spinning={loading}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          输入 / 输出目录，保存后立即生效，无需重启服务。存在 SQLite settings 表。
        </Text>
        <Form form={form} layout="vertical" onFinish={save} style={{ marginTop: 16 }}>
          <Form.Item label={<span>输入目录 <Tag color="green">热加载</Tag></span>} name="input_dir" extra={<Text type="secondary" style={{ fontSize: 12 }}>{inInfo}</Text>}>
            <Input placeholder="/volume1/Videos/.../camera_dir" style={{ fontFamily: 'monospace' }} />
          </Form.Item>
          <Form.Item label={<span>输出目录 <Tag color="green">热加载</Tag></span>} name="output_dir" extra={<Text type="secondary" style={{ fontSize: 12 }}>{outInfo}</Text>}>
            <Input placeholder="/volume1/Videos/.../compressed" style={{ fontFamily: 'monospace' }} />
          </Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={saving}>保存</Button>
            <Button onClick={resetDefault}>恢复默认</Button>
          </Space>
        </Form>
      </Spin>
    </Card>
  )
}

// ---- 编码参数 ----
function EncForm() {
  const { message } = App.useApp()
  const [qp, setQp] = useState(28)
  const [cap, setCap] = useState(4000)
  const [force, setForce] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api<any>('/api/enc-settings', { silent: true })
      setQp(r.qp ?? 28)
      setCap(r.bitrate_cap ?? 4000)
      setForce(!!r.force_recompress)
    } catch {
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function save() {
    setSaving(true)
    try {
      const r = await api<any>('/api/enc-settings', { method: 'POST', body: { qp, bitrate_cap: cap, force_recompress: force } })
      if (r.ok) message.success('编码参数已保存 · ' + r.note)
    } catch {
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card title="编码参数 (RKMPP 硬编)" extra={<Button size="small" icon={<ReloadOutlined />} onClick={load} />}>
      <Spin spinning={loading}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          存到 SQLite settings 表，热加载。不需重启服务。QP 越低质量越好文件越大；推荐 24-30。
        </Text>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, marginTop: 16 }}>
          <div>
            <div style={{ marginBottom: 4 }}>
              CQP 质量 (QP) <Text type="secondary">18-36，默认 28</Text>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <Slider min={18} max={36} step={1} value={qp} onChange={setQp} style={{ flex: 1 }} />
              <span style={{ fontFamily: 'monospace', width: 32, textAlign: 'right' }}>{qp}</span>
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>小=高质大文件, 大=低质小文件, 28 是平衡点</Text>
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>
              码率上限 (kbps) <Text type="secondary">0=不限, 默认 4000</Text>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <Slider min={0} max={10000} step={500} value={cap} onChange={setCap} style={{ flex: 1 }} />
              <span style={{ fontFamily: 'monospace', width: 48, textAlign: 'right' }}>{cap}</span>
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>防动态场景跳到 100MB+</Text>
          </div>
        </div>
        <Divider />
        <Checkbox checked={force} onChange={(e) => setForce(e.target.checked)}>
          <span style={{ fontWeight: 500 }}>⚠️ 强制重新压缩所有文件 (覆盖旧 output)</span>
        </Checkbox>
        <div style={{ fontSize: 12, color: '#64748b', marginLeft: 24 }}>
          默认关闭:output 已存在就跳过, 保留旧压缩文件。
          打开后:所有 input 都重新压缩, <b>会覆盖现有 output</b>。适合改了 QP 后要重新跑。
        </div>
        <div style={{ marginTop: 16 }}>
          <Button type="primary" onClick={save} loading={saving}>保存</Button>
        </div>
      </Spin>
    </Card>
  )
}

// ---- 脚本参数 ----
function ConfigForm() {
  const { message } = App.useApp()
  const [values, setValues] = useState<Record<string, string>>({})
  const [keys, setKeys] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api<any>('/api/config', { silent: true })
      setValues(r.config || {})
      setKeys(r.keys || [])
    } catch {
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function save() {
    setSaving(true)
    try {
      let needsRestart = false
      for (const k of keys) {
        if (CONFIG_META[k]?.restart) needsRestart = true
      }
      const r = await api<any>('/api/config', { method: 'POST', body: { config: values } })
      if (r.ok) {
        let msg = '已保存,旧版本备份为 .bak.manager'
        if (needsRestart) msg += ' · ⚠️ 部分项需重启服务才生效'
        if (needsRestart) message.info(msg)
        else message.success(msg)
      }
    } catch {
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card title="脚本参数" extra={<Button size="small" icon={<ReloadOutlined />} onClick={load} />}>
      <Spin spinning={loading}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          修改会写入 compress_video.sh，旧版本自动备份为 .bak.manager。
        </Text>
        <div style={{ marginTop: 16 }}>
          {CONFIG_GROUPS.map((grp) => {
            const grpKeys = keys.filter((k) => CONFIG_META[k]?.group === grp.name)
            if (!grpKeys.length) return null
            return (
              <div key={grp.name} style={{ marginBottom: 16 }}>
                <Divider orientation="left" style={{ marginTop: 0 }}>
                  {grp.name} <Text type="secondary" style={{ fontSize: 12 }}>{grp.desc}</Text>
                </Divider>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 16 }}>
                  {grpKeys.map((k) => {
                    const m = CONFIG_META[k] || { label: k, type: 'text' }
                    return (
                      <div key={k}>
                        <div style={{ marginBottom: 4, fontSize: 12 }}>
                          {m.label}{' '}
                          {m.restart ? <Tag color="amber">重启</Tag> : <Tag color="green">热加载</Tag>}{' '}
                          {m.hint && <Text type="secondary">{m.hint}</Text>}
                        </div>
                        {m.type === 'select' ? (
                          <select
                            value={values[k] ?? ''}
                            onChange={(e) => setValues((v) => ({ ...v, [k]: e.target.value }))}
                            style={{ width: '100%', padding: '4px 8px', border: '1px solid #d1d5db', borderRadius: 6 }}
                          >
                            {(m.options || []).map((o) => (
                              <option key={o} value={o}>
                                {o}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <InputNumber
                            value={values[k] === '' || values[k] == null ? null : Number(values[k])}
                            min={m.min}
                            max={m.max}
                            onChange={(v) => setValues((p) => ({ ...p, [k]: v == null ? '' : String(v) }))}
                            style={{ width: '100%', fontFamily: 'monospace' }}
                          />
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
        <div style={{ marginTop: 8 }}>
          <Button type="primary" onClick={save} loading={saving}>保存</Button>
        </div>
      </Spin>
    </Card>
  )
}

// ---- 重启服务 ----
function RestartButton() {
  const { message, modal } = App.useApp()
  const [restarting, setRestarting] = useState(false)

  async function restart() {
    modal.confirm({
      title: '确定要重启 video-manager 服务吗？',
      content: '连接会短暂中断（1-3 秒），然后自动恢复并刷新页面。',
      okText: '重启',
      okType: 'danger',
      onOk: async () => {
        setRestarting(true)
        message.loading('服务重启中，请稍候…', 0)
        try {
          await fetch(apiUrl('/api/service/restart'), { method: 'POST', cache: 'no-store' })
        } catch {
          /* 预期：连接被服务端断开 */
        }
        // 轮询 /api/status 最多 30s
        for (let i = 0; i < 30; i++) {
          await new Promise((r) => setTimeout(r, 1000))
          try {
            const r = await fetch(apiUrl('/api/status'), { cache: 'no-store' })
            if (r.ok) {
              const j = await r.json()
              if (j && (j.alive !== undefined || j.ffmpeg !== undefined || j.state !== undefined || j.running !== undefined)) {
                message.destroy()
                message.success('服务已恢复，刷新页面')
                setTimeout(() => location.reload(), 600)
                return
              }
            }
          } catch {}
        }
        message.destroy()
        message.error('服务重启超时，请手动刷新页面')
        setRestarting(false)
      },
    })
  }

  return (
    <Button danger loading={restarting} onClick={restart} title="重启 video-manager 服务（需 sudoers 配置）">
      重启服务
    </Button>
  )
}

export default function ConfigPage() {
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title="服务操作" size="small">
        <RestartButton />
      </Card>
      <SettingsForm />
      <EncForm />
      <ConfigForm />
    </Space>
  )
}
