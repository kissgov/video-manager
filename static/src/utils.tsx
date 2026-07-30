// 通用格式化工具（从原 app.js 迁移）

// 字节格式化：1.2M / 3.4G
export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  const u = ['B', 'K', 'M', 'G', 'T']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return v.toFixed(1) + u[i]
}

// 人类可读大小（带单位空格），用于文件列表
export function humanSize(n: number | null | undefined): string {
  if (!n) return '0 B'
  let v = n
  for (const u of ['B', 'K', 'M', 'G', 'T']) {
    if (v < 1024) return (v < 10 ? v.toFixed(1) : Math.round(v)) + ' ' + u
    v /= 1024
  }
  return v.toFixed(1) + ' T'
}

export function fmtDate(s: string | null | undefined): string {
  if (!s) return '—'
  return s
}

// 秒数 -> 时长
export function formatDurationSec(sec: number | null | undefined): string {
  if (sec == null) return '—'
  if (sec < 60) return Math.round(sec) + 's'
  if (sec < 3600) return Math.floor(sec / 60) + 'm ' + Math.round(sec % 60) + 's'
  return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm'
}

// 简洁时长（不带秒小数）
export function formatDurationPlain(sec: number | null | undefined): string {
  if (sec == null) return '—'
  if (sec < 60) return Math.round(sec) + 's'
  if (sec < 3600) return Math.floor(sec / 60) + 'm'
  return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm'
}

// 两个时间字符串(YYYY-MM-DD HH:MM:SS)之间耗时
export function formatDuration(start: string, end: string): string {
  try {
    const a = new Date(start.replace(' ', 'T'))
    const b = new Date(end.replace(' ', 'T'))
    const sec = Math.round((b.getTime() - a.getTime()) / 1000)
    if (sec < 0) return '—'
    if (sec < 60) return sec + 's'
    if (sec < 3600) return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's'
    return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm'
  } catch {
    return '—'
  }
}

// 日志级别分类（与原 app.js 一致）
export type LogLevel = 'error' | 'warn' | 'ok' | 'info'
export function classifyLogLevel(line: string): LogLevel {
  if (!line) return 'info'
  if (/失败|错误|error|exit=|fatal|Exception/i.test(line)) return 'error'
  if (/警告|warn|超时/i.test(line)) return 'warn'
  if (/完成|成功|已启动|已停止|启动:/i.test(line)) return 'ok'
  return 'info'
}

export const LOG_LEVEL_COLOR: Record<LogLevel, string> = {
  error: '#f87171',
  warn: '#fcd34d',
  ok: '#4ade80',
  info: '#e2e8f0',
}

// 回放文件名解析：00_20260714131547_20260714132639.mp4
export interface PbMeta {
  start: string
  end: string
  dur: number
  date: string
}
export function pbParseName(name: string): PbMeta | null {
  const m = name.match(/(\d{8})(\d{6})_(\d{8})(\d{6})/)
  if (!m) return null
  const startStr = `${m[1]}${m[2]}`
  const endStr = `${m[3]}${m[4]}`
  const fmt = (s: string) =>
    `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)} ${s.slice(8, 10)}:${s.slice(10, 12)}:${s.slice(12, 14)}`
  const start = fmt(startStr)
  const end = fmt(endStr)
  const dur =
    (new Date(end.replace(/-/g, '/')).getTime() - new Date(start.replace(/-/g, '/')).getTime()) /
    1000
  return { start, end, dur, date: m[1] }
}

export function pbFmtDur(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`
}

// 把关键字高亮成 React 节点数组
import React from 'react'
export function highlight(text: string, q: string): React.ReactNode {
  if (!q) return text
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const re = new RegExp(escaped, 'gi')
  const out: React.ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text)) !== null) {
    out.push(text.slice(last, m.index))
    out.push(
      <mark key={i++} style={{ background: '#fde047', color: '#000' }}>
        {m[0]}
      </mark>
    )
    last = m.index + m[0].length
    if (m.index === re.lastIndex) re.lastIndex++
  }
  out.push(text.slice(last))
  return out
}
