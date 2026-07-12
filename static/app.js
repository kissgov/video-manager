// 视频压缩任务管理 - 前端逻辑

const $  = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));

// ---- 通用工具 ----
function toast(msg, kind='info') {
  const el = $('#toast');
  el.textContent = msg;
  el.style.background = kind === 'error' ? '#dc2626' : kind === 'ok' ? '#16a34a' : '#0f172a';
  el.style.opacity = '1';
  clearTimeout(el._t);
  el._t = setTimeout(() => el.style.opacity = '0', 3000);
}

async function api(path, opts={}) {
  const hasBody = opts.body !== undefined && opts.body !== null;
  const method = (opts.method || (hasBody ? 'POST' : 'GET')).toUpperCase();
  const init = {
    method,
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: hasBody ? JSON.stringify(opts.body) : undefined,
  };
  const r = await fetch(path, init);
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    throw new Error(`HTTP ${r.status}: ${t}`);
  }
  return r.json();
}

function fmtBytes(n) {
  if (n == null) return '—';
  const u = ['B','K','M','G','T'];
  let i = 0; let v = n;
  while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
  return v.toFixed(1) + u[i];
}

function fmtDate(s) {
  if (!s) return '—';
  return s;
}

function formatDuration(sec) {
  if (sec == null) return '—';
  if (sec < 60)    return sec + 's';
  if (sec < 3600)  return Math.floor(sec/60) + 'm ' + (sec%60) + 's';
  return Math.floor(sec/3600) + 'h ' + Math.floor((sec%3600)/60) + 'm';
}

// ---- Tabs ----
function showTab(name) {
  $$('.tab-btn').forEach(b => b.classList.toggle('border-blue-600', b.dataset.tab === name));
  $$('.tab-btn').forEach(b => b.classList.toggle('text-blue-600', b.dataset.tab === name));
  $$('[data-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.panel !== name));
  if (name === 'overview') loadOverview();
  if (name === 'run')      loadRun();
  if (name === 'logs')     loadLogs();
  if (name === 'files')    loadFiles();
  if (name === 'config')   loadConfig();
  if (name === 'cron')     loadCron();
  if (name === 'system')   loadSystem();
  if (name === 'queue')    loadQueue();
}
$$('.tab-btn').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));

// ---- 状态 / 概览 ----
let logSince = 0;
async function refreshStatus() {
  try {
    const st = await api('/api/status');
    setStatusPill(st.running);
    $('#ov-state').textContent   = st.running ? '🟢 运行中' : '⚪ 空闲';
    $('#ov-state').className     = 'text-2xl font-bold ' + (st.running ? 'text-green-600' : 'text-slate-500');
    $('#ov-pid').textContent     = st.pid ? `pid=${st.pid}${st.external?' (外部)':''}` : '';
    $('#ov-current').textContent = st.current_file || '—';
    $('#ov-started').textContent = st.started_at ? `开始于 ${st.started_at}` : '';
    $('#run-pid').textContent    = st.pid || '—';
    $('#run-started').textContent= st.started_at || '—';
    $('#run-current').textContent= st.current_file || '—';
    $('#btn-run').disabled  = !!st.running;
    $('#btn-stop').disabled = !st.running;
    // 提示文本
    let hint = '';
    if (st.running && st.external) {
      hint = `⚠️ 检测到外部任务在运行(script_pid=${st.script_pid}),只能停止,不能同时启动另一个。点击停止只会杀压缩脚本及其 ffmpeg 子进程,不会影响你的终端 shell。`;
    } else if (st.running) {
      hint = '任务运行中。';
    } else {
      hint = '空闲。点击「开始压缩」将启动后台任务。';
    }
    $('#run-hint').textContent = hint;
    $('#run-hint').className   = 'text-sm ' + (st.running && st.external ? 'text-amber-700' : 'text-slate-500');

    // 同步 current_file
    if (st.running && !st.current_file) {
      api('/api/current-file').then(r => {
        if (r.current_file) $('#ov-current').textContent = $('#run-current').textContent = r.current_file;
      }).catch(()=>{});
    }
  } catch (e) { console.error(e); }
}

function setStatusPill(running) {
  const el = $('#status-pill');
  if (running) {
    el.textContent = '● 运行中';
    el.className = 'px-3 py-1.5 rounded-full text-sm font-medium bg-green-100 text-green-700';
  } else {
    el.textContent = '○ 空闲';
    el.className = 'px-3 py-1.5 rounded-full text-sm font-medium bg-slate-200 text-slate-600';
  }
}

