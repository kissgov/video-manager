// 任务页：手动启动 / 停止压缩任务
import { useState } from 'react'
import { Card, Button, Space, Alert, Descriptions, App } from 'antd'
import { PlayCircleOutlined, PauseCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import { api } from '../api'
import { useStore } from '../store'

export default function RunPage() {
  const { status, refresh } = useStore()
  const { message, modal } = App.useApp()
  const [busy, setBusy] = useState(false)

  const running = !!status?.running
  const external = !!status?.external

  async function runTask() {
    modal.confirm({
      title: '确定开始压缩？',
      content: '脚本会处理输入目录下所有 mp4，成功后会删除原始文件。',
      okText: '开始',
      cancelText: '取消',
      onOk: async () => {
        setBusy(true)
        try {
          const r = await api<any>('/api/run', { body: { trigger: 'manual' } })
          if (r.ok) message.success(r.message)
          else message.error(r.message)
          refresh()
        } catch {
          /* api 已提示 */
        } finally {
          setBusy(false)
        }
      },
    })
  }

  async function stopTask() {
    let content = '确定停止当前任务？\n会发 SIGTERM，2 秒后 SIGKILL。'
    if (external) {
      content = `检测到这是从终端启动的外部任务。\n停止只会杀死：compress_video.sh (pid ${status?.script_pid}) 及其 ffmpeg 子进程，不会影响你的终端 shell、其它进程或未压缩的文件。`
    }
    modal.confirm({
      title: '确定停止任务？',
      content,
      okText: '停止',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        setBusy(true)
        try {
          const r = await api<any>('/api/stop', { method: 'POST' })
          if (r.ok) message.success(r.message)
          else message.error(r.message)
          refresh()
        } catch {
          /* */
        } finally {
          setBusy(false)
        }
      },
    })
  }

  let hint = '空闲。点击「开始压缩」将启动后台任务。'
  let hintType: 'info' | 'warning' = 'info'
  if (running && external) {
    hint = `⚠️ 检测到外部任务在运行(script_pid=${status?.script_pid})，只能停止，不能同时启动另一个。点击停止只会杀压缩脚本及其 ffmpeg 子进程，不会影响你的终端 shell。`
    hintType = 'warning'
  } else if (running) {
    hint = '任务运行中。'
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title="手动运行">
        <Space wrap>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            onClick={runTask}
            disabled={running || busy}
            loading={busy && !running}
          >
            开始压缩
          </Button>
          <Button
            danger
            icon={<PauseCircleOutlined />}
            onClick={stopTask}
            disabled={!running || busy}
          >
            停止
          </Button>
          <Button icon={<ReloadOutlined />} onClick={refresh}>
            刷新
          </Button>
        </Space>
        <Descriptions column={1} size="small" style={{ marginTop: 16 }} bordered>
          <Descriptions.Item label="PID">{status?.pid || '—'}</Descriptions.Item>
          <Descriptions.Item label="启动时间">{status?.started_at || '—'}</Descriptions.Item>
          <Descriptions.Item label="当前文件">
            <span style={{ fontFamily: 'monospace', fontSize: 13, wordBreak: 'break-all' }}>
              {status?.current_file || '—'}
            </span>
          </Descriptions.Item>
        </Descriptions>
        {hint && <Alert style={{ marginTop: 16 }} type={hintType} message={hint} showIcon />}
      </Card>
      <Alert
        type="warning"
        message="⚠️ 提示"
        description="脚本使用 flock 互斥锁，如已有任务在跑，本次点击会被脚本自动跳过。压缩过程中会删除原始监控录像文件（只在输出大于 1MB 时）。"
        showIcon
      />
    </Space>
  )
}
