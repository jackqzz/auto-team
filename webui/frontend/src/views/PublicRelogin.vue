<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  checkPublicRelogin,
  getPublicReloginQueueStatus,
  refreshPublicReloginExport,
  runPublicRelogin,
} from '@/api/publicRelogin'
import { useProxyStore } from '@/stores/proxy'
import {
  decodeJwtPayload,
  buildCpaTokenJson,
  pushToCpa,
  pushToSub2Api,
  testCpaConnection,
  testSub2ApiConnection,
} from '@/utils/publicPoolPush'

const fileInput = ref(null)
const proxyStore = useProxyStore()
const loading = ref(false)
const checking = ref(false)
const relogining = ref(false)
const rawText = ref('')
const accounts = ref([])
const lastResults = ref({})
const checkProgress = ref({ done: 0, total: 0 })
const reloginProgress = ref({ done: 0, total: 0 })
const import2faVisible = ref(false)
const import2faText = ref('')
const import2faErrors = ref([])
const ACCESS_KEY_CACHE = 'gpt_auto_register_public_relogin_access_key'
const INSPECTION_SETTINGS_CACHE = 'gpt_auto_register_public_relogin_inspection'
const POOL_PUSH_CONFIG_CACHE = 'gpt_auto_register_public_relogin_pool_push'
const accessKey = ref('')
const inspectionBatchSize = ref(8)
const inspectionIntervalMinutes = ref(5)
const inspectionRunning = ref(false)
const inspectionNextAt = ref(0)
const inspectionRound = ref(0)
const inspectionLastBatch = ref(0)
const queueStatus = ref({ quota: null, relogin: null })
const clockNow = ref(Date.now())
const poolPushDrawerVisible = ref(false)
const testingPoolTarget = ref('')
const manualPoolPushing = ref(false)
const downloading = ref(false)
const aliveOnly = ref(false)
const poolPushResults = ref({})
const poolPushStats = reactive({ queued: 0, running: 0, success: 0, failed: 0, lastMessage: '' })

const defaultPoolPushConfig = () => ({
  autoEnabled: false,
  cpa: { enabled: false, url: '', key: '', timeout: 30 },
  sub2api: { enabled: false, url: '', key: '', groupIds: '2', timeout: 30 },
})
const poolPushConfig = reactive(defaultPoolPushConfig())
const pushedCredentials = new Set()
let inspectionTimer = null
let clockTimer = null
let queueStatusTimer = null
let poolPushQueue = Promise.resolve()
let suppressPoolPushSave = false

const statusCount = computed(() => ({
  total: accounts.value.length,
  active: accounts.value.filter((a) => a.status === 'active').length,
  unauthorized: accounts.value.filter((a) => a.status === '401').length,
  revived: accounts.value.filter((a) => a.status === 'revived').length,
  deactivated: accounts.value.filter((a) => a.status === 'deactivated').length,
}))
// 只有已经确认额度接口可用的账号才算“存活”。unknown/401/error/failed
// 都不是存活证明，不能进入手动推送或导出批次；复活项是刚完成重登录的有效账号。
const isAliveAccount = (item) => item?.status === 'active' || item?.status === 'revived'
const aliveAccounts = computed(() => accounts.value.filter(isAliveAccount))
// 导入后的账号状态是 unknown，需要先做一次额度检查才能确认是否存活；
// 因此检测范围只排除已确认停用的账号，不能复用严格的推送范围。
const checkableAccounts = computed(() => accounts.value.filter((item) => item?.status !== 'deactivated'))
const visibleAccounts = computed(() => (aliveOnly.value ? aliveAccounts.value : accounts.value))

