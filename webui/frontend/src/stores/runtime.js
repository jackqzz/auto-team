import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ADMIN_TOKEN_KEY, createSSE } from '@/api/request'
import { useStatsStore } from './stats'

let _logId = 0
const MAX_LOGS = 2000

function classify(line) {
  const l = (line || '').toLowerCase()
  if (l.includes('error') || l.includes('失败') || l.includes('拒绝')) return 'err'
  if (l.includes('warning') || l.includes('warn')) return 'warn'
  if (l.includes('成功') || l.includes('完成') || l.includes('命中') || l.includes('ok')) return 'ok'
  return ''
}

// 运行时状态：全局实时日志 + 单个注册 SSE + 自动跑号 SSE/状态 + 告警横幅。
// 放在 store 里是为了「切换菜单页面时，后台自动跑号和日志不中断」。
export const useRuntimeStore = defineStore('runtime', () => {
  const logs = ref([])            // { id, text, kind }
  const autoStatus = ref({
    state: 'stopped',
    registered_ok: 0,
    registered_fail: 0,
    retry_count: 0,
    retry_attempts: 0,
    task_total: null,
    task_total_known: false,
    task_completed: 0,
    progress_percent: null,
    proxy_pool_usage: [],
  })
  const banner = ref('')          // 熔断/严重错误横幅
  const lastRunResult = ref(null) // { email, password, access_token_len, partial } 或 { error }
  const dataVersion = ref(0)      // 递增：通知号池/结果/记录表刷新
  const runningSingle = ref(false)

  let currentEs = null
  let autoEs = null

  function addLog(text, kind) {
    logs.value.push({ id: ++_logId, text, kind: kind ?? classify(text) })
    if (logs.value.length > MAX_LOGS) logs.value.splice(0, logs.value.length - MAX_LOGS)
  }
  function clearLogs() { logs.value = [] }
  function bumpData() { dataVersion.value++ }
  function dismissBanner() { banner.value = '' }

  // ─── 单个注册 run 的 SSE ───
  function streamRun(runId) {
    if (currentEs) { try { currentEs.close() } catch (_) {} }
    runningSingle.value = true
    const es = createSSE(`/api/runs/${runId}/stream`, {
      log: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.line) addLog(d.line)
        } catch (_) {}
      },
      status: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.kind === 'done') {
            lastRunResult.value = {
              email: d.email,
              password: d.password || '',
              access_token_len: d.access_token_len,
              partial: d.partial,
            }
            addLog(
              `注册完成: ${d.email}${d.password ? ' / ' + d.password : ''}`
              + ` (access_token=${d.access_token_len}${d.partial ? ', 部分凭证' : ''})`,
              'ok',
            )
          } else if (d.kind === 'error') {
            lastRunResult.value = { email: d.email, error: d.message }
            addLog('错误: ' + d.message, 'err')
          } else if (d.kind === 'phase') {
            addLog(`phase=${d.phase} email=${d.email}`, 'evt')
          }
        } catch (_) {}
      },
      end: () => {
        try { es.close() } catch (_) {}
        currentEs = null
        runningSingle.value = false
        useStatsStore().refresh()
        bumpData()
      },
    }, () => {
      try { es.close() } catch (_) {}
      currentEs = null
      runningSingle.value = false
    })
    currentEs = es
  }

  // ─── 自动跑号全局 SSE（app 启动时连一次，自动重连） ───
  function connectAutoStream() {
    if (autoEs) { try { autoEs.close() } catch (_) {} }
    const es = createSSE('/api/auto/stream', {
      state: (e) => {
        try { autoStatus.value = JSON.parse(e.data) } catch (_) {}
      },
      run_started: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[auto] 开始注册 ${d.email} (run=${d.run_id})`, 'evt')
          streamRun(d.run_id) // 复用单跑 SSE，接管日志
        } catch (_) {}
      },
      run_finished: (e) => {
        try {
          const d = JSON.parse(e.data)
          const tag = d.ok
            ? '[成功]'
            : (d.retry_scheduled ? '[重试中]' : (d.category === 'network' ? '[网络错误，号已 release]' : '[最终失败]'))
          // run_finished 描述的是一次尝试；是否计入最终失败由后端的
          // retry_scheduled 字段决定，避免把中途失败误显示成账号失败。
          const suffix = d.ok ? '完成' : (d.retry_scheduled ? '本次失败，已排队重试' : '最终结束')
          addLog(
            `[auto] ${tag} ${d.email} ${suffix}`,
            d.ok ? 'ok' : (d.retry_scheduled ? 'warn' : 'err'),
          )
          useStatsStore().refresh()
          bumpData()
        } catch (_) {}
      },
      circuit_break: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[auto] 熔断: ${d.reason}`, 'err')
          banner.value = d.reason
        } catch (_) {}
      },
    }, () => {
      // 断线自动重连
      try { es.close() } catch (_) {}
      autoEs = null
      if (
        localStorage.getItem(ADMIN_TOKEN_KEY)
        && !(window.location.hash || '').startsWith('#/public-relogin')
      ) {
        setTimeout(connectAutoStream, 2000)
      }
    })
    autoEs = es
  }

  function disconnectAutoStream() {
    if (autoEs) {
      try { autoEs.close() } catch (_) {}
    }
    autoEs = null
  }

  return {
    logs, autoStatus, banner, lastRunResult, dataVersion, runningSingle,
    addLog, clearLogs, bumpData, dismissBanner, streamRun, connectAutoStream, disconnectAutoStream,
  }
})
