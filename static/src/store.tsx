// 全局状态：用 React Context + hooks 管理全局可复用状态（status / cluster 缓存等）。
// 状态页每 2s 轮询 /api/status，结果在多个页面共享（顶部状态条、任务页、概览页）。
import React, { createContext, useContext, useEffect, useRef, useState, useCallback } from 'react'
import { api } from './api'

export interface StatusData {
  running: boolean
  pid: number | null
  run_id: number | null
  current_file: string | null
  started_at: string | null
  external: boolean
  script_pid?: number | null
  log_lines?: number
  lock_exists?: boolean
  hint?: string
}

interface StoreCtx {
  status: StatusData | null
  loading: boolean
  refresh: () => Promise<void>
  // 顶部主题切换 / 折叠侧栏等可放这里
  collapsed: boolean
  setCollapsed: (v: boolean) => void
}

const Ctx = createContext<StoreCtx>(null as any)

export function StoreProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<StatusData | null>(null)
  const [loading, setLoading] = useState(true)
  const [collapsed, setCollapsed] = useState(false)
  const statusRef = useRef<StatusData | null>(null)
  statusRef.current = status

  const refresh = useCallback(async () => {
    try {
      const st = await api<StatusData>('/api/status', { silent: true })
      setStatus(st)
      // running 但无 current_file 时补拉一次（与原逻辑一致）
      if (st.running && !st.current_file) {
        api('/api/current-file', { silent: true })
          .then((r: any) => {
            if (r.current_file) {
              setStatus((prev) => (prev ? { ...prev, current_file: r.current_file } : prev))
            }
          })
          .catch(() => {})
      }
    } catch {
      /* 静默：轮询失败不刷屏 */
    } finally {
      setLoading(false)
    }
  }, [])

  // 每 2 秒轮询一次状态（全局唯一）
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 2000)
    return () => clearInterval(t)
  }, [refresh])

  return (
    <Ctx.Provider value={{ status, loading, refresh, collapsed, setCollapsed }}>
      {children}
    </Ctx.Provider>
  )
}

export function useStore(): StoreCtx {
  return useContext(Ctx)
}
