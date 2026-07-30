// API 客户端：统一封装 fetch，处理 JSON / 错误 / 子路径前缀。
// 后端 API 前缀 /api/，同源服务，baseURL 用相对路径。
import { message } from 'antd'

// 反向代理兼容：在子路径(如 /nas1/)部署时，fetch('/api/...') 会错位，
// 这里把 '/api/...' 自动加上当前页面的目录前缀。
function basePath(): string {
  const p = location.pathname
  return p.substring(0, p.lastIndexOf('/') + 1) || '/'
}

// 给 '/api/...' 之类的绝对路径加上子路径前缀，返回完整 URL 字符串。
// 用于 <img src> / <video src> / EventSource 等。
export function apiUrl(path: string): string {
  if (typeof path !== 'string' || path.charAt(0) !== '/') return path
  const b = basePath()
  if (b === '/') return path
  return b + path.slice(1)
}

export interface ApiOptions {
  method?: string
  body?: any
  signal?: AbortSignal
  // 不抛错，把 {ok:false,error} 之类原样返回（调用方自己判断）
  silent?: boolean
}

// 通用 API 调用：返回 JSON。失败抛 Error（并在 message 提示，除非 silent）。
export async function api<T = any>(path: string, opts: ApiOptions = {}): Promise<T> {
  const hasBody = opts.body !== undefined && opts.body !== null
  const method = (opts.method || (hasBody ? 'POST' : 'GET')).toUpperCase()
  const init: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: hasBody ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  }
  let r: Response
  try {
    r = await fetch(apiUrl(path), init)
  } catch (e: any) {
    if (!opts.silent) message.error('网络错误: ' + (e?.message || e))
    throw e
  }
  if (!r.ok) {
    const t = await r.text().catch(() => '')
    if (!opts.silent) message.error(`HTTP ${r.status}: ${t.slice(0, 200)}`)
    throw new Error(`HTTP ${r.status}: ${t}`)
  }
  return r.json()
}

// GET 请求 helper（带 query string 对象）
export async function apiGet<T = any>(path: string, params?: Record<string, any>, opts: ApiOptions = {}): Promise<T> {
  let url = path
  if (params) {
    const sp = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === '') continue
      sp.set(k, String(v))
    }
    const qs = sp.toString()
    if (qs) url += (path.includes('?') ? '&' : '?') + qs
  }
  return api<T>(url, { ...opts, method: 'GET' })
}