async function loadOverview() {
  await refreshStatus();
  try {
    const [s, qs, ds, fr] = await Promise.all([
      api('/api/stats'),
      api('/api/queue/stats'),
      api('/api/disk'),
      api('/api/queue?status=failed&limit=5&sort_by=id&sort_dir=desc'),
    ]);
    // 顶部 4 卡
    const t = s.today || {}, a = s.total || {};
    $('#ov-today-runs').textContent  = t.runs || 0;
    $('#ov-today-files').textContent = t.total || 0;
    $('#ov-today-detail').textContent = `成功 ${t.success||0} · 跳过 ${t.skipped||0} · 失败 ${t.failed||0}`;
    $('#ov-total-files').textContent = a.total || 0;
    $('#ov-total-detail').textContent = `运行 ${a.runs||0} 次 · 成功 ${a.success||0} · 失败 ${a.failed||0}`;

    // 进度条
    const pending  = qs.pending  || 0;
    const running  = qs.running  || 0;
    const done     = qs.done     || 0;
    const failed   = qs.failed   || 0;
    const skipped  = qs.skipped  || 0;
    const total    = qs.total    || 0;
    const finished = done + failed + skipped;
    const pct      = total > 0 ? Math.round(finished * 100 / total) : 0;
    $('#ov-progress-bar').style.width = pct + '%';
    $('#ov-progress-label').textContent =
      running > 0
        ? `进行中 ${finished}/${total} (${pct}%) · 正在跑 ${running} 个`
        : total > 0 ? `总进度 ${finished}/${total} (${pct}%)` : '队列为空';
    // 预估剩余时间(用过去 done 的 throughput 平均)
    let eta = '';
    if (running > 0 && s.total && s.total.duration_sec) {
      // 用最近一次 run 的 throughput 估
      const recentDur = a.recent_avg_dur_sec;
      if (recentDur && recentDur > 0) {
        eta = `· 预估剩余 ${formatDurationPlain(recentDur * (pending / Math.max(a.recent_throughput_per_sec || 1, 1)))}`;
      }
    }
    $('#ov-progress-detail').textContent =
      `完成 ${done} · 跳过 ${skipped} · 失败 ${failed} · 待处理 ${pending}` + eta;

    // 队列条形图
    const totalForBar = Math.max(1, total);
    const bars = [
      { label: '待处理',   n: pending, color: 'bg-blue-500'   },
      { label: '处理中',   n: running, color: 'bg-amber-500'  },
      { label: '已完成',   n: done,    color: 'bg-green-500'  },
      { label: '跳过',     n: skipped, color: 'bg-slate-400'  },
      { label: '失败',     n: failed,  color: 'bg-red-500'    },
    ];
    $('#ov-queue-bars').innerHTML = bars.map(b => {
      const w = Math.round(b.n * 100 / totalForBar);
      return `<div class="flex items-center gap-2">
        <span class="w-14 text-slate-500">${b.label}</span>
        <div class="flex-1 bg-slate-100 rounded h-2 overflow-hidden"><div class="${b.color} h-full" style="width:${w}%"></div></div>
        <span class="w-10 text-right font-mono">${b.n}</span>
      </div>`;
    }).join('');
    $('#ov-queue-summary').textContent = `总计 ${total} 个任务`;

    // 磁盘
    const disk = ds || {};
    const diskEl = $('#ov-disk');
    diskEl.innerHTML = Object.entries(disk).map(([k, v]) => {
      if (!v || v.error) return '';
      const used = v.used || 0;
      const total = v.total || 0;
      const free = v.free || 0;
      const pct = total > 0 ? Math.round(used * 100 / total) : 0;
      return `<div>
        <div class="flex justify-between mb-1">
          <span class="text-slate-600 font-mono">${escapeHtml(k)}</span>
          <span class="text-slate-500">${humanSize(v.used)} / ${humanSize(v.total)} (${pct}%)</span>
        </div>
        <div class="w-full bg-slate-200 rounded-full h-2 overflow-hidden">
          <div class="h-full ${pct>85?'bg-red-500':pct>70?'bg-amber-500':'bg-blue-500'}" style="width:${pct}%"></div>
        </div>
        <div class="text-slate-400 mt-0.5">剩余 ${humanSize(free)}</div>
      </div>`;
    }).join('') || '<div class="text-slate-400">无磁盘信息</div>';

    // 最近失败
    const failedItems = fr.items || [];
    if (failedItems.length === 0) {
      $('#ov-failed').innerHTML = '<div class="text-slate-400 py-2">无失败任务 ✓</div>';
    } else {
      $('#ov-failed').innerHTML = failedItems.map(it => `
        <div class="flex items-start gap-2 py-1 border-b border-slate-100 last:border-0">
          <span class="px-1 rounded bg-red-100 text-red-700 text-xs flex-shrink-0">失败</span>
          <span class="font-mono text-xs text-slate-700 flex-1 truncate">${escapeHtml(it.rel_path)}</span>
          <span class="text-xs text-slate-400">尝试 ${it.attempts}</span>
        </div>
      `).join('');
    }

    // 历史
    const tbody = $('#ov-history-body');
    const rows = s.recent || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="text-center py-6 text-slate-400">暂无数据</td></tr>';
    } else {
      tbody.innerHTML = rows.map(r => {
        const dur = (r.started_at && r.ended_at)
          ? formatDuration(r.started_at, r.ended_at) : '—';
        return `<tr class="border-b border-slate-100">
          <td class="py-2 px-2">${r.id}</td>
          <td class="py-2 px-2 font-mono text-xs">${fmtDate(r.started_at)}</td>
          <td class="py-2 px-2 font-mono text-xs">${fmtDate(r.ended_at)||'进行中…'}</td>
          <td class="py-2 px-2"><span class="px-1.5 py-0.5 rounded bg-slate-100 text-xs">${r.trigger||'-'}</span></td>
          <td class="py-2 px-2 text-right text-green-600">${r.success||0}</td>
          <td class="py-2 px-2 text-right text-slate-500">${r.skipped||0}</td>
          <td class="py-2 px-2 text-right text-red-600">${r.failed||0}</td>
          <td class="py-2 px-2 text-right font-medium">${r.total||0}</td>
          <td class="py-2 px-2 text-right">${dur}</td>
        </tr>`;
      }).join('');
    }
  } catch (e) { console.error(e); toast('加载概览失败: '+e.message, 'error'); }
}

function humanSize(n) {
  if (n == null) return '—';
  const u = ['B','K','M','G','T'];
  let i = 0; let v = n;
  while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
  return v.toFixed(1) + u[i];
}

function formatDurationPlain(sec) {
  if (sec == null) return '—';
  if (sec < 60)    return Math.round(sec) + 's';
  if (sec < 3600)  return Math.floor(sec/60) + 'm';
  return Math.floor(sec/3600) + 'h ' + Math.floor((sec%3600)/60) + 'm';
}

function formatDuration(s, e) {
  try {
    const a = new Date(s.replace(' ', 'T'));
    const b = new Date(e.replace(' ', 'T'));
    const sec = Math.round((b - a) / 1000);
    if (sec < 60) return sec + 's';
    if (sec < 3600) return Math.floor(sec/60) + 'm ' + (sec%60) + 's';
    return Math.floor(sec/3600) + 'h ' + Math.floor((sec%3600)/60) + 'm';
  } catch { return '—'; }
}

