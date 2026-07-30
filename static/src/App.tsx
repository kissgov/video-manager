import { useEffect, useState } from 'react'
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { Layout, Menu, Tag, Tooltip, Button, theme } from 'antd'
import type { MenuProps } from 'antd'
import {
  DashboardOutlined,
  PlayCircleOutlined,
  FileTextOutlined,
  FolderOutlined,
  SettingOutlined,
  ClockCircleOutlined,
  ApartmentOutlined,
  VideoCameraOutlined,
  DesktopOutlined,
  UnorderedListOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { StoreProvider, useStore } from './store'
import { api } from './api'
import OverviewPage from './pages/OverviewPage'
import RunPage from './pages/RunPage'
import LogsPage from './pages/LogsPage'
import FilesPage from './pages/FilesPage'
import QueuePage from './pages/QueuePage'
import ClusterPage from './pages/ClusterPage'
import PlaybackPage from './pages/PlaybackPage'
import ConfigPage from './pages/ConfigPage'
import CronPage from './pages/CronPage'
import SystemPage from './pages/SystemPage'

const { Sider, Header, Content } = Layout

// 侧边菜单项（按原 index.html tab 顺序）
const MENU_ITEMS: MenuProps['items'] = [
  { key: '/overview', icon: <DashboardOutlined />, label: '概览' },
  { key: '/run', icon: <PlayCircleOutlined />, label: '任务' },
  { key: '/logs', icon: <FileTextOutlined />, label: '日志' },
  { key: '/files', icon: <FolderOutlined />, label: '文件' },
  { key: '/config', icon: <SettingOutlined />, label: '配置' },
  { key: '/cron', icon: <ClockCircleOutlined />, label: '定时' },
  { key: '/cluster', icon: <ApartmentOutlined />, label: '集群' },
  { key: '/playback', icon: <VideoCameraOutlined />, label: '回放' },
  { key: '/system', icon: <DesktopOutlined />, label: '系统' },
  { key: '/queue', icon: <UnorderedListOutlined />, label: '队列' },
]

function Shell() {
  const location = useLocation()
  const navigate = useNavigate()
  const { status, refresh, collapsed, setCollapsed } = useStore()
  const [ffmpeg, setFfmpeg] = useState('')
  const {
    token: { colorBgContainer },
  } = theme.useToken()

  // 顶部 ffmpeg 信息（启动时拉一次）
  useEffect(() => {
    api<any>('/api/system', { silent: true })
      .then((s) => setFfmpeg(s.ffmpeg || ''))
      .catch(() => {})
  }, [])

  const running = status?.running
  const external = status?.external

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        theme="dark"
        width={180}
        style={{ position: 'sticky', top: 0, height: '100vh', overflow: 'auto' }}
      >
        <div
          style={{
            color: '#fff',
            fontWeight: 700,
            fontSize: 16,
            padding: '16px 16px',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
          }}
        >
          📹 视频压缩管理
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={MENU_ITEMS}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: colorBgContainer,
            padding: '0 16px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderBottom: '1px solid #f0f0f0',
          }}
        >
          <Button
            type="text"
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            onClick={() => setCollapsed(!collapsed)}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            {ffmpeg && (
              <Tooltip title="ffmpeg 路径">
                <span style={{ fontFamily: 'monospace', fontSize: 12, color: '#94a3b8' }}>
                  ffmpeg: {ffmpeg}
                </span>
              </Tooltip>
            )}
            {running ? (
              <Tag color="success" style={{ margin: 0 }}>● 运行中</Tag>
            ) : (
              <Tag color="default" style={{ margin: 0 }}>○ 空闲</Tag>
            )}
            {external && <Tag color="warning">外部任务</Tag>}
            <Tooltip title="刷新状态">
              <Button type="text" icon={<ReloadOutlined />} onClick={() => refresh()} />
            </Tooltip>
          </div>
        </Header>
        <Content style={{ margin: 16 }}>
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<OverviewPage />} />
            <Route path="/run" element={<RunPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/files" element={<FilesPage />} />
            <Route path="/config" element={<ConfigPage />} />
            <Route path="/cron" element={<CronPage />} />
            <Route path="/cluster" element={<ClusterPage />} />
            <Route path="/playback" element={<PlaybackPage />} />
            <Route path="/system" element={<SystemPage />} />
            <Route path="/queue" element={<QueuePage />} />
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  )
}

export default function App() {
  return (
    <StoreProvider>
      <Shell />
    </StoreProvider>
  )
}