const inspectionCountdown = computed(() => {
  if (!inspectionRunning.value || !inspectionNextAt.value) return '-'
  const seconds = Math.max(0, Math.ceil((inspectionNextAt.value - clockNow.value) / 1000))
  const minutes = Math.floor(seconds / 60)
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`
})
const queueItems = computed(() => [queueStatus.value.quota, queueStatus.value.relogin].filter(Boolean))
const enabledPoolTargetNames = computed(() => [
  poolPushConfig.cpa.enabled ? 'CPA' : '',
  poolPushConfig.sub2api.enabled ? 'Sub2API' : '',
].filter(Boolean))
const configuredPoolTargetNames = computed(() => [
  poolPushConfig.cpa.url.trim() && poolPushConfig.cpa.key.trim() ? 'CPA' : '',
  poolPushConfig.sub2api.url.trim() && poolPushConfig.sub2api.key.trim() ? 'Sub2API' : '',
].filter(Boolean))
const manualPoolPushConfigIssue = computed(() => (
  configuredPoolTargetNames.value.length ? '' : '请先填写 CPA 或 Sub2API 的地址和密钥'
))
const poolPushConfigIssue = computed(() => {
  if (!enabledPoolTargetNames.value.length) return '请至少启用一个推送目标'
  if (poolPushConfig.cpa.enabled && (!poolPushConfig.cpa.url.trim() || !poolPushConfig.cpa.key.trim())) {
    return 'CPA 已启用，但 URL 或管理密钥未填写完整'
  }
  if (poolPushConfig.sub2api.enabled && (!poolPushConfig.sub2api.url.trim() || !poolPushConfig.sub2api.key.trim())) {
    return 'Sub2API 已启用，但 URL 或 API Key 未填写完整'
  }
  return ''
})

const openFile = () => fileInput.value?.click()

const import2faLineCount = computed(
  () => import2faText.value.split('\n').filter((l) => l.trim() && !l.trim().startsWith('#')).length,
)

function doImport2FA() {
  const text = import2faText.value.trim()
  if (!text) return ElMessage.warning('请输入要导入的账号')
  import2faErrors.value = []
  const lines = text.split('\n')
  const prepared = []
  const errors = []
  for (let idx = 0; idx < lines.length; idx++) {
    const line = lines[idx].trim()
    if (!line || line.startsWith('#')) continue
    const parts = line.split('----')
    if (parts.length < 2 || parts.length > 3) {
      errors.push({ line: idx + 1, error: `需要 2~3 段（邮箱----密码 或 邮箱----密码----2FA），实际 ${parts.length} 段` })
      continue
    }
    const email = parts[0].trim().toLowerCase()
    const password = parts[1].trim()
    const secret = parts.length === 3 ? parts[2].trim() : ''
    if (!email || !email.includes('@')) {
      errors.push({ line: idx + 1, error: '邮箱格式不对' })
      continue
    }
    if (!password) {
      errors.push({ line: idx + 1, error: '密码不能为空' })
      continue
    }
    prepared.push({ email, password, totp_secret: secret })
  }
  if (errors.length) {
    import2faErrors.value = errors
    ElMessage.error(`有 ${errors.length} 行不合法，请修正后重试`)
    return
  }
  if (!prepared.length) {
    ElMessage.warning('没有有效行可导入')
    return
  }
  // 将解析结果合并到 accounts 列表（和 JSON 导入一样，纯前端，不写库）
  const newAccounts = prepared.map((item, idx) => normalizeAccount({
    email: item.email,
    password: item.password,
    totp_secret: item.totp_secret,
  }, `2fa-${accounts.value.length + idx}`))
  accounts.value = [...accounts.value, ...newAccounts]
  lastResults.value = {}
  ElMessage.success(`已导入 ${prepared.length} 个 2FA 账号`)
  import2faText.value = ''
  import2faVisible.value = false
}

async function loadQueueStatus() {
  try {
    const result = await getPublicReloginQueueStatus()
    queueStatus.value = result.queues || { quota: null, relogin: null }
  } catch (_) { /* 页面未启用或服务重启时保留上一次状态 */ }
}

function handleAuthError(e) {
  if (e?.status === 403 && String(e.message || '').includes('访问密钥')) {
    accessKey.value = ''
    localStorage.removeItem(ACCESS_KEY_CACHE)
  }
}

function requireAccessKey() {
  const key = accessKey.value.trim()
  if (!key) {
    ElMessage.warning('请先输入公开重登访问密钥')
    return ''
  }
  return key
}

function poolTargetConfig(target) {
  return target === 'cpa' ? poolPushConfig.cpa : poolPushConfig.sub2api
}

function poolTargetLabel(target) {
  return target === 'cpa' ? 'CPA' : 'Sub2API'
}

function loadPoolPushConfig() {
  try {
    const saved = JSON.parse(localStorage.getItem(POOL_PUSH_CONFIG_CACHE) || '{}')
    poolPushConfig.autoEnabled = Boolean(saved.autoEnabled)
    Object.assign(poolPushConfig.cpa, {
      enabled: Boolean(saved.cpa?.enabled),
      url: String(saved.cpa?.url || ''),
      key: String(saved.cpa?.key || ''),
      timeout: Math.max(5, Math.min(300, Number(saved.cpa?.timeout) || 30)),
    })
    Object.assign(poolPushConfig.sub2api, {
      enabled: Boolean(saved.sub2api?.enabled),
      url: String(saved.sub2api?.url || ''),
      key: String(saved.sub2api?.key || ''),
      groupIds: String(saved.sub2api?.groupIds || '2'),
      timeout: Math.max(5, Math.min(300, Number(saved.sub2api?.timeout) || 30)),
    })
  } catch (_) {
    localStorage.removeItem(POOL_PUSH_CONFIG_CACHE)
  }
}

function savePoolPushConfig() {
  if (suppressPoolPushSave) return
  localStorage.setItem(POOL_PUSH_CONFIG_CACHE, JSON.stringify({
    autoEnabled: poolPushConfig.autoEnabled,
    cpa: { ...poolPushConfig.cpa },
    sub2api: { ...poolPushConfig.sub2api },
  }))
}

async function clearPoolPushConfig() {
  if (poolPushStats.queued || poolPushStats.running) {
    ElMessage.warning('号池推送仍在执行，请等待当前队列结束后再清除配置')
    return
  }
  try {
    await ElMessageBox.confirm(
      '将从当前浏览器清除 CPA/Sub2API 地址、密钥和自动推送设置。',
      '清除本地号池配置',
      { type: 'warning', confirmButtonText: '清除', cancelButtonText: '取消' },
    )
  } catch (_) {
    return
  }
  suppressPoolPushSave = true
  Object.assign(poolPushConfig, defaultPoolPushConfig())
  poolPushResults.value = {}
  poolPushStats.queued = 0
  poolPushStats.running = 0
  poolPushStats.success = 0
  poolPushStats.failed = 0
  poolPushStats.lastMessage = ''
  pushedCredentials.clear()
  localStorage.removeItem(POOL_PUSH_CONFIG_CACHE)
  await nextTick()
  suppressPoolPushSave = false
  ElMessage.success('当前浏览器的号池配置已清除')
}

async function testPoolTarget(target) {
  testingPoolTarget.value = target
  try {
    const config = poolTargetConfig(target)
    const result = target === 'cpa'
      ? await testCpaConnection(config)
      : await testSub2ApiConnection(config)
    ElMessage.success(result.message)
  } catch (error) {
    ElMessage.error(error.message || `${poolTargetLabel(target)} 连接失败`)
  } finally {
    testingPoolTarget.value = ''
  }
}

function pushCredentialKey(target, account) {
  return `${target}:${String(account.email || '').toLowerCase()}:${String(account.access_token || '')}`
}

function setPoolPushResult(account, target, result) {
  const key = `${account.id}:${target}`
  poolPushResults.value = {
    ...poolPushResults.value,
    [key]: { ...result, updatedAt: Date.now() },
  }
}

async function executePoolPush(account, target, config, credentialKey) {
  const label = poolTargetLabel(target)
  poolPushStats.queued = Math.max(0, poolPushStats.queued - 1)
  poolPushStats.running += 1
  setPoolPushResult(account, target, { status: 'pushing', message: `${label} 推送中` })
  try {
    const result = target === 'cpa'
      ? await pushToCpa(account, config)
      : await pushToSub2Api(account, config)
    poolPushStats.success += 1
    poolPushStats.lastMessage = `${label}：${result.message}`
    setPoolPushResult(account, target, { status: 'success', message: result.message })
    return true
  } catch (error) {
    pushedCredentials.delete(credentialKey)
    const message = error.message || `${label} 推送失败`
    poolPushStats.failed += 1
    poolPushStats.lastMessage = `${label}：${message}`
    setPoolPushResult(account, target, { status: 'failed', message })
    return false
  } finally {
    poolPushStats.running = Math.max(0, poolPushStats.running - 1)
  }
}

function enqueuePoolPush(account, { auto = false, force = false, targets: requestedTargets = null } = {}) {
  if (auto && !poolPushConfig.autoEnabled) return 0
  const targets = requestedTargets || ['cpa', 'sub2api'].filter((target) => poolTargetConfig(target).enabled)
  let queued = 0
  for (const target of targets) {
    const config = { ...poolTargetConfig(target) }
    const credentialKey = `${pushCredentialKey(target, account)}:${config.url.trim()}`
    if (!force && pushedCredentials.has(credentialKey)) continue
    pushedCredentials.add(credentialKey)
    poolPushStats.queued += 1
    queued += 1
    poolPushQueue = poolPushQueue.then(() => executePoolPush(account, target, config, credentialKey))
  }
  return queued
}

async function pushCurrentAlive() {
  if (poolPushStats.queued || poolPushStats.running) {
    return ElMessage.warning('已有号池推送正在执行，请等待当前队列结束')
  }
  const rows = aliveAccounts.value
  if (!rows.length) return ElMessage.warning('当前没有存活账号可推送')
  if (manualPoolPushConfigIssue.value) return ElMessage.warning(manualPoolPushConfigIssue.value)
  const targets = ['cpa', 'sub2api'].filter((target) => {
    const config = poolTargetConfig(target)
    return config.url.trim() && config.key.trim()
  })
  manualPoolPushing.value = true
  const startStats = { success: poolPushStats.success, failed: poolPushStats.failed }
  let queued = 0
  for (const account of rows) queued += enqueuePoolPush(account, { force: true, targets })
  if (!queued) {
    manualPoolPushing.value = false
    return ElMessage.info('没有需要推送的目标')
  }
  try {
    await poolPushQueue
    const success = poolPushStats.success - startStats.success
    const failed = poolPushStats.failed - startStats.failed
    if (failed) ElMessage.warning(`手动推送完成：成功 ${success}，失败 ${failed}`)
    else ElMessage.success(`手动推送完成：成功 ${success}（存活账号 ${rows.length} 个）`)
  } finally {
    manualPoolPushing.value = false
  }
}

function autoPushRevived(account) {
  if (!poolPushConfig.autoEnabled || poolPushConfigIssue.value) return
  enqueuePoolPush(account, { auto: true })
}

function poolPushStatusFor(row, target) {
  return poolPushResults.value[`${row.id}:${target}`] || null
}

const asObject = (value) => {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
    } catch (_) { return {} }
  }
  return {}
}

const normalizeAccount = (item, idx) => {
  const raw = asObject(item)
  const credentials = asObject(raw.credentials)
  const data = asObject(raw.data)
  const extra = asObject(raw.extra)
  const sources = [credentials, data, raw, extra]
  const first = (...keys) => {
    for (const source of sources) {
      for (const key of keys) {
        if (source[key] !== undefined && source[key] !== null && String(source[key]).trim()) return source[key]
      }
    }
    return ''
  }
  const accessToken = String(first('access_token', 'accessToken', 'access-token', 'token')).trim()
  const tokenPayload = decodeJwtPayload(accessToken)
  const tokenAuth = tokenPayload?.['https://api.openai.com/auth'] || {}
  const tokenProfile = tokenPayload?.['https://api.openai.com/profile'] || {}
  const tokenExp = Number(tokenPayload?.exp)
  const expiresAt = Number.isInteger(tokenExp) && tokenExp > 0
    ? tokenExp
    : (Number(first('expires_at', 'expiresAt')) || 0)
  const email = String(first('email', 'mail', 'username', 'name') || tokenProfile.email || '').trim().toLowerCase()
  const refreshToken = String(first('refresh_token', 'refreshToken')).trim()
  const idToken = String(first('id_token', 'idToken')).trim()
  const idTokenAuth = decodeJwtPayload(idToken)?.['https://api.openai.com/auth'] || {}
  const password = String(first('password', 'passwd')).trim()
  const totpSecret = String(first('totp_secret', 'totpSecret', 'two_factor_secret', 'twoFactorSecret', '2fa')).trim()
  const workspaceId = String(
    first('chatgpt_account_id', 'chatgptAccountId', 'workspace_id', 'workspaceId')
      || tokenAuth.chatgpt_account_id
      || tokenAuth.account_id
      || idTokenAuth.chatgpt_account_id
      || idTokenAuth.account_id
      || first('account_id', 'accountId')
      || '',
  ).trim()
  const userId = String(
    first('chatgpt_user_id', 'chatgptUserId', 'user_id', 'userId')
      || tokenAuth.chatgpt_user_id
      || tokenAuth.user_id
      || '',
  ).trim()
  const clientId = String(first('client_id', 'clientId')).trim()
  const planType = String(first('plan_type', 'planType') || tokenAuth.chatgpt_plan_type || 'team').trim()
  const sessionToken = String(first('session_token', 'sessionToken')).trim()
  const organizationId = String(
    first('organization_id', 'organizationId')
      || tokenAuth.organization_id
      || tokenAuth.poid
      || idTokenAuth.organization_id
      || '',
  ).trim()
  // 从 organizations 数组取第一个 org id 作为 fallback
  const orgFallback = (() => {
    for (const auth of [idTokenAuth, tokenAuth]) {
      if (Array.isArray(auth.organizations)) {
        const org = auth.organizations.find((o) => o && typeof o === 'object' && String(o.id || '').trim())
        if (org) return String(org.id).trim()
      }
    }
    return ''
  })()
  const proxy = String(first('proxy') || '').trim()
  return {
    id: `${workspaceId || 'ws'}:${email || idx}`,
    email,
    access_token: accessToken,
    refresh_token: refreshToken,
    id_token: idToken,
    password,
    totp_secret: totpSecret,
    workspace_id: workspaceId,
    chatgpt_user_id: userId,
    client_id: clientId,
    plan_type: planType || 'team',
    session_token: sessionToken,
    organization_id: organizationId || orgFallback,
    expires_at: expiresAt,
    proxy,
    status: 'unknown',
    quota: null,
    error: '',
    last_checked_at: 0,
  }
}

const parsePayload = (payload) => {
  const root = asObject(payload)
  const nestedData = asObject(root.data)
  let rows = []
  if (Array.isArray(payload)) rows = payload
  else if (Array.isArray(root.accounts)) rows = root.accounts
  else if (Array.isArray(root.items)) rows = root.items
  else if (Array.isArray(root.data)) rows = root.data
  else if (Array.isArray(nestedData.accounts)) rows = nestedData.accounts
  else if (nestedData.credentials || nestedData.access_token || nestedData.accessToken || nestedData.refresh_token || nestedData.refreshToken) rows = [nestedData]
  else if (root.credentials || root.access_token || root.accessToken || root.refresh_token || root.refreshToken || root.type === 'codex') rows = [root]
  if (!rows.length) throw new Error('不支持的导入格式，未找到账号列表（accounts / items / data）')
  return rows.map((a, idx) => normalizeAccount(a, idx))
}

const handleFile = async (event) => {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  loading.value = true
  try {
    rawText.value = await file.text()
    const payload = JSON.parse(rawText.value)
    accounts.value = parsePayload(payload)
    lastResults.value = {}
    stopInspection(false)
    const missingTokenCount = accounts.value.filter((item) => !item.access_token).length
    if (missingTokenCount) {
      ElMessage.warning(`已导入 ${accounts.value.length} 个账号，其中 ${missingTokenCount} 个缺少 access_token，无法查询额度`)
    } else {
      ElMessage.success(`已导入 ${accounts.value.length} 个账号，已解析 access_token`)
    }
  } catch (e) {
    ElMessage.error(e.message || '导入失败')
  } finally {
    loading.value = false
  }
}

const updateOne = (targetId, patch) => {
  accounts.value = accounts.value.map((item) => (item.id === targetId ? { ...item, ...patch } : item))
}

const applyCheckResult = (item, result, checkedAt = Date.now()) => {
  if (!result) return
  lastResults.value = { ...lastResults.value, [item.id]: result }
  if (result.status === 'revived' && result.account) {
    const refreshed = normalizeAccount(result.account, 0)
    const revivedAccount = {
      ...refreshed,
      id: item.id,
      proxy: item.proxy || refreshed.proxy,
      status: 'revived',
      error: '',
      last_checked_at: checkedAt,
    }
    updateOne(item.id, revivedAccount)
    autoPushRevived(revivedAccount)
    return
  }
  updateOne(item.id, {
    status: result.status === 'active' ? 'active' : result.status,
    email: result.email || item.email,
    workspace_id: result.workspace_id || item.workspace_id,
    quota: result.quota || null,
    error: result.error || '',
    last_checked_at: checkedAt,
  })
}

const checkAccounts = async (items, notify = true, autoReloginOn401 = false) => {
  if (!items.length) return false
  if (checking.value || relogining.value) return false
  const key = requireAccessKey()
  if (!key) return false
  checking.value = true
  checkProgress.value = { done: 0, total: items.length }
  const ids = new Set(items.map((item) => item.id))
  accounts.value = accounts.value.map((item) => ({
    ...item,
    ...(ids.has(item.id) ? { status: 'checking', quota: null, error: '' } : {}),
  }))
  const checkedAt = Date.now()
  try {
    // 每个账号单独提交一个 HTTP 请求；后端的全局额度队列负责并发执行。
    // 这样不会让数百条账号共用一个请求生命周期，也能逐账号显示真实错误。
    const plainItems = JSON.parse(JSON.stringify(items)).map((item) => {
      const { account: _unusedAccount, ...rest } = item
      return rest
    })
    console.debug('[public-relogin] submit per-account quota checks', {
      count: plainItems.length,
      withAccessToken: plainItems.filter((item) => Boolean(item.access_token)).length,
    })
    let queueFullCount = 0
    let errorCount = 0
    let firstError = null
    const processResult = (original, result, queues) => {
      // 单个请求一返回就立即更新对应行，不等待其它账号。
      applyCheckResult(original, result, checkedAt)
      if (queues) queueStatus.value = queues
      if (result?.status === 'queue_full') queueFullCount += 1
      if (result?.status === 'error' || result?.status === 'failed') errorCount += 1
    }
    const responses = await Promise.all(plainItems.map(async (plainItem, index) => {
      const original = items[index]
      try {
        const response = await checkPublicRelogin({
          accounts: [plainItem],
          access_key: key,
          concurrency: 0,
          proxy_pool: proxyStore.text,
          auto_relogin_on_401: autoReloginOn401,
        })
        const result = response.results?.[plainItem.id]
          || Object.values(response.results || {})[0]
          || {
            ok: false,
            status: 'error',
            email: original.email,
            workspace_id: original.workspace_id,
            error: '服务器未返回该账号的巡检结果',
          }
        processResult(original, result, response.queues)
        return { original, result, queues: response.queues }
      } catch (error) {
        handleAuthError(error)
        const result = {
          ok: false,
          status: error.status === 401 ? '401' : error.status === 429 ? 'queue_full' : 'error',
          email: original.email,
          workspace_id: original.workspace_id,
          error: error.message || '额度检查失败',
        }
        if (!firstError && error.status !== 429) firstError = error
        processResult(original, result)
        return {
          original,
          result,
          error,
        }
      } finally {
        checkProgress.value = { ...checkProgress.value, done: checkProgress.value.done + 1 }
      }
    }))
    if (queueFullCount) {
      ElMessage.warning(`${queueFullCount} 个账号未能进入 401 重登录队列，请稍后重试`)
    }
    if (firstError && notify && errorCount === items.length) {
      ElMessage.error(firstError.message || '额度检查失败')
    }
    if (notify) ElMessage.success(`额度检查完成，共 ${items.length} 个`)
    return true
  } catch (e) {
    handleAuthError(e)
    if (e.status === 429) {
      for (const item of items) {
        updateOne(item.id, {
          status: item.status,
          quota: item.quota,
          error: e.message || '额度查询队列已满，请稍后重试',
        })
      }
      ElMessage.warning(e.message || '额度查询队列已满，请稍后重试')
      loadQueueStatus()
      return false
    }
    for (const item of items) {
      applyCheckResult(item, {
          ok: false,
          status: e.status === 401 ? '401' : 'error',
          email: item.email,
          workspace_id: item.workspace_id,
          error: e.message || '额度检查失败',
      }, checkedAt)
      checkProgress.value = { ...checkProgress.value, done: checkProgress.value.done + 1 }
    }
    if (notify) ElMessage.error(e.message)
    return false
  } finally {
    checking.value = false
  }
}

const doCheck = async () => {
  const rows = checkableAccounts.value
  if (!rows.length) return ElMessage.warning('没有可检测的账号（停用账号已过滤）')
  await checkAccounts([...rows])
}

function clearInspectionTimer() {
  if (inspectionTimer) clearTimeout(inspectionTimer)
  inspectionTimer = null
  inspectionNextAt.value = 0
}

function scheduleInspection(delayMs) {
  clearInspectionTimer()
  if (!inspectionRunning.value) return
  inspectionNextAt.value = Date.now() + delayMs
  inspectionTimer = setTimeout(runInspectionBatch, delayMs)
}

async function runInspectionBatch() {
  if (!inspectionRunning.value) return
  if (checking.value || relogining.value) {
    scheduleInspection(5000)
    return
  }
  const candidates = checkableAccounts.value
  if (!candidates.length) {
    stopInspection()
    return
  }
  const size = Math.min(
    Math.max(1, Number(inspectionBatchSize.value) || 8),
    candidates.length,
  )
  const batch = candidates
    .map((item, index) => ({ item, index }))
    .sort((a, b) => (a.item.last_checked_at || 0) - (b.item.last_checked_at || 0) || a.index - b.index)
    .slice(0, size)
    .map(({ item }) => item)
  const completed = await checkAccounts(batch, false, true)
  if (!accessKey.value.trim()) {
    stopInspection(false)
    return
  }
  if (completed) {
    inspectionRound.value += 1
    inspectionLastBatch.value = batch.length
  }
  if (inspectionRunning.value) {
    scheduleInspection(Math.max(1, Number(inspectionIntervalMinutes.value) || 5) * 60000)
  }
}

function startInspection() {
  if (!checkableAccounts.value.length) return ElMessage.warning('没有可巡检的账号（停用账号已过滤）')
  if (!requireAccessKey()) return
  inspectionRunning.value = true
  inspectionRound.value = 0
  inspectionLastBatch.value = 0
  clearInspectionTimer()
  ElMessage.success('定时巡检已启动')
  runInspectionBatch()
}

function stopInspection(notify = true) {
  inspectionRunning.value = false
  clearInspectionTimer()
  if (notify) ElMessage.info('定时巡检已停止')
}

function formatCheckedAt(value) {
  return value ? new Date(value).toLocaleTimeString('zh-CN', { hour12: false }) : '未检查'
}

const doRelogin = async (onlyRevived = true) => {
  const list = accounts.value.filter((item) => (
    item.status !== 'deactivated'
    && (item.status === '401' || !onlyRevived)
  ))
  if (!list.length) return ElMessage.warning('没有可重新登录的账号')
  const key = requireAccessKey()
  if (!key) return
  relogining.value = true
  reloginProgress.value = { done: 0, total: list.length }
  const targetIds = new Set(list.map((item) => item.id))
  let revived = 0
  accounts.value = accounts.value.map((item) => (
    targetIds.has(item.id)
      ? { ...item, status: 'relogging', error: '' }
      : item
  ))
  try {
    // 将 Vue 响应式代理转为纯 JS 对象，避免序列化异常
    const plainList = JSON.parse(JSON.stringify(list))
    // 每个账号单独提交一个 HTTP 请求；后端的全局重登队列负责并发执行。
    // 这样每个账号完成时立即更新 UI，不用等所有账号跑完。
    console.debug('[public-relogin] submit per-account relogin', {
      count: plainList.length,
    })
    const applyReloginResult = (item, result) => {
      lastResults.value = { ...lastResults.value, [item.id]: result }
      if (result.status === 'revived' && result.account) {
        revived += 1
        const refreshed = normalizeAccount(result.account, 0)
        const revivedAccount = {
          ...refreshed,
          id: item.id,
          proxy: item.proxy || refreshed.proxy,
          status: 'revived',
          error: '',
        }
        updateOne(item.id, revivedAccount)
        autoPushRevived(revivedAccount)
      } else {
        updateOne(item.id, {
          status: result.status || 'failed',
          error: result.error || '',
        })
      }
    }
    await Promise.all(plainList.map(async (plainItem, index) => {
      const original = list[index]
      try {
        const res = await runPublicRelogin({
          accounts: [plainItem],
          access_key: key,
          concurrency: 0,
          proxy_pool: proxyStore.text,
        })
        const result = res.results?.[plainItem.id]
          || Object.values(res.results || {})[0]
          || {
            ok: false,
            status: 'failed',
            email: original.email,
            workspace_id: original.workspace_id,
            error: '服务器未返回重登录结果',
          }
        applyReloginResult(original, result)
        if (res.queues) queueStatus.value = res.queues
      } catch (e) {
        handleAuthError(e)
        const result = {
          ok: false,
          status: e.status === 401 ? '401' : e.status === 429 ? 'queue_full' : 'failed',
          email: original.email,
          workspace_id: original.workspace_id,
          error: e.message || '重登录失败',
        }
        applyReloginResult(original, result)
      } finally {
        reloginProgress.value = { ...reloginProgress.value, done: reloginProgress.value.done + 1 }
      }
    }))
    ElMessage.success(`重登完成，成功 ${revived} 个`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    relogining.value = false
    loadQueueStatus()
  }
}

const buildSub2Export = (rows) => {
  const accountsOut = rows.map((item) => {
    const workspaceId = item.workspace_id || ''
    const expiresAt = item.expires_at || 0
    const expiresIn = Math.max(0, Math.floor(expiresAt - Date.now() / 1000))
    const tokenPayload = decodeJwtPayload(item.access_token || '')
    const tokenAuth = tokenPayload?.['https://api.openai.com/auth'] || {}
    const tokenProfile = tokenPayload?.['https://api.openai.com/profile'] || {}
    const accountId = tokenAuth.chatgpt_account_id || tokenAuth.account_id || workspaceId
    const userId = item.chatgpt_user_id || tokenAuth.chatgpt_user_id || tokenAuth.user_id || tokenPayload.sub || ''
    const planType = item.plan_type || tokenAuth.chatgpt_plan_type || 'free'
    const exportedAt = new Date().toISOString()
    const expired = expiresAt ? new Date(expiresAt * 1000).toISOString() : ''
    const accountUserId = tokenAuth.chatgpt_account_user_id || (userId && accountId ? `${userId}__${accountId}` : '')
    const displayName = tokenProfile.name || item.email || ''
    const liveIdentity = {
      plan: planType,
      email: item.email || tokenProfile.email || '',
      user_id: userId,
    client_id: tokenPayload.client_id || item.client_id || '',
      account_id: accountId,
      plan_source: 'oauth_access_token_claim',
      verified_at: exportedAt,
      email_source: 'oauth_userinfo_email',
      official_plan: planType,
      client_trusted: false,
      email_verified: tokenProfile.email_verified !== false,
      user_id_source: 'oauth_access_token_claim',
      account_user_id: accountUserId,
      identity_source: 'oauth_access_token_claim',
      account_id_source: 'oauth_access_token_claim',
      account_user_id_source: 'oauth_access_token_claim',
    }
    const credExtra = {
      email: item.email || tokenProfile.email || '',
      source: 'internal_resource_exchange',
      privacy_mode: 'training_off',
      original_format: 'codex-account',
      openai_oauth_responses_websockets_v2_mode: 'off',
      openai_oauth_responses_websockets_v2_enabled: false,
    }
    return {
      name: item.email,
      type: 'oauth',
      extra: credExtra,
      platform: 'openai',
      priority: 1,
      plan_type: planType,
      concurrency: 10,
      credentials: {
        name: displayName,
        type: 'codex',
        extra: credExtra,
        expired,
        disabled: false,
        access_token: item.access_token || '',
        email: item.email || '',
        password: item.password || '',
        totp_secret: item.totp_secret || '',
        id_token: item.id_token || '',
        client_id: tokenPayload.client_id || item.client_id || '',
        plan_type: planType,
        account_id: accountId,
        email_source: 'oauth_userinfo_email',
        last_refresh: exportedAt,
        workspace_id: accountId,
        live_identity: liveIdentity,
        outlook_email: item.email || '',
        refresh_token: item.refresh_token || '',
        session_token: item.session_token || '',
        expires_in: expiresIn,
        organization_id: item.organization_id || '',
        chatgpt_user_id: userId,
        identity_source: 'oauth_access_token_claim',
        account_id_source: 'oauth_access_token_claim',
        chatgpt_plan_type: planType,
        chatgpt_account_id: accountId,
        chatgpt_account_user_id: accountUserId,
        expires_at: expired,
      },
      group_ids: [4],
      auto_pause_on_expired: true,
      expires_at: expiresAt,
    }
  })
  return {
    type: 'sub2api-data',
    version: 1,
    exported_at: new Date().toISOString(),
    proxies: [],
    accounts: accountsOut,
  }
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

function crc32(bytes) {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0)
  }
  return (crc ^ 0xffffffff) >>> 0
}

function concatBytes(chunks) {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const output = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) { output.set(chunk, offset); offset += chunk.length }
  return output
}

function makeZip(entries) {
  const encoder = new TextEncoder()
  const local = []
  const central = []
  let offset = 0
  for (const entry of entries) {
    const name = encoder.encode(entry.name)
    const data = encoder.encode(entry.content)
    const checksum = crc32(data)
    const header = new Uint8Array(30 + name.length)
    const view = new DataView(header.buffer)
    view.setUint32(0, 0x04034b50, true)
    view.setUint16(4, 20, true)
    view.setUint16(6, 0x800, true)
    view.setUint16(8, 0, true)
    view.setUint32(14, checksum, true)
    view.setUint32(18, data.length, true)
    view.setUint32(22, data.length, true)
    view.setUint16(26, name.length, true)
    header.set(name, 30)
    local.push(header, data)

    const directory = new Uint8Array(46 + name.length)
    const directoryView = new DataView(directory.buffer)
    directoryView.setUint32(0, 0x02014b50, true)
    directoryView.setUint16(4, 20, true)
    directoryView.setUint16(6, 20, true)
    directoryView.setUint16(8, 0x800, true)
    directoryView.setUint32(16, checksum, true)
    directoryView.setUint32(20, data.length, true)
    directoryView.setUint32(24, data.length, true)
    directoryView.setUint16(28, name.length, true)
    directoryView.setUint32(42, offset, true)
    directory.set(name, 46)
    central.push(directory)
    offset += header.length + data.length
  }
  const centralBytes = concatBytes(central)
  const end = new Uint8Array(22)
  const endView = new DataView(end.buffer)
  endView.setUint32(0, 0x06054b50, true)
  endView.setUint16(8, entries.length, true)
  endView.setUint16(10, entries.length, true)
  endView.setUint32(12, centralBytes.length, true)
  endView.setUint32(16, offset, true)
  return new Blob([concatBytes([...local, centralBytes, end])], { type: 'application/zip' })
}

async function buildCpaExport(rows) {
  const entries = []
  const filenameCounts = new Map()
  for (let index = 0; index < rows.length; index++) {
    const item = await buildCpaTokenJson(rows[index])
    const data = { ...item, disabled: false }
    const baseName = String(data.email || rows[index].email || `account-${index + 1}`).replace(/[\\/:*?"<>|]/g, '_') || `account-${index + 1}`
    const occurrence = (filenameCounts.get(baseName) || 0) + 1
    filenameCounts.set(baseName, occurrence)
    const filename = occurrence === 1 ? `${baseName}.json` : `${baseName}-${occurrence}.json`
    entries.push({ name: filename, content: JSON.stringify(data, null, 2) })
  }
  if (entries.length === 1) return { blob: new Blob([entries[0].content], { type: 'application/json' }), filename: entries[0].name }
  return { blob: makeZip(entries), filename: `cpa-accounts-${new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14)}.zip` }
}

function buildPassword2faExport(rows) {
  const text = rows.map((item) => `${item.email || ''}----${item.password || ''}----${item.totp_secret || ''}`).join('\n') + '\n'
  return { blob: new Blob([text], { type: 'text/plain;charset=utf-8' }), filename: `accounts-password-2fa-${rows.length}.txt` }
}

async function refreshRowsForExport(rows) {
  const key = requireAccessKey()
  if (!key) throw new Error('缺少公开重登访问密钥')
  const refreshed = new Array(rows.length)
  const errors = []
  let cursor = 0
  const proxies = proxyStore.list.map((value) => String(value || '').trim()).filter(Boolean)
  const workerCount = Math.min(10, rows.length)
  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (cursor < rows.length) {
      const index = cursor
      cursor += 1
      const original = rows[index]
      try {
        const plain = JSON.parse(JSON.stringify(original))
        const response = await refreshPublicReloginExport({
          account: plain,
          access_key: key,
          proxy: proxies.length ? proxies[index % proxies.length] : '',
        })
        const normalized = normalizeAccount(response.account || {}, index)
        refreshed[index] = {
          ...original,
          ...normalized,
          id: original.id,
          status: original.status,
          quota: original.quota,
          error: original.error,
          last_checked_at: original.last_checked_at,
        }
        updateOne(original.id, refreshed[index])
      } catch (error) {
        errors.push(`${original.email || `第 ${index + 1} 条`}: ${error.message || '刷新失败'}`)
      }
    }
  }))
  if (errors.length) {
    throw new Error(`有 ${errors.length} 个账号未能生成有效导出凭证：${errors.slice(0, 3).join('；')}`)
  }
  return refreshed
}

const download = async (format = 'sub2api', mode = 'all') => {
  const filtered = aliveAccounts.value
  const rows = mode === 'revived' ? filtered.filter((item) => item.status === 'revived') : filtered
  if (!rows.length) return ElMessage.warning('没有可下载的账号')
  if (downloading.value) return ElMessage.warning('导出正在生成，请稍候')
  downloading.value = true
  try {
    if (format === 'password2fa') {
      const result = buildPassword2faExport(rows)
      downloadBlob(result.blob, result.filename)
      ElMessage.success(`账号----密码----2FA 导出完成，共 ${rows.length} 个`)
    } else {
      // CPA 文件可直接使用当前账号凭证；Sub2 导出仍刷新并校验 AT/RT/ID 一致性。
      const exportRows = format === 'cpa' ? rows : await refreshRowsForExport(rows)
      const result = format === 'cpa'
        ? await buildCpaExport(exportRows)
        : { blob: new Blob([JSON.stringify(buildSub2Export(exportRows), null, 2)], { type: 'application/json' }), filename: mode === 'revived' ? 'sub2api-revived.json' : `sub2api-accounts-remaining-${exportRows.length}.json` }
      downloadBlob(result.blob, result.filename)
      ElMessage.success(`${format === 'cpa' ? 'CPA' : 'Sub2'} 凭证刷新并下载完成，共 ${exportRows.length} 个`)
    }
  } catch (error) {
    handleAuthError(error)
    ElMessage.error(error.message || 'Sub2 导出失败')
  } finally {
    downloading.value = false
  }
}

watch(accessKey, (value) => {
  const key = String(value || '').trim()
  if (key) localStorage.setItem(ACCESS_KEY_CACHE, key)
  else localStorage.removeItem(ACCESS_KEY_CACHE)
})

watch([inspectionBatchSize, inspectionIntervalMinutes], ([batchSize, interval]) => {
  localStorage.setItem(INSPECTION_SETTINGS_CACHE, JSON.stringify({ batchSize, interval }))
})

watch(poolPushConfig, savePoolPushConfig, { deep: true })

onMounted(() => {
  accessKey.value = localStorage.getItem(ACCESS_KEY_CACHE) || ''
  try {
    const saved = JSON.parse(localStorage.getItem(INSPECTION_SETTINGS_CACHE) || '{}')
    inspectionBatchSize.value = Math.max(1, Number(saved.batchSize) || 8)
    inspectionIntervalMinutes.value = Math.max(1, Number(saved.interval) || 5)
  } catch (_) { /* 使用默认值 */ }
  loadPoolPushConfig()
  clockTimer = setInterval(() => { clockNow.value = Date.now() }, 1000)
  loadQueueStatus()
  queueStatusTimer = setInterval(loadQueueStatus, 5000)
})

onBeforeUnmount(() => {
  clearInspectionTimer()
  if (clockTimer) clearInterval(clockTimer)
  if (queueStatusTimer) clearInterval(queueStatusTimer)
})
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <span class="section-title" style="margin:0">公开 401 重登录</span>
      </template>
      <p class="hint">导入 sub2api / cpa JSON 后，在浏览器内完成解析、检查 401、密码 + 2FA 重登录和下载。</p>

      <el-alert
        type="info"
        show-icon
        :closable="false"
        style="margin-bottom: 12px"
        title="访问密钥会缓存在当前浏览器本地；后端只接受有效且未过期的密钥。"
      />
      <el-input
        v-model="accessKey"
        type="password"
        show-password
        clearable
        placeholder="公开重登访问密钥"
        style="max-width: 520px; margin-bottom: 12px"
      />

      <div class="toolbar">
        <input ref="fileInput" type="file" accept="application/json,.json" hidden @change="handleFile" />
        <el-button :loading="loading" @click="openFile">导入 JSON</el-button>
        <el-button type="warning" @click="import2faVisible = true">导入 2FA 账号</el-button>
        <el-button :loading="checking" type="primary" @click="doCheck">检查额度 / 401</el-button>
        <el-button :loading="relogining" type="success" @click="doRelogin(true)">一键重新登录</el-button>
        <el-button @click="poolPushDrawerVisible = true">
          <el-icon><Upload /></el-icon>
          自动号池推送
        </el-button>
        <el-button :type="aliveOnly ? 'primary' : 'default'" @click="aliveOnly = !aliveOnly">
          仅查看存活账号
        </el-button>
        <el-dropdown>
          <el-button :loading="downloading">
            下载
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="download('cpa', 'all')">导出 CPA 格式（存活账号）</el-dropdown-item>
              <el-dropdown-item @click="download('sub2api', 'all')">导出 Sub2API 格式（存活账号）</el-dropdown-item>
              <el-dropdown-item @click="download('password2fa', 'all')">导出 账号----密码----2FA（存活账号）</el-dropdown-item>
              <el-dropdown-item divided @click="download('cpa', 'revived')">导出 CPA 格式（仅复活项）</el-dropdown-item>
              <el-dropdown-item @click="download('sub2api', 'revived')">导出 Sub2API 格式（仅复活项）</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>

      <div class="pool-push-strip">
        <el-tag :type="poolPushConfig.autoEnabled ? 'success' : 'info'" size="small">
          自动推送{{ poolPushConfig.autoEnabled ? '已开启' : '未开启' }}
        </el-tag>
        <el-tag
          v-if="poolPushConfig.autoEnabled && poolPushConfigIssue"
          type="danger"
          size="small"
          effect="plain"
        >{{ poolPushConfigIssue }}</el-tag>
        <span>目标：{{ enabledPoolTargetNames.join(' + ') || '未配置' }}</span>
        <span v-if="poolPushStats.queued || poolPushStats.running">
          等待 {{ poolPushStats.queued }}，推送中 {{ poolPushStats.running }}
        </span>
        <span>成功 {{ poolPushStats.success }}，失败 {{ poolPushStats.failed }}</span>
        <span>配置仅存当前浏览器，浏览器直接连接号池</span>
        <span v-if="poolPushStats.lastMessage" class="pool-push-last" :title="poolPushStats.lastMessage">
          {{ poolPushStats.lastMessage }}
        </span>
      </div>

      <div class="inspection-bar">
        <el-form-item label="巡检批次" style="margin: 0">
          <el-input-number
            v-model="inspectionBatchSize"
            :min="1"
            :max="200"
            controls-position="right"
          />
        </el-form-item>
        <el-form-item label="巡检周期（分钟）" style="margin: 0">
          <el-input-number
            v-model="inspectionIntervalMinutes"
            :min="1"
            :max="1440"
            controls-position="right"
          />
        </el-form-item>
        <el-button
          v-if="!inspectionRunning"
          type="primary"
          plain
          :disabled="!checkableAccounts.length"
          @click="startInspection"
        >启动定时巡检</el-button>
        <el-button v-else type="danger" plain @click="stopInspection()">停止定时巡检</el-button>
        <div class="inspection-status">
          <el-tag :type="inspectionRunning ? 'success' : 'info'">
            {{ inspectionRunning ? '运行中' : '未运行' }}
          </el-tag>
          <span v-if="inspectionRunning">
            下一轮 {{ inspectionCountdown }}，已完成 {{ inspectionRound }} 批，上一批 {{ inspectionLastBatch }} 个；发现 401 立即复活
          </span>
          <span v-else>启动后立即检查最久未检查的一批</span>
        </div>
      </div>

      <div class="queue-strip">
        <div v-for="item in queueItems" :key="item.name" class="queue-item">
          <el-tag :type="item.full ? 'danger' : item.waiting ? 'warning' : 'success'" size="small">
            {{ item.name }}{{ item.full ? ' · 已满' : '' }}
          </el-tag>
          <span>运行 {{ item.running }}/{{ item.concurrency }}</span>
          <span>等待 {{ item.waiting }}/{{ item.capacity }}</span>
          <span>可接收 {{ item.available }}</span>
        </div>
      </div>

      <div class="hint" style="margin-top: 8px">
        当前会随请求复用系统代理池：{{ proxyStore.count }} 条；单账号代理不为空时优先使用单账号代理。
      </div>

      <el-alert
        style="margin-top: 12px"
        type="warning"
        show-icon
        :closable="false"
        title="公开页面不会把导入账号写入后端数据库；只在点击检查/重登时把当前账号对象发给后端处理。"
      />

      <el-descriptions :column="5" border style="margin-top: 16px">
        <el-descriptions-item label="总计">{{ statusCount.total }}</el-descriptions-item>
        <el-descriptions-item label="活跃">{{ statusCount.active }}</el-descriptions-item>
        <el-descriptions-item label="401">{{ statusCount.unauthorized }}</el-descriptions-item>
        <el-descriptions-item label="复活">{{ statusCount.revived }}</el-descriptions-item>
        <el-descriptions-item label="停用">{{ statusCount.deactivated }}</el-descriptions-item>
      </el-descriptions>
      <el-progress
        v-if="checking"
        style="margin-top: 12px"
        :percentage="checkProgress.total ? Math.round((checkProgress.done / checkProgress.total) * 100) : 0"
        :format="() => `${checkProgress.done}/${checkProgress.total}`"
      />
      <el-progress
        v-if="relogining"
        style="margin-top: 12px"
        status="success"
        :percentage="reloginProgress.total ? Math.round((reloginProgress.done / reloginProgress.total) * 100) : 0"
        :format="() => `重登录 ${reloginProgress.done}/${reloginProgress.total}`"
      />

      <el-table :data="visibleAccounts" style="margin-top: 16px" height="640" row-key="id">
        <el-table-column prop="email" label="邮箱" min-width="220" />
        <el-table-column label="密码" min-width="180">
          <template #default="{ row }">
            <el-input v-model="row.password" size="small" show-password placeholder="password" @change="updateOne(row.id, { password: row.password })" />
          </template>
        </el-table-column>
        <el-table-column label="2FA" min-width="180">
          <template #default="{ row }">
            <el-input v-model="row.totp_secret" size="small" placeholder="totp secret" @change="updateOne(row.id, { totp_secret: row.totp_secret })" />
          </template>
        </el-table-column>
        <el-table-column label="代理" min-width="220">
          <template #default="{ row }">
            <el-input v-model="row.proxy" size="small" placeholder="代理，可空" @change="updateOne(row.id, { proxy: row.proxy })" />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="140">
          <template #default="{ row }">
            <el-tag v-if="row.status === 'checking'" type="info">
              <el-icon class="is-loading"><Loading /></el-icon>
              检查中
            </el-tag>
            <el-tag v-else-if="row.status === 'relogging'" type="info">
              <el-icon class="is-loading"><Loading /></el-icon>
              重登录中
            </el-tag>
            <el-tag v-else-if="row.status === 'revived'" type="success">复活</el-tag>
            <el-tag v-else-if="row.status === '401'" type="warning">401</el-tag>
            <el-tag v-else-if="row.status === 'deactivated'" type="danger">停用</el-tag>
            <el-tag v-else-if="row.status === 'active'" type="success" effect="plain">正常</el-tag>
            <el-tag v-else-if="row.status === 'error'" type="danger" effect="plain">错误</el-tag>
            <el-tag v-else-if="row.status === 'failed'" type="danger" effect="plain">重登录失败</el-tag>
            <el-tag v-else-if="row.status === 'queue_full'" type="danger" effect="plain">队列已满</el-tag>
            <el-tag v-else type="info" effect="plain">未知</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近检查" width="110">
          <template #default="{ row }">
            <span class="hint">{{ formatCheckedAt(row.last_checked_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="号池推送" min-width="180">
          <template #default="{ row }">
            <div v-if="poolPushStatusFor(row, 'cpa') || poolPushStatusFor(row, 'sub2api')" class="push-result-list">
              <el-tag
                v-for="target in ['cpa', 'sub2api']"
                v-show="poolPushStatusFor(row, target)"
                :key="target"
                :type="poolPushStatusFor(row, target)?.status === 'success' ? 'success' : poolPushStatusFor(row, target)?.status === 'failed' ? 'danger' : 'info'"
                size="small"
                effect="plain"
                :title="poolPushStatusFor(row, target)?.message"
              >
                {{ poolTargetLabel(target) }} · {{ poolPushStatusFor(row, target)?.status === 'success' ? '成功' : poolPushStatusFor(row, target)?.status === 'failed' ? '失败' : '推送中' }}
              </el-tag>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="额度" min-width="220">
          <template #default="{ row }">
            <span v-if="row.quota">{{ row.quota.credits_balance ?? row.quota.primary?.used_percent ?? '-' }}</span>
            <span v-else>{{ row.error || '-' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer
      v-model="poolPushDrawerVisible"
      title="浏览器自动号池推送"
      size="min(620px, 94vw)"
      append-to-body
    >
      <el-alert
        type="success"
        show-icon
        :closable="false"
        title="号池地址与密钥仅保存在当前浏览器；推送由浏览器直接连接 CPA / Sub2API。本项目服务器不会接收、保存或转发这些配置。"
      />
      <el-alert
        class="pool-push-alert"
        type="warning"
        show-icon
        :closable="false"
        title="目标号池必须允许跨域请求（CORS）及对应鉴权请求头；本页面为 HTTPS 时只能连接 HTTPS 号池。"
      />

      <div class="pool-auto-row">
        <div>
          <div class="pool-setting-title">复活后自动推送</div>
          <div class="hint">手动重登录和定时巡检复活成功后立即进入浏览器推送队列。</div>
        </div>
        <el-switch v-model="poolPushConfig.autoEnabled" />
      </div>
      <el-alert
        v-if="poolPushConfig.autoEnabled && poolPushConfigIssue"
        class="pool-push-alert"
        type="error"
        show-icon
        :closable="false"
        :title="poolPushConfigIssue"
      />

      <section class="pool-target-section">
        <div class="pool-target-header">
          <el-checkbox v-model="poolPushConfig.cpa.enabled">启用 CPA</el-checkbox>
          <span class="hint">POST /v0/management/auth-files</span>
        </div>
        <el-form label-position="top">
          <el-form-item label="CPA URL">
            <el-input v-model="poolPushConfig.cpa.url" placeholder="https://cpa.example.com" clearable />
          </el-form-item>
          <el-form-item label="管理密钥（Authorization Bearer + X-Management-Key）">
            <el-input v-model="poolPushConfig.cpa.key" type="password" show-password clearable placeholder="CPA 管理密钥" />
          </el-form-item>
          <div class="pool-target-actions">
            <el-form-item label="请求超时（秒）" style="margin-bottom: 0">
              <el-input-number v-model="poolPushConfig.cpa.timeout" :min="5" :max="300" controls-position="right" />
            </el-form-item>
            <el-button :loading="testingPoolTarget === 'cpa'" @click="testPoolTarget('cpa')">测试浏览器直连</el-button>
          </div>
        </el-form>
      </section>

      <section class="pool-target-section">
        <div class="pool-target-header">
          <el-checkbox v-model="poolPushConfig.sub2api.enabled">启用 Sub2API</el-checkbox>
          <span class="hint">POST /api/v1/admin/accounts</span>
        </div>
        <el-form label-position="top">
          <el-form-item label="Sub2API URL">
            <el-input v-model="poolPushConfig.sub2api.url" placeholder="https://sub2api.example.com" clearable />
          </el-form-item>
          <el-form-item label="API Key（x-api-key）">
            <el-input v-model="poolPushConfig.sub2api.key" type="password" show-password clearable placeholder="Sub2API API Key" />
          </el-form-item>
          <el-form-item label="分组 IDs（逗号分隔）">
            <el-input v-model="poolPushConfig.sub2api.groupIds" placeholder="2" clearable />
          </el-form-item>
          <div class="pool-target-actions">
            <el-form-item label="请求超时（秒）" style="margin-bottom: 0">
              <el-input-number v-model="poolPushConfig.sub2api.timeout" :min="5" :max="300" controls-position="right" />
            </el-form-item>
            <el-button :loading="testingPoolTarget === 'sub2api'" @click="testPoolTarget('sub2api')">测试浏览器直连</el-button>
          </div>
        </el-form>
      </section>

      <div class="pool-drawer-actions">
        <el-button type="danger" plain @click="clearPoolPushConfig">清除本地配置</el-button>
        <el-button
          type="primary"
          :loading="manualPoolPushing"
          :disabled="Boolean(manualPoolPushConfigIssue) || Boolean(poolPushStats.queued || poolPushStats.running)"
          @click="pushCurrentAlive"
        >手动推送存活账号</el-button>
      </div>
    </el-drawer>

    <!-- 导入 2FA 账号对话框 -->
    <el-dialog v-model="import2faVisible" title="导入 2FA 账号" width="600px" destroy-on-close>
      <el-alert type="info" show-icon :closable="false" style="margin-bottom:12px">
        <template #title>每行一个，格式：<code>邮箱----密码----2FA</code>（2FA 可选）</template>
        <p style="margin:0">以 <code>#</code> 开头的行视为注释。如有任何一行格式不对，全部拒绝。</p>
      </el-alert>
      <el-input
        v-model="import2faText"
        type="textarea"
        :rows="10"
        placeholder="user@example.com----P@ssw0rd----JBSWY3DPEHPK3PXP"
      />
      <div style="text-align:right;margin-top:4px;color:var(--el-text-color-secondary);font-size:12px">
        有效行：{{ import2faLineCount }}
      </div>
      <div v-if="import2faErrors.length" style="margin-top:8px">
        <el-alert type="error" :closable="false" show-icon title="以下行不合法">
          <ul class="err-list">
            <li v-for="e in import2faErrors" :key="e.line">第 {{ e.line }} 行：{{ e.error }}</li>
          </ul>
        </el-alert>
      </div>
      <template #footer>
        <el-button @click="import2faVisible = false">取消</el-button>
        <el-button type="primary" @click="doImport2FA">导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { padding: 16px; }
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; }
.pool-push-strip {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  min-height: 34px;
  padding-top: 10px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.pool-push-last {
  max-width: min(520px, 100%);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.inspection-bar {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding: 12px 0;
  border-top: 1px solid var(--el-border-color-light);
  border-bottom: 1px solid var(--el-border-color-light);
}
.inspection-status {
  min-height: 32px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.queue-strip {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
  min-height: 34px;
  padding-top: 10px;
}
.queue-item {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.push-result-list { display: flex; gap: 6px; flex-wrap: wrap; }
.pool-push-alert { margin-top: 12px; }
.pool-auto-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 0 14px;
  border-bottom: 1px solid var(--el-border-color-light);
}
.pool-setting-title { font-weight: 600; }
.pool-target-section {
  padding: 18px 0;
  border-bottom: 1px solid var(--el-border-color-light);
}
.pool-target-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.pool-target-actions {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.pool-drawer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 18px;
}
.err-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12px;
  max-height: 160px;
  overflow-y: auto;
}
</style>