// ---- 任务 ----
async function runTask() {
  if (!confirm('确定开始压缩？\n\n脚本会处理 /input 下所有 mp4,成功后会删除原始文件。')) return;
  try {
    const r = await api('/api/run', { body: { trigger: 'manual' } });
    toast(r.message, r.ok ? 'ok' : 'error');
    await refreshStatus();
    await loadOverview();
  } catch (e) { toast('启动失败: ' + e.message, 'error'); }
}
async function stopTask() {
  const st = await api('/api/status');
  let msg = '确定停止当前任务？\n\n会发 SIGTERM,2 秒后 SIGKILL。';
  if (st.external) {
    msg = `检测到这是从终端启动的外部任务。\n\n停止只会杀死：\n  • compress_video.sh (pid ${st.script_pid})\n  • 它启动的所有 ffmpeg 子进程\n\n不会影响你的终端 shell、其它进程、或未压缩的文件。\n\n确定继续？`;
  }
  if (!confirm(msg)) return;
  try {
    const r = await api('/api/stop', { method: 'POST' });
    toast(r.message, r.ok ? 'ok' : 'error');
    await refreshStatus();
    await loadOverview();
  } catch (e) { toast('停止失败: ' + e.message, 'error'); }
}
async function loadRun() { await refreshStatus(); }

// ---- 日志 ----
let logLines = [];      // 当前已加载的行(line_no -> text)
// logSince 已在顶部声明(全局唯一)
let logFilter = { q: '', level: 'all' };
let logSearchTimer = null;

function classifyLogLevel(line) {
  if (!line) return 'info';
  // ERROR
  if (/失败|错误|error|exit=|fatal|Exception/i.test(line)) return 'error';
  // WARN
  if (/警告|warn|超时/i.test(line)) return 'warn';
  // OK
  if (/完成|成功|已启动|已停止|启动:/i.test(line)) return 'ok';
  return 'info';
}

const LOG_LEVEL_COLOR = {
  error: 'text-red-400',
  warn:  'text-amber-300',
  ok:    'text-green-400',
  info:  'text-slate-200',
};

function escapeAttr(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function highlightSearch(text, q) {
  if (!q) return escapeAttr(text);
  // 不区分大小写,全局匹配
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(escaped, 'gi');
  let out = '';
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    out += escapeAttr(text.slice(last, m.index));
    out += `<mark class="bg-yellow-500 text-black">${escapeAttr(m[0])}</mark>`;
    last = m.index + m[0].length;
    if (m.index === re.lastIndex) re.lastIndex++;  // 防零长度死循环
  }
  out += escapeAttr(text.slice(last));
  return out;
}

function renderLogView() {
  const view = $('#log-view');
  // 按 line_no 排序,生成 HTML
  const sorted = logLines.slice().sort((a, b) => a.no - b.no);
  const html = sorted.map(({ no, text }) => {
    const lvl = classifyLogLevel(text);
    const color = LOG_LEVEL_COLOR[lvl] || LOG_LEVEL_COLOR.info;
    return `<div class="${color}"><span class="text-slate-500 select-none pr-2">${no.toString().padStart(5,' ')}</span>${highlightSearch(text, logFilter.q)}</div>`;
  }).join('');
  view.innerHTML = html || '<div class="text-slate-500">（无匹配行）</div>';
  // meta 统计
  const counts = { error: 0, warn: 0, ok: 0, info: 0 };
  for (const { text } of sorted) counts[classifyLogLevel(text)]++;
  const shown = sorted.length;
  const lastNo = sorted.length ? sorted[sorted.length - 1].no : 0;
  $('#log-meta').innerHTML =
    `服务器总行数 <b>${logSince}</b> · 已加载 <b>${shown}</b> ` +
    `<span class="text-green-600">[完成 ${counts.ok}]</span> ` +
    `<span class="text-amber-600">[警告 ${counts.warn}]</span> ` +
    `<span class="text-red-600">[错误 ${counts.error}]</span> ` +
    `<span>[信息 ${counts.info}]</span> ` +
    (logFilter.q ? `<span>· 搜索: <i>${escapeAttr(logFilter.q)}</i></span>` : '') +
    ` · 过滤: ${logFilter.level}`;
}

function maybeScrollLogToBottom() {
  const v = $('#log-view');
  if (!$('#log-autoscroll').checked) return;
  // 仅在用户原本就在底部时,才自动滚(避免查看历史时被跳转)
  // 简化:始终滚到最新
  v.scrollTop = v.scrollHeight;
}

function scrollLogToBottom() {
  $('#log-autoscroll').checked = true;
  const v = $('#log-view');
  v.scrollTop = v.scrollHeight;
}

function clearLogView() {
  logLines = [];
  logSince = 0;
  $('#log-view').innerHTML = '<div class="text-slate-500">（已清屏,下一次刷新会重新拉取）</div>';
  $('#log-meta').textContent = '—';
}

async function loadLogs(reset=false) {
  if (reset) {
    logLines = [];
    logSince = 0;
  }
  const params = new URLSearchParams({
    since: logSince, limit: 1000, max_lines: 5000,
    level: logFilter.level,
  });
  if (logFilter.q) params.set('q', logFilter.q);
  try {
    const r = await api('/api/logs?' + params);
    if (r.lines && r.lines.length) {
      // 用 line_nos 关联
      const nos = r.line_nos || [];
      for (let i = 0; i < r.lines.length; i++) {
        logLines.push({ no: nos[i] || 0, text: r.lines[i] });
      }
      // 去重(以 line_no 为 key)
      const seen = new Set();
      logLines = logLines.filter(x => {
        if (seen.has(x.no)) return false;
        seen.add(x.no);
        return true;
      });
      renderLogView();
      maybeScrollLogToBottom();
    } else if (reset) {
      renderLogView();
    }
    logSince = r.total || logSince;
    // meta 中的总行数补一下
    const totalTxt = $('#log-meta').innerHTML;
    $('#log-meta').innerHTML = totalTxt.replace(/服务器总行数 <b>\d+<\/b>/, `服务器总行数 <b>${logSince}</b>`);
  } catch (e) { console.error(e); }
}

