import React from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import { ConfigProvider, App as AntApp } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import App from './App'
import './index.css'

dayjs.locale('zh-cn')

// AntD 主题：干净的主色 + 圆角
const theme = {
  token: {
    colorPrimary: '#1677ff',
    borderRadius: 6,
  },
  components: {
    Layout: {
      siderBg: '#001529',
      headerBg: '#fff',
    },
  },
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <ConfigProvider locale={zhCN} theme={theme}>
    <AntApp>
      <HashRouter>
        <App />
      </HashRouter>
    </AntApp>
  </ConfigProvider>
)