// 搜索 debounce
$('#log-search').addEventListener('input', e => {
  clearTimeout(logSearchTimer);
  logSearchTimer = setTimeout(() => {
    logFilter.q = e.target.value.trim();
    loadLogs(true);
  }, 300);
});

// 级别筛选
$('#log-level').addEventListener('change', e => {
  logFilter.level = e.target.value;
  loadLogs(true);
});

// ---- 文件 ----
async function loadFiles(which) {
  if (which === 'input' || !which) {
    try {
      const r = await api('/api/files/input');
      renderFiles('input', r.files || r);
    } catch (e) { toast('读取输入目录失败: ' + e.message, 'error'); }
  }
  if (which === 'output' || !which) {
    try {
      const r = await api('/api/files/output');
      renderFiles('output', r.files || r);
    } catch (e) { toast('读取输出目录失败: ' + e.message, 'error'); }
  }
}

function renderFiles(which, r) {
  const body = $(`#files-${which}-body`);
  const meta = $(`#files-${which}-meta`);
  if (!r.exists) {
    meta.textContent = '目录不存在';
    body.innerHTML = '<tr><td colspan="3" class="text-center py-6 text-slate-400">目录不存在</td></tr>';
    return;
  }
  if (r.error) {
    meta.textContent = '读取失败: ' + r.error;
    body.innerHTML = '';
    return;
  }
  meta.textContent = `共 ${r.count} 个文件 · 总大小 ${r.total_size_h}`;
  if (!r.items.length) {
    body.innerHTML = '<tr><td colspan="3" class="text-center py-6 text-slate-400">空目录</td></tr>';
    return;
  }
  body.innerHTML = r.items.map(it => `
    <tr class="border-b border-slate-100 hover:bg-slate-50">
      <td class="py-1.5 px-2 font-mono">${escapeHtml(it.path)}</td>
      <td class="py-1.5 px-2 text-right">${fmtBytes(it.size)}</td>
      <td class="py-1.5 px-2 text-right font-mono">${it.mtime}</td>
    </tr>
  `).join('');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ---- 配置 ----
// 配置项 metadata
// group: 显示分组   restart: true=改后需重启服务才生效
const CONFIG_META = {
  OUTPUT_WIDTH:   { label: '输出宽度',     type: 'number', hint: '像素,推荐 1280', min: 320, max: 3840, group: '输出参数', restart: true },
  OUTPUT_HEIGHT:  { label: '输出高度',     type: 'number', hint: '像素,推荐 720',  min: 240, max: 2160, group: '输出参数', restart: true },
  OUTPUT_FPS:     { label: '帧率',         type: 'number', hint: 'fps,推荐 10',   min: 1,   max: 60,   group: '输出参数', restart: true },
  SOFT_CODEC:     { label: '软编码器',     type: 'select', options: ['libx264','libx265'],
                    hint: 'libx264 快,libx265 压缩率高但慢', group: '编码参数', restart: false },
  SOFT_PRESET:    { label: '编码预设',     type: 'select', options: ['ultrafast','superfast','veryfast','fast','medium'],
                    hint: '越慢压缩越好', group: '编码参数', restart: false },
  SOFT_CRF:       { label: '软编码 CRF',   type: 'number', hint: '质量(数字越大越糊)', min: 0, max: 51,
                    group: '编码参数', restart: false },
  VAAPI_QP:       { label: '硬编码 QP',    type: 'number', hint: '硬编质量参数', min: 0, max: 51,
                    group: '编码参数', restart: false },
  NICE_LEVEL:     { label: '进程优先级',   type: 'number', hint: 'nice 值,越大越不抢 CPU', min: -20, max: 19,
                    group: '系统参数', restart: true },
  MAX_LOG_LINES:  { label: '日志最大行数', type: 'number', hint: '超过自动截断', min: 100, max: 100000,
                    group: '系统参数', restart: false },
  MIN_FILE_SIZE:  { label: '最小输出字节', type: 'number', hint: '小于此值视为失败', min: 0,
                    group: '系统参数', restart: false },
};

// 配置分组顺序
const CONFIG_GROUPS = [
  { name: '输出参数', desc: '输出视频的规格' },
  { name: '编码参数', desc: '软/硬编码质量与速度权衡' },
  { name: '系统参数', desc: 'CPU 优先级、日志、判定阈值' },
];

async function loadConfig() {
  try {
    const r = await api('/api/config');
    const form = $('#config-form');
    form.innerHTML = '';
    let dirty = false;  // 有需要重启的项被改了

    for (const grp of CONFIG_GROUPS) {
      const keys = Object.keys(CONFIG_META).filter(k => CONFIG_META[k].group === grp.name);
      if (keys.length === 0) continue;
      const section = document.createElement('div');
      section.className = 'col-span-1 md:col-span-2 mt-2 first:mt-0';
      section.innerHTML = `
        <div class="border-b border-slate-200 pb-1 mb-3">
          <h4 class="text-sm font-semibold text-slate-700">${grp.name}</h4>
          <p class="text-xs text-slate-500">${grp.desc}</p>
        </div>
      `;
      form.appendChild(section);

      for (const k of keys) {
        const m = CONFIG_META[k];
        const v = r.config[k] ?? '';
        const restartBadge = m.restart
          ? `<span class="ml-1 px-1 text-xs rounded bg-amber-100 text-amber-700" title="改这项需重启服务才生效">重启</span>`
          : `<span class="ml-1 px-1 text-xs rounded bg-green-100 text-green-700" title="下次压缩文件时立即生效">热加载</span>`;
        let ctrl;
        if (m.type === 'select') {
          ctrl = `<select name="${k}" class="w-full border border-slate-300 rounded-md px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
            ${m.options.map(o => `<option value="${o}" ${o===v?'selected':''}>${o}</option>`).join('')}
          </select>`;
        } else {
          const range = (m.min !== undefined && m.max !== undefined)
            ? `min="${m.min}" max="${m.max}"`
            : '';
          ctrl = `<input type="number" name="${k}" value="${escapeHtml(v)}" ${range}
            class="w-full border border-slate-300 rounded-md px-2 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500">`;
        }
        const div = document.createElement('div');
        div.className = 'flex flex-col';
        div.innerHTML = `
          <label class="block text-xs font-medium text-slate-600 mb-1">
            ${m.label} ${restartBadge}
            <span class="text-slate-400 ml-1">${m.hint||''}</span>
          </label>
          ${ctrl}
        `;
        form.appendChild(div);
      }
    }
    $('#config-msg').textContent = '';
    $('#config-restart-warn').classList.add('hidden');
  } catch (e) { toast('读取配置失败: ' + e.message, 'error'); }
}

async function loadConfig() {
  try {
    const r = await api('/api/config');
    const form = $('#config-form');
    form.innerHTML = '';
    for (const k of r.keys) {
      const m = CONFIG_META[k] || { label: k, type: 'text' };
      const v = r.config[k] ?? '';
      let ctrl;
      if (m.type === 'select') {
        ctrl = `<select name="${k}" class="w-full border border-slate-300 rounded-md px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
          ${m.options.map(o => `<option value="${o}" ${o===v?'selected':''}>${o}</option>`).join('')}
        </select>`;
      } else {
        ctrl = `<input type="${m.type||'text'}" name="${k}" value="${escapeHtml(v)}" class="w-full border border-slate-300 rounded-md px-2 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500">`;
      }
      const div = document.createElement('div');
      div.innerHTML = `
        <label class="block text-xs font-medium text-slate-600 mb-1">${m.label} <span class="text-slate-400">${m.hint||''}</span></label>
        ${ctrl}
      `;
      form.appendChild(div);
    }
    $('#config-msg').textContent = '';
  } catch (e) { toast('读取配置失败: ' + e.message, 'error'); }
}

async function saveConfig() {
  const data = {};
  let needsRestart = false;
  $$('#config-form [name]').forEach(el => {
    data[el.name] = el.value;
    const m = CONFIG_META[el.name];
    if (m && m.restart) needsRestart = true;
  });
  try {
    const r = await api('/api/config', { method: 'POST', body: { config: data } });
    if (r.ok) {
      let msg = '已保存,旧版本备份为 .bak.manager';
      if (needsRestart) msg += ' · ⚠️ 部分项需重启服务才生效';
      toast(msg, needsRestart ? 'info' : 'ok');
      $('#config-msg').textContent = msg;
      $('#config-msg').className = 'text-sm ' + (needsRestart ? 'text-amber-600' : 'text-green-600');
      if (needsRestart) $('#config-restart-warn').classList.remove('hidden');
    } else {
      $('#config-msg').textContent = '保存失败';
      $('#config-msg').className = 'text-sm text-red-600';
    }
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
}

// ---- 定时任务 (ofelia) ----
let cronJobs = [];

async function loadCron() {
  try {
    const [cronR, statusR, statsR] = await Promise.all([
      api('/api/cron'),
      api('/api/cron/status'),
      api('/api/stats'),
    ]);
    cronJobs = cronR.jobs;
    $('#cron-path').textContent = cronR.ini_path;
    renderCron();
    renderOfeliaStatus(statusR.state);
    renderNextRuns(cronJobs);
    renderCronHistory(statsR.recent || []);
    $('#cron-msg').textContent = '';
  } catch (e) { toast('读取定时任务失败: ' + e.message, 'error'); }
}

function renderOfeliaStatus(state) {
  const el = $('#cron-ofelia-state');
  const map = {
    running:           { text: '🟢 运行中', cls: 'text-green-600' },
    exited:            { text: '⚪ 已退出', cls: 'text-slate-500' },
    absent:            { text: '❓ 未发现', cls: 'text-amber-600' },
    docker_unavailable: { text: '⚠️ docker 不可用', cls: 'text-red-600' },
  };
  const m = map[state] || { text: state, cls: 'text-slate-500' };
  el.textContent = m.text;
  el.className = 'text-lg font-semibold ' + m.cls;
}

function renderNextRuns(jobs) {
  const el = $('#cron-next-runs');
  const future = jobs
    .map(j => ({ name: j.name || j.section || '(unnamed)', next: j.next_run }))
    .filter(j => j.next && j.next !== 'invalid' && j.next !== '');
  if (!future.length) {
    el.innerHTML = '<div class="text-slate-400">无有效调度</div>';
    return;
  }
  el.innerHTML = future.slice(0, 5).map(j =>
    `<div><span class="font-mono text-slate-700">${escapeHtml(j.name)}</span>: <span class="text-blue-600">${escapeHtml(j.next)}</span></div>`
  ).join('');
}

function renderCronHistory(recent) {
  const tbody = $('#cron-history-body');
  if (!recent.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-4 text-slate-400">暂无记录</td></tr>';
    return;
  }
  // 只展示非 manual 触发的(cron 触发的),如果都是 manual 就全部展示
  const cronRuns = recent.filter(r => r.trigger && r.trigger !== 'manual').concat(
    recent.filter(r => r.trigger === 'manual')
  ).slice(0, 10);
  tbody.innerHTML = cronRuns.map(r => {
    const dur = (r.started_at && r.ended_at)
      ? formatDuration(r.started_at, r.ended_at) : (r.ended_at ? '—' : '进行中…');
    const triggerBadge = {
      manual: 'bg-blue-100 text-blue-700',
      cron:   'bg-amber-100 text-amber-700',
      unknown:'bg-slate-100 text-slate-700',
    }[r.trigger || 'unknown'] || 'bg-slate-100 text-slate-700';
    return `<tr class="border-b border-slate-100">
      <td class="py-2 px-2">${r.id}</td>
      <td class="py-2 px-2 font-mono text-xs">${fmtDate(r.started_at)}</td>
      <td class="py-2 px-2 font-mono text-xs">${fmtDate(r.ended_at)||'—'}</td>
      <td class="py-2 px-2"><span class="px-1.5 py-0.5 rounded text-xs ${triggerBadge}">${r.trigger||'-'}</span></td>
      <td class="py-2 px-2 text-right text-green-600">${r.success||0}</td>
      <td class="py-2 px-2 text-right text-red-600">${r.failed||0}</td>
      <td class="py-2 px-2 text-right font-medium">${r.total||0}</td>
      <td class="py-2 px-2 text-right">${dur}</td>
    </tr>`;
  }).join('');
}

async function triggerRunNow() {
  if (!confirm('立即触发一次压缩任务?')) return;
  try {
    const r = await api('/api/run', { method: 'POST', body: { trigger: 'manual' } });
    toast(r.message, r.ok ? 'ok' : 'error');
    // 2s 后自动刷新页面状态
    setTimeout(() => loadCron(), 2000);
  } catch (e) { toast('触发失败: ' + e.message, 'error'); }
}

function renderCron() {
  const wrap = $('#cron-list');
  if (!cronJobs.length) {
    wrap.innerHTML = '<div class="text-sm text-slate-400 text-center py-4">暂无定时任务,点 "新增" 添加一个</div>';
    return;
  }
  wrap.innerHTML = cronJobs.map((j, idx) => `
    <div class="bg-slate-50 rounded-lg p-4 border border-slate-200 space-y-2">
      <div class="flex items-center justify-between">
        <div class="font-medium text-sm">📅 任务 #${idx+1}: <span class="font-mono">${escapeHtml(j.name||j.section)}</span></div>
        <button onclick="delCronJob(${idx})" class="text-xs text-red-600 hover:underline">删除</button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
        <div>
          <label class="block text-xs text-slate-500 mb-1">名称</label>
          <input data-i="${idx}" data-k="name" value="${escapeHtml(j.name||'')}" class="w-full border border-slate-300 rounded px-2 py-1 font-mono text-xs">
        </div>
        <div>
          <label class="block text-xs text-slate-500 mb-1">容器</label>
          <input data-i="${idx}" data-k="container" value="${escapeHtml(j.container||'')}" class="w-full border border-slate-300 rounded px-2 py-1 font-mono text-xs">
        </div>
        <div class="md:col-span-2">
          <label class="block text-xs text-slate-500 mb-1">调度表达式（cron 5 字段）</label>
          <input data-i="${idx}" data-k="schedule" value="${escapeHtml(j.schedule||'')}" class="w-full border border-slate-300 rounded px-2 py-1 font-mono text-xs">
          <div class="text-xs text-slate-400 mt-1">下次运行: <span class="font-mono">${j.next_run||'—'}</span></div>
        </div>
        <div class="md:col-span-2">
          <label class="block text-xs text-slate-500 mb-1">命令</label>
          <input data-i="${idx}" data-k="command" value="${escapeHtml(j.command||'')}" class="w-full border border-slate-300 rounded px-2 py-1 font-mono text-xs">
        </div>
      </div>
    </div>
  `).join('');
  // 同步编辑
  $$('#cron-list [data-i]').forEach(el => {
    el.addEventListener('input', e => {
      const i = +el.dataset.i, k = el.dataset.k;
      cronJobs[i][k] = el.value;
    });
  });
}

function addCronJob() {
  cronJobs.push({
    section: 'job-exec "new-task"',
    name:    'new-task',
    schedule: '0 3 * * *',
    container: 'ffmpeg-worker',
    command:   'bash /scripts/compress_video.sh',
  });
  renderCron();
}

function delCronJob(i) {
  if (!confirm('删除这个定时任务？')) return;
  cronJobs.splice(i, 1);
  renderCron();
}

async function saveCron() {
  try {
    const r = await api('/api/cron', { method: 'POST', body: { jobs: cronJobs } });
    if (r.ok) {
      cronJobs = r.jobs;
      renderCron();
      $('#cron-msg').textContent = '已保存,记得点 "重启 ofelia" 让配置生效';
      $('#cron-msg').className = 'text-sm text-green-600';
      toast('已保存', 'ok');
    } else {
      $('#cron-msg').textContent = '保存失败';
      $('#cron-msg').className = 'text-sm text-red-600';
    }
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
}

async function restartOfelia() {
  if (!confirm('重启 ofelia 容器？\n(需要 docker 权限,失败时会给出手动命令)')) return;
  try {
    const r = await api('/api/cron/restart', { method: 'POST' });
    toast(r.message, r.ok ? 'ok' : 'error');
  } catch (e) { toast('重启失败: ' + e.message, 'error'); }
}

// ---- 系统 ----
async function loadSystem() {
  try {
    const s = await api('/api/system');
    $('#sys-ffmpeg').textContent = s.ffmpeg ? `ffmpeg: ${s.ffmpeg}` : 'ffmpeg 未找到';
    const dl = $('#sys-info');
    dl.innerHTML = Object.entries({
      'ffmpeg 路径': s.ffmpeg || '—',
      '版本': s.ffmpeg_version || '—',
      '输入目录': s.input_dir,
      '输出目录': s.output_dir,
      '脚本路径': s.script,
      '日志路径': s.script_log,
      '硬件提示': (s.hints || []).join(' · '),
    }).map(([k,v]) => `<dt class="text-slate-500">${k}</dt><dd class="font-mono text-xs break-all">${escapeHtml(v)}</dd>`).join('');
  } catch (e) { console.error(e); }
  try {
    const d = await api('/api/disk');
    const wrap = $('#disk-info');
    wrap.innerHTML = Object.entries(d).map(([k,v]) => {
      if (v.error) return `<div><div class="text-sm font-medium">${k}</div><div class="text-xs text-red-600">${v.error}</div></div>`;
      const pct = v.percent || 0;
      const color = pct > 90 ? 'bg-red-500' : pct > 75 ? 'bg-amber-500' : 'bg-blue-500';
      return `<div>
        <div class="flex items-center justify-between text-sm mb-1">
          <span class="font-medium">${k}</span>
          <span class="text-xs text-slate-500">${v.used_h} / ${v.total_h} (${pct}%)</span>
        </div>
        <div class="h-2 bg-slate-200 rounded-full overflow-hidden">
          <div class="${color} h-full" style="width:${pct}%"></div>
        </div>
      </div>`;
    }).join('');
  } catch (e) { console.error(e); }
}

// ---- 队列 ----
let qFilter = 'all';
let qOffset = 0;
let qSortBy = null;      // null = 默认 (status 优先级 + id DESC)
let qSortDir = 'desc';   // asc / desc
let qSearch = '';
const Q_LIMIT = 100;
let qSelected = new Set();
let qSearchTimer = null;

async function loadQueue() {
  await Promise.all([loadQueueStats(), loadQueueList()]);
}

async function loadQueueStats() {
  try {
    const s = await api('/api/queue/stats');
    $('#q-stat-pending').textContent = s.pending  ?? 0;
    $('#q-stat-running').textContent = s.running  ?? 0;
    $('#q-stat-done').textContent    = s.done     ?? 0;
    $('#q-stat-skipped').textContent = s.skipped  ?? 0;
    $('#q-stat-failed').textContent  = s.failed   ?? 0;
  } catch (e) { /* ignore */ }
}

async function loadQueueList() {
  const status = qFilter === 'all' ? '' : qFilter;
  const params = new URLSearchParams({
    status, limit: Q_LIMIT, offset: qOffset,
  });
  if (qSortBy) {
    params.set('sort_by', qSortBy);
    params.set('sort_dir', qSortDir);
  }
  if (qSearch) params.set('q', qSearch);
  try {
    const r = await api(`/api/queue?${params}`);
    renderQueue(r);
    updateSortIndicators();
  } catch (e) {
    $('#q-body').innerHTML = `<tr><td colspan="11" class="text-center py-6 text-red-500">加载失败: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function updateSortIndicators() {
  $$('th[data-sort]').forEach(th => {
    const ind = th.querySelector('.q-sort-ind');
    if (!ind) return;
    if (th.dataset.sort === qSortBy) {
      ind.textContent = qSortDir === 'asc' ? '▲' : '▼';
      ind.classList.add('text-blue-600');
    } else {
      ind.textContent = '';
      ind.classList.remove('text-blue-600');
    }
  });
}

function renderQueue(r) {
  const body = $('#q-body');
  qSelected = new Set();
  updateRetryBtn();
  if (!r.items || r.items.length === 0) {
    body.innerHTML = '<tr><td colspan="11" class="text-center py-6 text-slate-400">无任务</td></tr>';
    $('#q-list-meta').textContent = `总计 ${r.total} 个`;
    $('#q-page-info').textContent = '';
    $('#q-page-prev').disabled = true;
    $('#q-page-next').disabled = true;
    return;
  }
  body.innerHTML = r.items.map(it => {
    const statusBadge = {
      pending:  '<span class="px-1.5 py-0.5 text-xs rounded bg-blue-100 text-blue-700">待处理</span>',
      running:  '<span class="px-1.5 py-0.5 text-xs rounded bg-amber-100 text-amber-700">处理中</span>',
      done:     '<span class="px-1.5 py-0.5 text-xs rounded bg-green-100 text-green-700">已完成</span>',
      skipped:  '<span class="px-1.5 py-0.5 text-xs rounded bg-slate-100 text-slate-600">跳过</span>',
      failed:   '<span class="px-1.5 py-0.5 text-xs rounded bg-red-100 text-red-700">失败</span>',
    }[it.status] || it.status;
    const errCell = it.last_error
      ? `<span class="text-xs text-red-600" title="${escapeHtml(it.last_error)}">${escapeHtml(it.last_error.slice(0, 40))}${it.last_error.length > 40 ? '…' : ''}</span>`
      : '<span class="text-xs text-slate-300">—</span>';
    // 输入/输出/压缩比
    const inCell  = it.size        != null ? `<span class="text-slate-700">${fmtBytes(it.size)}</span>`        : '<span class="text-slate-300">—</span>';
    const outCell = it.output_size != null ? `<span class="text-slate-700">${fmtBytes(it.output_size)}</span>` : '<span class="text-slate-300">—</span>';
    let ratioCell = '<span class="text-slate-300">—</span>';
    if (it.ratio != null) {
      const pct = (it.ratio * 100).toFixed(1);
      const color = it.ratio < 0.3 ? 'text-green-600' : it.ratio < 0.6 ? 'text-amber-600' : 'text-red-600';
      ratioCell = `<span class="${color} font-mono">${pct}%</span>`;
    }
    // 用时
    const durCell = it.duration_sec != null
      ? `<span class="font-mono">${formatDuration(it.duration_sec)}</span>`
      : '<span class="text-slate-300">—</span>';
    return `<tr class="border-b border-slate-100 hover:bg-slate-50" data-id="${it.id}">
      <td class="py-1.5 px-2"><input type="checkbox" class="q-check" data-id="${it.id}"></td>
      <td class="py-1.5 px-2 text-slate-400 font-mono text-xs">${it.id}</td>
      <td class="py-1.5 px-2 font-mono text-xs">${escapeHtml(it.rel_path)}</td>
      <td class="py-1.5 px-2 text-right font-mono text-xs">${inCell}</td>
      <td class="py-1.5 px-2 text-right font-mono text-xs">${outCell}</td>
      <td class="py-1.5 px-2 text-right text-xs">${ratioCell}</td>
      <td class="py-1.5 px-2 text-right text-xs">${durCell}</td>
      <td class="py-1.5 px-2 text-center text-xs">${it.attempts || 0}</td>
      <td class="py-1.5 px-2">${statusBadge}</td>
      <td class="py-1.5 px-2">${errCell}</td>
      <td class="py-1.5 px-2 font-mono text-xs text-slate-500">${it.ended_at || '—'}</td>
    </tr>`;
  }).join('');
  $('#q-list-meta').textContent = `总计 ${r.total} 个,显示 ${r.offset + 1}-${r.offset + r.items.length}`;
  const start = r.offset + 1;
  const end   = r.offset + r.items.length;
  $('#q-page-info').textContent = `${start}-${end} / ${r.total}`;
  $('#q-page-prev').disabled = qOffset === 0;
  $('#q-page-next').disabled = end >= r.total;
  // bind checkboxes
  $$('.q-check').forEach(cb => cb.addEventListener('change', e => {
    const id = parseInt(e.target.dataset.id);
    if (e.target.checked) qSelected.add(id); else qSelected.delete(id);
    updateRetryBtn();
  }));
  $('#q-check-all').checked = false;
}

function updateRetryBtn() {
  const n = qSelected.size;
  $('#q-btn-retry').disabled  = n === 0;
  $('#q-btn-delete').disabled = n === 0;
  $('#q-btn-retry').textContent  = n > 0 ? `重试选中 (${n})` : '重试选中';
  $('#q-btn-delete').textContent = n > 0 ? `删除选中 (${n})` : '删除选中';
}

// 筛选按钮
$$('.q-filter').forEach(btn => btn.addEventListener('click', () => {
  $$('.q-filter').forEach(b => {
    b.classList.remove('bg-slate-200', 'text-slate-700');
    b.classList.add('bg-slate-100', 'text-slate-600');
  });
  btn.classList.remove('bg-slate-100', 'text-slate-600');
  btn.classList.add('bg-slate-200', 'text-slate-700');
  qFilter = btn.dataset.qfilter;
  qOffset = 0;
  loadQueueList();
}));

// 排序表头点击
$$('th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.sort;
    if (qSortBy === col) {
      qSortDir = qSortDir === 'asc' ? 'desc' : 'asc';
    } else {
      qSortBy  = col;
      qSortDir = 'desc';  // 新列默认降序
    }
    qOffset = 0;
    loadQueueList();
  });
});

// 搜索输入(防抖 300ms)
$('#q-search').addEventListener('input', e => {
  clearTimeout(qSearchTimer);
  qSearchTimer = setTimeout(() => {
    qSearch = e.target.value.trim();
    qOffset = 0;
    loadQueueList();
  }, 300);
});

// 全选
$('#q-check-all').addEventListener('change', e => {
  const checked = e.target.checked;
  $$('.q-check').forEach(cb => {
    cb.checked = checked;
    const id = parseInt(cb.dataset.id);
    if (checked) qSelected.add(id); else qSelected.delete(id);
  });
  updateRetryBtn();
});

// 分页
$('#q-page-prev').addEventListener('click', () => {
  qOffset = Math.max(0, qOffset - Q_LIMIT);
  loadQueueList();
});
$('#q-page-next').addEventListener('click', () => {
  qOffset += Q_LIMIT;
  loadQueueList();
});

// 重试选中
$('#q-btn-retry').addEventListener('click', async () => {
  if (qSelected.size === 0) return;
  if (!confirm(`确认重试选中的 ${qSelected.size} 个任务?\n\n会删除对应的输出文件,重新标记为待处理。`)) return;
  try {
    const r = await api('/api/queue/retry', { body: { ids: [...qSelected] } });
    toast(`重试 ${r.result.reset} 个任务(删除 ${r.result.deleted_outputs} 个输出)`, 'ok');
    qSelected.clear();
    updateRetryBtn();
    await loadQueue();
  } catch (e) { toast('重试失败: ' + e.message, 'error'); }
});

// 删除选中（只删除表行,不动 /input /output 文件;正在跑的任务会被拒绝）
$('#q-btn-delete').addEventListener('click', async () => {
  if (qSelected.size === 0) return;
  if (!confirm(`确认删除选中的 ${qSelected.size} 个任务?\n\n只是从队列表中移除记录,不会删除 /input 或 /output 里的实际文件。\n正在跑的任务会被跳过。`)) return;
  try {
    const r = await api('/api/queue/delete', { method: 'POST', body: { ids: [...qSelected] } });
    let msg = `删除 ${r.result.deleted} 个任务`;
    if (r.result.rejected) msg += `,跳过 ${r.result.rejected} 个正在跑的任务`;
    toast(msg, 'ok');
    qSelected.clear();
    updateRetryBtn();
    await loadQueue();
  } catch (e) { toast('删除失败: ' + e.message, 'error'); }
});

// 重新扫描
$('#q-btn-rescan').addEventListener('click', async () => {
  if (!confirm('重新扫描 /input 目录并同步到队列?\n\n仅添加新文件/标记已完成的输出,不会改动正在处理的项。')) return;
  try {
    const r = await api('/api/queue/sync', { method: 'POST' });
    const s = r.synced;
    toast(`同步完成: 待处理 +${s.added_input} / 已完成 +${s.added_done} / 标记 +${s.updated_done} / 调和 ${s.reconciled||0}`, 'ok');
    await loadQueue();
  } catch (e) { toast('扫描失败: ' + e.message, 'error'); }
});

// ---- 自动刷新 ----
setInterval(() => {
  if (!$('[data-panel="overview"]').classList.contains('hidden')) loadOverview();
  if (!$('[data-panel="run"]').classList.contains('hidden'))      refreshStatus();
  if (!$('[data-panel="logs"]').classList.contains('hidden') && $('#log-autorefresh').checked) loadLogs();
  if (!$('[data-panel="queue"]').classList.contains('hidden'))    loadQueue();
}, 2000);

// ---- 启动 ----
showTab('overview');
loadSystem();