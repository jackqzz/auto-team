<script setup>
import { computed, nextTick, onActivated, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Icon } from '@iconify/vue'
import {
  listRegistered, getRegistered, deleteRegistered,
  bulkDeleteRegistered, bulkDeleteAccounts, checkPlus,
  listExportFormats, exportRegistered, updateCredentials,
  importSub2Api, import2FA, pushRegisteredToCpa,
  autoStart,
} from '@/api/register'
import {
  setAccountsGroup, createAccountGroup, renameAccountGroup, deleteAccountGroup,
} from '@/api/accounts'
import { copyText, fmtTime } from '@/api/request'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import { listWorkspaceMasters } from '@/api/workspaces'
import { assignCandidates } from '@/api/workspaceCandidates'
import StatusDot from '@/components/StatusDot.vue'
import { PAGE_SIZE_OPTIONS, SELECT_ALL_FETCH_LIMIT } from '@/utils/pagination'

const { form } = storeToRefs(useFormStore())
// 检测用的代理必须能从代理池里挑：以前这页只在代码里读 form.proxy，页面上
// 连个输入框都没有，主人在代理池换了密码，这里还在用 localStorage 里的旧值，
// 结果是 curl:(97) 代理鉴权被拒 → 静默降级直连 → 拿真实 IP 打 chatgpt.com。
const { list: proxyList } = storeToRefs(useProxyStore())
const runtime = useRuntimeStore()
// dataVersion 要走 storeToRefs 才保持响应（watch 用）；bumpData 是 action，直接从
// store 实例上取 —— storeToRefs 只转 state/getter，把 action 解构出来会丢 this。
const { dataVersion } = storeToRefs(runtime)

const pageSize = ref(20)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const filter = ref('all')
const groupFilter = ref('__all__')
const groups = ref([])
const groupManagerVisible = ref(false)
const selected = ref([])
const registeredTableRef = ref(null)
const loading = ref(false)
const checking = ref(false)
const plusCheckConcurrency = ref(4)
const checkProgress = ref({ done: 0, total: 0 })
const checkResult = ref('')
const importingSub2Api = ref(false)
const sub2apiInput = ref(null)
const pushingCpa = ref(false)
const relogging = ref(false)
const import2faVisible = ref(false)
const import2faText = ref('')
const importing2fa = ref(false)
const import2faResult = ref('')
const import2faErrors = ref([])
let loadRequestGeneration = 0

const PLUS_TYPE = {
  plus_eligible: 'success', plus_active: 'primary', free: 'warning',
  queued: 'info', checking: 'primary',
  // token_invalid（401 且响应体没有封号措辞）仍与 banned 分开显示——判据不同，
  // 不能混成一个。但配色从橙改红：AT 未到期却 401 = 被吊销，实测多半就是封号，
  // 橙色（=号还在）会让主人以为重新登录就能救回来。
  token_invalid: 'danger',
  banned: 'danger', error: 'danger',
}
function plusOf(row) { return row.plus_check || null }

async function runRollingPool(items, concurrency, worker) {
  let cursor = 0
  const workerCount = Math.min(Math.max(1, Number(concurrency) || 1), 20, items.length)
  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (cursor < items.length) {
      const index = cursor++
      await worker(items[index], index)
    }
  }))
}

async function load(resetPage) {
  if (resetPage) page.value = 1
  const generation = ++loadRequestGeneration
  loading.value = true
  try {
    const { items, total: t, groups: groupItems } = await listRegistered({
      limit: pageSize.value, offset: (page.value - 1) * pageSize.value, filter: filter.value,
      group_name: groupFilter.value,
    })
    // 筛选/分组切换可能同时发出多个请求；旧请求晚返回时不能覆盖最新结果。
    if (generation !== loadRequestGeneration) return
    rows.value = items
    total.value = t
    groups.value = groupItems || []
  } catch (e) {
    if (generation === loadRequestGeneration) ElMessage.error(e.message)
  }
  finally {
    if (generation === loadRequestGeneration) loading.value = false
  }
}

function clearSelection() {
  // 开了 reserve-selection 后，只清 selected 不会取消表格里已勾的行，
  // 必须走表格实例的 clearSelection 才能把跨页保留的勾选一起清掉。
  registeredTableRef.value?.clearSelection()
  selected.value = []
}

async function selectAllFiltered() {
  try {
    const r = await listRegistered({
      limit: SELECT_ALL_FETCH_LIMIT, offset: 0, filter: filter.value, group_name: groupFilter.value,
    })
    const all = r.items || []
    const table = registeredTableRef.value
    if (!table) return ElMessage.warning('列表尚未加载完成')
    table.clearSelection()
    await nextTick()
    // 全量结果里只有当前页那部分行存在于表格中；靠 row-key="email" 匹配，
    // 其余行由 selected 兜住，翻页时 reserve-selection 会自动补上勾选态。
    all.forEach((row) => table.toggleRowSelection(row, true))
    selected.value = all
    if (Number(r.total || 0) > all.length) {
      // 真超过单次拉取上限时必须明说，否则用户以为选全了、批量操作却只落到前一批。
      ElMessage.warning(`已选 ${all.length} 个，但当前筛选共 ${r.total} 个，超出单次上限未全部选中，请收窄筛选条件`)
    } else {
      ElMessage.success(`已全选当前筛选条件下的 ${all.length} 个账号`)
    }
  } catch (e) { ElMessage.error('全选失败: ' + e.message) }
}

const workspaceDialog = ref(false)
const workspaceOptions = ref([])
const workspaceTarget = ref(null)
async function openWorkspaceAssign() {
  if (!selected.value.length) return
  try { const r = await listWorkspaceMasters({ limit: 200, offset: 0 }); workspaceOptions.value = r.items || []; workspaceTarget.value = null; workspaceDialog.value = true }
  catch (e) { ElMessage.error('加载母号空间失败: ' + e.message) }
}
async function assignSelectedWorkspace() {
  if (!workspaceTarget.value) return ElMessage.warning('请选择母号空间')
  const invalid = selected.value.filter((row) => row.account_status === 'permanently_invalid')
  if (invalid.length) return ElMessage.warning('已永久失效账号不能划分到母号空间')
  try {
    const r = await assignCandidates(workspaceTarget.value, selected.value.map(x => x.email))
    ElMessage.success(`已划分 ${r.added} 个候选人到母号空间`); workspaceDialog.value = false
  } catch (e) { ElMessage.error('划分失败: ' + e.message) }
}

function collectEmails(mode) {
  if (mode === 'selected') return selected.value.map((r) => r.email)
  if (mode === 'unchecked') return rows.value.filter((r) => !plusOf(r)).map((r) => r.email)
  return rows.value.map((r) => r.email) // all（当前页）
}

async function doCheck(mode) {
  const emails = collectEmails(mode)
  if (!emails.length) { ElMessage.info('当前页没有可检测的号'); return }
  if (checking.value) return ElMessage.warning('检测任务正在执行中')
  checking.value = true
  checkProgress.value = { done: 0, total: emails.length }
  const selectedProxy = proxyText(form.value)
  const proxies = selectedProxy
    ? [selectedProxy]
    : [...new Set(proxyList.value.map((value) => String(value || '').trim()).filter(Boolean))]
  const rowsByEmail = new Map(
    rows.value.map((row) => [String(row.email || '').toLowerCase(), row]),
  )
  emails.forEach((email) => {
    const row = rowsByEmail.get(email.toLowerCase())
    if (row) row.plus_check = { status: 'queued', label: '排队中' }
  })
  checkResult.value = `检查中... (0/${emails.length})${proxies.length ? `，代理池 ${proxies.length} 条轮询` : '，直连'}`
  let plus = 0, free = 0, banned = 0, failed = 0, badToken = 0, done = 0
  const notes = new Set()
  let progressTimer = null
  let lastProgressAt = 0
  let latestDone = 0
  const publishProgress = (value, force = false) => {
    latestDone = value
    const now = Date.now()
    const elapsed = now - lastProgressAt
    if (!force && elapsed < 100) {
      if (!progressTimer) {
        progressTimer = setTimeout(() => {
          progressTimer = null
          publishProgress(latestDone, true)
        }, 100 - elapsed)
      }
      return
    }
    lastProgressAt = now
    checkProgress.value = { done: value, total: emails.length }
    checkResult.value = `检查中... (${value}/${emails.length})${proxies.length ? `，代理池 ${proxies.length} 条轮询` : '，直连'}`
  }
  try {
    await runRollingPool(emails, plusCheckConcurrency.value, async (email, index) => {
      const key = email.toLowerCase()
      const row = rowsByEmail.get(key)
      if (row) row.plus_check = { status: 'checking', label: '检测中' }
      let info
      try {
        const response = await checkPlus([email], proxies.length ? proxies[index % proxies.length] : '')
        info = response.results?.[key] || Object.values(response.results || {})[0]
        if (!info) info = { status: 'error', label: '服务器未返回检测结果' }
        if (response.note) notes.add(response.note)
      } catch (error) {
        info = { status: 'error', label: error.message || '检测失败' }
      }
      if (row) row.plus_check = info
      if (info.status === 'plus_eligible' || info.status === 'plus_active') plus++
      else if (info.status === 'banned') banned++
      else if (info.status === 'free') free++
      else if (info.status === 'token_invalid') badToken++
      else if (info.status === 'error') failed++
      done++
      publishProgress(done)
    })
    // failed / note 不入库，只是这一次的现场说明：
    // 以前网络/代理挂了这里只会显示「0 可用Plus, 0 Free, 0 封号」，看不出是没检测成。
    // badToken 从 2026-08-10 起是**会入库**的结论，措辞也跟着改：
    // AT 没过期却 401 = 被吊销，大概率就是封号，不该再说得像只是要重新登录。
    const parts = [`完成: ${plus} 可用Plus, ${free} Free, ${banned} 封号`]
    if (badToken) parts.push(`${badToken} 个凭证失效（AT 被吊销，多半已封）`)
    if (failed) parts.push(`${failed} 个没检测成`)
    if (notes.size) parts.push(...notes)
    checkResult.value = parts.join(' · ')
  } catch (e) {
    checkResult.value = ''
    ElMessage.error('检查失败: ' + e.message)
  } finally {
    if (progressTimer) clearTimeout(progressTimer)
    progressTimer = null
    checkProgress.value = { done, total: emails.length }
    checking.value = false
  }
}

// customClass 里的 pre-line 让消息里的 \n 真的换行。
// 不用 dangerouslyUseHTMLString：消息里会拼邮箱、文件名这些数据，走 HTML 等于开 XSS 口子。
async function confirm(msg) {
  try {
    await ElMessageBox.confirm(msg, '确认', {
      type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消',
      customClass: 'confirm-multiline',
    })
    return true
  }
  catch (_) { return false }
}
async function deleteOne(email) {
  if (!(await confirm(`删除 ${email} 的凭证？`))) return
  try { await deleteRegistered(email); ElMessage.success('已删除'); clearSelection(); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`确定删除选中的 ${emails.length} 条凭证？(不可恢复)`))) return
  try { const r = await bulkDeleteRegistered({ emails }); ElMessage.success(`已删除 ${r.deleted} 条`); clearSelection(); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteAll() {
  if (!(await confirm('这会清空注册结果表里的所有凭证！邮箱列表不受影响，确定？'))) return
  if (!(await confirm('再次确认：真的要删除全部凭证吗？此操作不可恢复！'))) return
  try { const r = await bulkDeleteRegistered({ all: true }); ElMessage.success(`已清空 ${r.deleted} 条`); clearSelection(); load() }
  catch (e) { ElMessage.error(e.message) }
}

async function afterGroupMutate() {
  await load()
  runtime.bumpData()
}

async function moveSelectedToGroup(groupName) {
  if (groupName === '__manage__') {
    groupManagerVisible.value = true
    return
  }
  if (groupName === '__ungrouped__') groupName = ''
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  try {
    const r = await setAccountsGroup(emails, groupName)
    ElMessage.success(`已移动 ${r.updated} 条注册结果`)
    await afterGroupMutate()
  } catch (e) { ElMessage.error(e.message) }
}

async function addGroup() {
  try {
    const { value } = await ElMessageBox.prompt('分组可先为空，之后再移动账号进去。', '新增分组', {
      inputPlaceholder: '例如：8月采购', confirmButtonText: '新增', cancelButtonText: '取消',
      inputValidator: (v) => String(v || '').trim().length > 0 || '分组名称不能为空',
    })
    await createAccountGroup(value.trim())
    ElMessage.success('分组已新增')
    await afterGroupMutate()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || String(e))
  }
}

async function renameGroup(group) {
  try {
    const { value } = await ElMessageBox.prompt('邮箱列表和注册结果会同步改名。', '重命名分组', {
      inputValue: group.name, confirmButtonText: '保存', cancelButtonText: '取消',
      inputValidator: (v) => String(v || '').trim().length > 0 || '分组名称不能为空',
    })
    const next = value.trim()
    const r = await renameAccountGroup(group.name, next)
    if (groupFilter.value === group.name) groupFilter.value = next
    ElMessage.success(`分组已改名，同步 ${r.moved} 个账号`)
    await afterGroupMutate()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || String(e))
  }
}

async function removeGroup(group) {
  if (!(await confirm(
    `删除分组“${group.name}”？\n邮箱列表 ${group.total} 个、注册结果 ${group.registered_total} 个账号会保留并归入未分组。`,
  ))) return
  try {
    const r = await deleteAccountGroup(group.name)
    if (groupFilter.value === group.name) groupFilter.value = ''
    ElMessage.success(`已删除分组，${r.ungrouped} 个账号归入未分组`)
    await afterGroupMutate()
  } catch (e) { ElMessage.error(e.message) }
}

// ──────────── 批量导出 ────────────
// 格式清单来自后端 export_formats.py，下拉菜单是 v-for 出来的：
// 以后加格式只改后端那一个文件，这里一行都不用动。
const exportFormats = ref([])
const exporting = ref(false)
const exportVisible = ref(false)
const exportText = ref('')
const exportCount = ref(0)
const exportFilename = ref('')
const exportLabel = ref('')
// 这一批导出的到底是哪些号 —— 「下载并删除」照着它删，来自后端 r.emails。
// 为什么要后端给、为什么在导出那一刻就存下来：
//   · 「导出全部」是跨页的，前端手里只有当前页 20 行，自己凑必漏；
//   · 弹窗开着的时候主人可能改勾选、翻页，后台自动跑号还会插进新号进来，
//     那时再去读 selected/表格，删的就不是刚下载的那批了。
const exportedEmails = ref([])
const deletingExported = ref(false)

const exportBtnText = computed(() =>
  selected.value.length ? `导出选中 (${selected.value.length})` : '导出全部',
)

async function loadExportFormats() {
  if (exportFormats.value.length) return
  try {
    const { formats } = await listExportFormats()
    exportFormats.value = formats || []
  } catch (e) { ElMessage.error('加载导出格式失败: ' + e.message) }
}

async function doExport(fmt) {
  const emails = selected.value.map((r) => r.email)
  // 没勾选 = 导出全部（跨页，不只当前页）
  const payload = emails.length
    ? { format: fmt.id, emails, proxy_pool: proxyList.value.join('\n') }
    : { format: fmt.id, all: true, proxy_pool: proxyList.value.join('\n') }
  exporting.value = true
  try {
    const r = await exportRegistered(payload)
    exportedEmails.value = (r.emails || []).filter(Boolean)
    // download 模式（CPA zip / SUB2API json）：不弹预览，直接落盘
    if (r.mode === 'download') {
      saveBlob(b64ToBytes(r.b64), r.filename, r.mime)
      ElMessage.success(`已下载 ${r.filename}（${r.count} 个号）`)
      return
    }
    exportText.value = r.text || ''
    exportCount.value = r.count || 0
    exportFilename.value = r.filename || 'export.txt'
    exportLabel.value = r.label || fmt.label
    exportVisible.value = true
  } catch (e) { ElMessage.error('导出失败: ' + e.message) }
  finally { exporting.value = false }
}

function b64ToBytes(b64) {
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

function saveBlob(data, filename, mime) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function downloadExport() {
  saveBlob(exportText.value, exportFilename.value, 'text/plain;charset=utf-8')
}

// ──────────── 下载并删除 ────────────
// 主人的原话：「不然分不清楚越堆越多」。导出的 txt 里邮箱/密码/2FA/取件url 都齐了，
// 这两张表就没有留存价值了，一起清掉。
//
// ⚠️ 顺序**必须**是「先下载、再确认、最后删」：
//    删库是不可恢复的，而浏览器下载可能被拦（弹窗拦截 / 用户点了取消 / 磁盘满）。
//    先把文件落盘再问，主人是在**手里已经有 txt** 的前提下点的确认。
//    确认框里再报一遍将要删的两张表各多少条，删完之前还有最后一次反悔机会。
async function downloadAndDelete() {
  downloadExport()

  const emails = exportedEmails.value
  if (!emails.length) {
    ElMessage.warning('这批导出没有拿到 email 列表，只下载不删除')
    return
  }

  const ok = await confirm(
    `已下载 ${exportFilename.value}。\n\n` +
    `现在删除这 ${emails.length} 个号：\n` +
    `  · 注册结果（凭证、2FA secret）\n` +
    `  · 邮箱列表（号池那一行，含取件链接）\n\n` +
    `删掉后只剩刚下载的 txt 这一份，不可恢复。确定？`,
  )
  if (!ok) return

  deletingExported.value = true
  try {
    // 两张表分别删。先删注册结果：它是主人真正在看的那张表，
    // 万一号池那边报错（比如这批号根本不是号池导入的、压根没有对应行），
    // 至少结果表已经清干净了，不会出现"删了一半还看得见"。
    const r1 = await bulkDeleteRegistered({ emails })
    let poolDeleted = 0
    try {
      const r2 = await bulkDeleteAccounts({ emails })
      poolDeleted = r2.deleted || 0
    } catch (e) {
      // 号池删失败不算整体失败：凭证已经清掉了，主人该知道的是号池还剩着
      ElMessage.warning('注册结果已删，但邮箱列表删除失败: ' + e.message)
    }
    ElMessage.success(`已删除：注册结果 ${r1.deleted} 条 / 邮箱列表 ${poolDeleted} 条`)
    exportVisible.value = false
    exportedEmails.value = []
    clearSelection()
    load(true)          // 回第一页：这一批没了，停在旧页码多半是空页
    runtime.bumpData()  // 通知「邮箱列表」那一页也刷新，否则主人切过去还看得到已删的号
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  } finally {
    deletingExported.value = false
  }
}

// 凭证弹窗
const credVisible = ref(false)
const credEmail = ref('')
const credData = ref(null)
// totp_secret 放最前：它是唯一「服务端取不回」的字段，弹窗一打开就要能看到
const CRED_KEYS = ['totp_secret', 'totp_factor_id', 'access_token', 'session_token', 'refresh_token', 'id_token', 'device_id', 'csrf_token', 'cookie_header', 'password']
const credRows = computed(() => {
  if (!credData.value) return []
  return CRED_KEYS.filter((k) => credData.value[k]).map((k) => ({ key: k, val: credData.value[k] }))
})
async function viewCred(email) {
  try {
    const { data } = await getRegistered(email)
    credData.value = data
    credEmail.value = email
    credVisible.value = true
  } catch (e) { ElMessage.error('加载凭证失败: ' + e.message) }
}
async function copyCell(email, field) {
  try {
    const { data } = await getRegistered(email)
    const val = data[field] || ''
    if (!val) { ElMessage.warning(`${field} 为空`); return }
    await copyText(val)
  } catch (e) { ElMessage.error('加载凭证失败: ' + e.message) }
}
function copyAllJson() {
  if (credData.value) copyText(JSON.stringify(credData.value, null, 2))
}

// ── 手动编辑凭证 ──
// 只改本地库，不同步 OpenAI。改完的值会被登录流程直接用上
// （registrar 的 account_callback 走 db.get_registered，不区分数据来源）。
const editVisible = ref(false)
const editSaving = ref(false)
const editPasswordOnly = ref(false)
const editEmail = ref('')
const editPassword = ref('')
const editSecret = ref('')
// 打开弹窗时的原值，用来判断哪些字段真被改过（没改的不传，后端就不碰）
const editOrigPassword = ref('')
const editOrigSecret = ref('')

function openEdit(row) {
  editPasswordOnly.value = false
  editEmail.value = row.email
  editPassword.value = row.password || ''
  editSecret.value = row.totp_secret || ''
  editOrigPassword.value = row.password || ''
  editOrigSecret.value = row.totp_secret || ''
  editVisible.value = true
}

function openPasswordEntry(row) {
  editPasswordOnly.value = true
  editEmail.value = row.email
  editPassword.value = ''
  editSecret.value = row.totp_secret || ''
  editOrigPassword.value = row.password || ''
  editOrigSecret.value = row.totp_secret || ''
  editVisible.value = true
}

async function saveEdit() {
  const pw = editPassword.value
  const sec = editSecret.value.trim()
  const payload = { email: editEmail.value }
  if (editPasswordOnly.value && !pw.trim()) {
    ElMessage.warning('请输入已经在网页版创建的密码')
    return
  }
  // 只把真正改动过的字段传给后端 —— 没动的字段不传，后端就不会碰它
  if (pw !== editOrigPassword.value) payload.password = pw
  if (!editPasswordOnly.value && sec !== editOrigSecret.value) payload.totp_secret = sec
  if (payload.password === undefined && payload.totp_secret === undefined) {
    ElMessage.info('没有改动')
    editVisible.value = false
    return
  }
  // secret 是唯一「服务端取不回」的凭证：覆盖掉原值 = 该号 2FA 永久锁死。
  // 只在「原本就有 secret」且「确实要改」时拦一道，新填不打扰。
  if (payload.totp_secret !== undefined && editOrigSecret.value) {
    try {
      await ElMessageBox.confirm(
        `该账号已有 2FA secret：\n${editOrigSecret.value}\n\n` +
        '覆盖后原 secret 将永久丢失，服务端取不回。\n' +
        '若原 secret 仍是账号上生效的那个，覆盖会导致该号 2FA 永远登不上。',
        '确认覆盖 2FA secret？',
        { type: 'warning', confirmButtonText: '确认覆盖', cancelButtonText: '取消' },
      )
    } catch { return }
  }
  editSaving.value = true
  try {
    const r = await updateCredentials(payload)
    ElMessage.success(`已保存：${(r.changed || []).join(' + ') || '无改动'}`)
    editVisible.value = false
    await load()
  } catch (e) {
    // 后端 400 会带具体原因（如「TOTP secret 含非法字符」），原样透出
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally { editSaving.value = false }
}

function chooseSub2ApiFile() { sub2apiInput.value?.click() }
async function onSub2ApiFile(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  importingSub2Api.value = true
  try {
    const text = await file.text()
    const result = await importSub2Api(text, groupFilter.value === '__all__' ? '' : groupFilter.value)
    ElMessage.success(`已导入 ${result.imported} 个 Sub2API 已注册账号`)
    await load(true)
  } catch (e) {
    const detail = e.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : (detail?.message || e.message))
  } finally { importingSub2Api.value = false }
}

const import2faLineCount = computed(
  () => import2faText.value.split('\n').filter((l) => l.trim() && !l.trim().startsWith('#')).length,
)

async function doImport2FA() {
  if (!import2faText.value.trim()) {
    ElMessage.warning('请输入要导入的账号')
    return
  }
  importing2fa.value = true
  import2faResult.value = ''
  import2faErrors.value = []
  try {
    const grp = groupFilter.value === '__all__' ? '' : groupFilter.value
    const r = await import2FA(import2faText.value.trim(), grp)
    const groupLabel = grp || '未分组'
    import2faResult.value = `导入到"${groupLabel}"：共 ${r.total} 行，新增 ${r.imported}，更新 ${r.updated}`
    ElMessage.success('2FA 导入完成')
    import2faText.value = ''
    await load(true)
    runtime.bumpData()
  } catch (e) {
    const detail = e.response?.data?.detail
    if (detail && typeof detail === 'object' && detail.errors?.length) {
      import2faErrors.value = detail.errors
      import2faResult.value = `有 ${detail.errors.length} 行不合法，已全部拒绝，一个都没导入`
      ElMessage.error('导入被拒绝，请修正后重试')
    } else {
      import2faResult.value = '导入失败: ' + (typeof detail === 'string' ? detail : (detail?.message || e.message))
      ElMessage.error(typeof detail === 'string' ? detail : e.message)
    }
  } finally { importing2fa.value = false }
}

async function pushSelectedToCpa() {
  const emails = selected.value.map((row) => row.email).filter(Boolean)
  if (!emails.length) return
  try {
    await ElMessageBox.confirm(
      `将选中的 ${emails.length} 个账号转换为 CPA 格式并上传到已配置的 CPA 号池。\n` +
      '上传前会优先使用 refresh_token 刷新 Codex access_token；没有 RT 的账号会失败。继续？',
      '推送到 CPA 号池',
      { type: 'warning', confirmButtonText: '开始推送', cancelButtonText: '取消', customClass: 'confirm-multiline' },
    )
  } catch { return }
  pushingCpa.value = true
  try {
    const result = await pushRegisteredToCpa(emails, proxyText(form.value))
    const failed = (result.results || []).filter((item) => !item.ok)
    if (failed.length) {
      const detail = failed.slice(0, 3).map((item) => `${item.email}: ${item.error || '失败'}`).join('；')
      ElMessage.warning(`CPA 推送完成：成功 ${result.succeeded}，失败 ${result.failed}。${detail}`)
    } else {
      ElMessage.success(`CPA 推送成功：${result.succeeded} 个账号`)
    }
  } catch (e) {
    ElMessage.error('CPA 推送失败: ' + (e.response?.data?.detail || e.message))
  } finally { pushingCpa.value = false }
}

async function reloginSelected() {
  const emails = selected.value.map((row) => row.email).filter(Boolean)
  if (!emails.length) return
  try {
    await ElMessageBox.confirm(
      `将选中的 ${emails.length} 个已注册账号投入重登录。\n` +
      '仅登录开启“补齐2FA”时，只会为已有密码但缺少 TOTP 的账号绑定 2FA；不会创建密码，通用 OTP 账号必须有中转链接。遇到可重试错误会重新尝试，页面会按账号统计最终成功、最终失败和重试次数。继续？',
      '重登录选中账号',
      { type: 'warning', confirmButtonText: '开始重登录', cancelButtonText: '取消', customClass: 'confirm-multiline' },
    )
  } catch { return }
  relogging.value = true
  try {
    await autoStart({
      login_only: true,
      login_emails: emails,
      personal_only: true,
      group_name: '__all__',
      concurrency: 1,
      proxy: proxyText(form.value),
      proxy_pool: proxyList.value.join('\n'),
      otp_timeout: 180,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: true,
      want_password: false,
      want_2fa: false,
      ensure_credentials: true,
      allow_existing_login: true,
      cool_down_seconds: 0,
      account_retry_count: 1,
      auto_export: true,
      export_refresh_oauth: false,
      target_count: 0,
    })
    ElMessage.success(`已开始重登录 ${emails.length} 个账号，可在自动任务页查看进度`)
  } catch (e) {
    ElMessage.error('启动重登录失败: ' + (e.response?.data?.detail || e.message))
  } finally { relogging.value = false }
}

watch(page, () => load())
watch(pageSize, () => { page.value = 1; clearSelection(); load() })
watch([filter, groupFilter], () => { clearSelection() })
watch(dataVersion, () => load())
onActivated(() => load())
</script>
<template>
  <div class="page-container">
    <!-- Hero Summary Header -->
    <div class="hero-kpi-grid">
      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">已注册账号</span>
          <Icon icon="lucide:users" class="kpi-type-icon" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val">{{ total }}</div>
          <div class="kpi-hint">系统已托管注册结果总量</div>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">分组统计</span>
          <span class="kpi-dot dot-primary" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val-row">
            <span class="kpi-val">{{ groups.length }}</span>
            <span class="kpi-sub">个自定义分组</span>
          </div>
          <div class="kpi-hint">当前选择: {{ groupFilter === '__all__' ? '全部分组' : (groupFilter || '未分组') }}</div>
        </div>
        <div class="kpi-footer">
          <el-button link type="primary" size="small" @click="groupManagerVisible = true">
            <Icon icon="lucide:settings-2" style="margin-right: 4px" /> 分组管理
          </el-button>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">选中与操作</span>
          <span class="kpi-dot dot-warning" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val-row">
            <span class="kpi-val">{{ selected.length }}</span>
            <span class="kpi-sub">/ {{ total }} 选中</span>
          </div>
          <div class="kpi-hint">支持批量检测、移动、划分与重登录</div>
        </div>
        <div class="kpi-footer">
          <el-button link type="primary" size="small" :disabled="!total" @click="selectAllFiltered">全选当前筛选</el-button>
          <el-button link type="info" size="small" :disabled="!selected.length" @click="clearSelection">清空选择</el-button>
        </div>
      </div>
    </div>

    <!-- Quick Segment Status Filter -->
    <div class="filter-segment-bar">
      <div class="segment-tabs">
        <button
          v-for="item in [
            { label: '全部', value: 'all', icon: 'lucide:layers' },
            { label: '已获取 AT', value: 'has_at', icon: 'lucide:key' },
            { label: '无 AT', value: 'no_at', icon: 'lucide:key-round' },
            { label: '有 RT', value: 'has_rt', icon: 'lucide:refresh-cw' },
            { label: '无 RT', value: 'no_rt', icon: 'lucide:alert-circle' },
            { label: '未检测', value: 'unchecked', icon: 'lucide:help-circle' },
            { label: 'Free', value: 'free', icon: 'lucide:coffee' },
            { label: 'Plus生效', value: 'plus_active', icon: 'lucide:sparkles' },
            { label: '可领Plus', value: 'plus_eligible', icon: 'lucide:gift' },
            { label: '永久失效', value: 'permanently_invalid', icon: 'lucide:ban' },
            { label: '凭证失效', value: 'token_invalid', icon: 'lucide:shield-alert' }
          ]"
          :key="item.value"
          class="segment-tab-btn"
          :class="{ active: filter === item.value }"
          @click="filter = item.value; load(true)"
        >
          <Icon :icon="item.icon" class="tab-icon" />
          <span>{{ item.label }}</span>
        </button>
      </div>

      <div class="segment-right">
        <el-select v-model="groupFilter" placeholder="选择分组" style="width: 170px" size="default" @change="load(true)">
          <el-option label="全部分组" value="__all__" />
          <el-option label="未分组" value="" />
          <el-option
            v-for="g in groups" :key="g.name"
            :label="`${g.name} (${g.registered_total})`" :value="g.name"
          />
        </el-select>
        <el-button :icon="Refresh" circle @click="load(false)" />
      </div>
    </div>

    <!-- Workflow Action Toolbar -->
    <div class="workflow-toolbar">
      <div class="toolbar-left">
        <input ref="sub2apiInput" type="file" accept=".json,application/json" hidden @change="onSub2ApiFile" />
        <el-button :loading="importingSub2Api" @click="chooseSub2ApiFile">
          <Icon icon="lucide:upload" style="margin-right: 5px" /> 导入 Sub2API
        </el-button>
        <el-button @click="import2faVisible = true">
          <Icon icon="lucide:shield-check" style="margin-right: 5px" /> 导入 2FA
        </el-button>

        <el-divider direction="vertical" />

        <el-select
          v-model="form.proxy" filterable clearable allow-create default-first-option
          :reserve-keyword="false" placeholder="检测代理（留空直连）"
          style="width: 220px"
        >
          <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
        </el-select>

        <div class="concurrency-box">
          <span class="concurrency-label">并发</span>
          <el-input-number v-model="plusCheckConcurrency" :min="1" :max="20" controls-position="right" style="width: 80px" />
        </div>

        <el-dropdown trigger="click" @command="doCheck">
          <el-button :loading="checking">
            <Icon icon="lucide:search-check" style="margin-right: 4px" /> 检测 Plus
            <Icon icon="lucide:chevron-down" style="margin-left: 4px" />
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="unchecked">检查当前页未检测</el-dropdown-item>
              <el-dropdown-item command="all">重新检查当前页全部</el-dropdown-item>
              <el-dropdown-item command="selected" :disabled="!selected.length">
                检测选中项 ({{ selected.length }})
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>

        <span v-if="checkResult" class="check-result-tag">{{ checkResult }}</span>
      </div>

      <div class="toolbar-right">
        <!-- Batch Actions Menu -->
        <el-dropdown trigger="click" :disabled="!selected.length" @command="moveSelectedToGroup">
          <el-button :disabled="!selected.length">
            <Icon icon="lucide:folder-input" style="margin-right: 4px" /> 移动分组 ({{ selected.length }})
            <Icon icon="lucide:chevron-down" style="margin-left: 4px" />
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="__ungrouped__">移动到未分组</el-dropdown-item>
              <el-dropdown-item v-for="g in groups" :key="g.name" :command="g.name">
                移动到 {{ g.name }}
              </el-dropdown-item>
              <el-dropdown-item divided command="__manage__">管理分组</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>

        <el-button type="warning" plain :disabled="!selected.length" @click="openWorkspaceAssign">
          <Icon icon="lucide:building" style="margin-right: 4px" /> 划分到母号 ({{ selected.length }})
        </el-button>

        <el-button type="success" plain :loading="pushingCpa" :disabled="!selected.length" @click="pushSelectedToCpa">
          <Icon icon="lucide:send" style="margin-right: 4px" /> 推送 CPA ({{ selected.length }})
        </el-button>

        <el-button type="primary" plain :loading="relogging" :disabled="!selected.length" @click="reloginSelected">
          <Icon icon="lucide:log-in" style="margin-right: 4px" /> 重登录 ({{ selected.length }})
        </el-button>

        <el-dropdown trigger="click" @command="doExport" @visible-change="(v) => v && loadExportFormats()">
          <el-button :loading="exporting">
            <Icon icon="lucide:download" style="margin-right: 4px" /> {{ exportBtnText }}
            <Icon icon="lucide:chevron-down" style="margin-left: 4px" />
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="f in exportFormats" :key="f.id" :command="f" :divided="f.mode === 'download' && f.id === 'cpa'">
                {{ f.label }}
                <span v-if="f.note" class="hint" style="margin-left: 6px">{{ f.note }}</span>
              </el-dropdown-item>
              <el-dropdown-item v-if="!exportFormats.length" disabled>加载中...</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>

        <el-dropdown trigger="click">
          <el-button type="danger" plain>
            <Icon icon="lucide:trash-2" style="margin-right: 4px" /> 清理
            <Icon icon="lucide:chevron-down" style="margin-left: 4px" />
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item :disabled="!selected.length" @click="deleteSelected">
                <span style="color: var(--el-color-danger)">删除选中 ({{ selected.length }})</span>
              </el-dropdown-item>
              <el-dropdown-item divided @click="deleteAll">
                <span style="color: var(--el-color-danger)">清空当前列表全部</span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <!-- Main Data Table -->
    <div class="table-container">
      <el-skeleton v-if="loading && !rows.length" :rows="8" animated style="padding: 16px" />
      <el-table
        v-else
        ref="registeredTableRef"
        v-loading="loading"
        :data="rows"
        size="default"
        row-key="email"
        class="modern-table"
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="48" align="center" :reserve-selection="true" />

        <!-- 账号信息复合列 -->
        <el-table-column label="账号与分组" min-width="220">
          <template #default="{ row }">
            <div class="composite-cell">
              <div class="primary-row">
                <span class="mono-text account-email">{{ row.email }}</span>
                <el-button
                  size="small"
                  text
                  circle
                  class="mini-copy-btn"
                  title="复制邮箱"
                  @click="copyText(row.email)"
                >
                  <Icon icon="lucide:copy" />
                </el-button>
              </div>
              <div class="secondary-meta">
                <el-tag v-if="row.group_name" size="small" type="info" effect="plain" class="group-tag">
                  <Icon icon="lucide:folder" style="font-size: 11px; margin-right: 3px" />
                  {{ row.group_name }}
                </el-tag>
                <span v-else class="sub-hint">未分组</span>
                <span class="meta-dot">·</span>
                <span class="time-hint">{{ fmtTime(row.created_at) }}</span>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 状态列 -->
        <el-table-column label="账号状态" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.account_status === 'permanently_invalid'" type="danger" size="small" effect="dark">
              已永久失效
            </el-tag>
            <el-tag v-else type="success" size="small" effect="plain">
              正常
            </el-tag>
          </template>
        </el-table-column>

        <!-- 密码 -->
        <el-table-column label="密码" min-width="160">
          <template #default="{ row }">
            <div v-if="row.password" class="cred-cell">
              <span class="mono-text cred-val">{{ row.password }}</span>
              <el-button
                size="small"
                text
                circle
                class="mini-copy-btn"
                title="复制密码"
                @click="copyText(row.password)"
              >
                <Icon icon="lucide:copy" />
              </el-button>
            </div>
            <span v-else class="sub-hint">—</span>
          </template>
        </el-table-column>

        <!-- 2FA Secret -->
        <el-table-column label="2FA Secret" min-width="200">
          <template #default="{ row }">
            <div v-if="row.totp_secret" class="cred-cell">
              <span class="mono-text cred-val totp-val">{{ row.totp_secret }}</span>
              <el-button
                size="small"
                text
                circle
                class="mini-copy-btn"
                title="复制 2FA Secret"
                @click="copyText(row.totp_secret)"
              >
                <Icon icon="lucide:copy" />
              </el-button>
            </div>
            <span v-else class="sub-hint">—</span>
          </template>
        </el-table-column>

        <!-- Plus 状态 -->
        <el-table-column label="Plus 状态" width="130">
          <template #default="{ row }">
            <StatusDot v-if="plusOf(row)" :type="PLUS_TYPE[plusOf(row).status] || 'info'" :text="plusOf(row).label" />
            <span v-else class="sub-hint">未检测</span>
          </template>
        </el-table-column>

        <!-- Token 凭证 -->
        <el-table-column label="凭证概况" width="210">
          <template #default="{ row }">
            <div class="token-badges">
              <span
                class="token-badge"
                :class="{ 'has-token': row.at_len > 0 }"
                title="Access Token"
                @click="row.at_len > 0 && copyCell(row.email, 'access_token')"
              >
                AT <span v-if="row.at_len > 0" class="token-len">{{ row.at_len }}</span>
              </span>
              <span
                class="token-badge"
                :class="{ 'has-token': row.st_len > 0 }"
                title="Session Token"
                @click="row.st_len > 0 && copyCell(row.email, 'session_token')"
              >
                ST <span v-if="row.st_len > 0" class="token-len">{{ row.st_len }}</span>
              </span>
              <span
                class="token-badge"
                :class="{ 'has-token': row.rt_len > 0 }"
                title="Refresh Token"
                @click="row.rt_len > 0 && copyCell(row.email, 'refresh_token')"
              >
                RT <span v-if="row.rt_len > 0" class="token-len">{{ row.rt_len }}</span>
              </span>
            </div>
          </template>
        </el-table-column>

        <!-- 操作 -->
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <div class="action-cell">
              <el-button size="small" text type="primary" @click="viewCred(row.email)">凭证</el-button>
              <el-button
                v-if="!row.password"
                size="small"
                text
                type="success"
                @click="openPasswordEntry(row)"
              >
                录入
              </el-button>
              <el-button v-else size="small" text type="warning" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text type="danger" @click="deleteOne(row.email)">删除</el-button>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-box">
            <Icon icon="lucide:inbox" class="empty-icon" />
            <div class="empty-title">暂无注册结果</div>
            <div class="empty-sub">去「单次注册」或「全自动批量」跑号获取账号</div>
          </div>
        </template>
      </el-table>

      <!-- Pagination Footer -->
      <div class="table-pagination-footer">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="PAGE_SIZE_OPTIONS"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </div>

    <!-- 划分到母号空间弹窗 -->
    <el-dialog v-model="workspaceDialog" title="划分到母号空间" width="520px" destroy-on-close>
      <div class="modal-instruction">
        这里只建立系统内候选关系，不会调用邀请或申请加入接口；同一账号可以划分到多个母号空间。
      </div>
      <el-select v-model="workspaceTarget" filterable placeholder="选择母号空间" style="width: 100%">
        <el-option
          v-for="item in workspaceOptions"
          :key="item.id"
          :value="item.id"
          :label="`${item.account} · ${item.workspace_id || '无 Workspace ID'}`"
        />
      </el-select>
      <template #footer>
        <el-button @click="workspaceDialog = false">取消</el-button>
        <el-button type="primary" @click="assignSelectedWorkspace">确认划分</el-button>
      </template>
    </el-dialog>

    <!-- 导出弹窗 -->
    <el-dialog v-model="exportVisible" width="720px" top="8vh" destroy-on-close>
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px">
          <span style="font-weight: 600">导出 · {{ exportLabel }}</span>
          <el-tag size="small" type="info">共 {{ exportCount }} 行</el-tag>
        </div>
      </template>
      <el-input
        :model-value="exportText"
        type="textarea"
        :rows="14"
        readonly
        class="mono-text export-area"
      />
      <template #footer>
        <el-button @click="copyText(exportText)">
          <Icon icon="lucide:copy" style="margin-right: 4px" /> 复制全部
        </el-button>
        <el-button type="primary" @click="downloadExport">
          <Icon icon="lucide:download" style="margin-right: 4px" /> 下载 {{ exportFilename }}
        </el-button>
        <el-button
          type="danger"
          plain
          :loading="deletingExported"
          :disabled="!exportedEmails.length"
          @click="downloadAndDelete"
        >
          <Icon icon="lucide:trash-2" style="margin-right: 4px" /> 下载并删除这 {{ exportedEmails.length }} 个号
        </el-button>
      </template>
    </el-dialog>

    <!-- 查看凭证弹窗 -->
    <el-dialog v-model="credVisible" :title="credEmail" width="760px" top="6vh" destroy-on-close>
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between; padding-right: 20px">
          <span class="mono-text" style="font-weight: 600; font-size: 15px">{{ credEmail }}</span>
          <el-button size="small" type="primary" plain @click="copyAllJson">
            <Icon icon="lucide:copy" style="margin-right: 4px" /> 复制全部 JSON
          </el-button>
        </div>
      </template>
      <div v-for="r in credRows" :key="r.key" class="cred-row-card">
        <div class="cred-row-header">
          <span class="mono-text cred-row-key">{{ r.key }}</span>
          <el-tag size="small" type="info">len={{ r.val.length }}</el-tag>
          <el-button size="small" text @click="copyText(r.val)">复制</el-button>
        </div>
        <el-input :model-value="r.val" type="textarea" :rows="2" readonly class="mono-text" />
      </div>
      <el-empty v-if="!credRows.length" description="无凭证字段" />
    </el-dialog>

    <!-- 手动编辑/录入凭证 -->
    <el-dialog
      v-model="editVisible"
      :title="editPasswordOnly ? '录入账号密码' : '编辑凭证'"
      width="560px"
      top="10vh"
      destroy-on-close
    >
      <el-alert
        :type="editPasswordOnly ? 'info' : 'warning'"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
        :title="editPasswordOnly ? '补录已经在网页版创建好的密码' : '仅修改本地记录，不会同步到 OpenAI'"
        :description="editPasswordOnly
          ? '保存后，仅登录会优先使用这个密码和 2FA；不会修改 OpenAI 网页版密码。'
          : '这里改密码不等于改了账号密码。填入的值会被登录流程直接使用。'"
      />
      <el-form label-position="top">
        <el-form-item label="邮箱">
          <el-input :model-value="editEmail" class="mono-text" disabled />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="editPassword"
            class="mono-text"
            type="password"
            show-password
            :placeholder="editPasswordOnly ? '请输入网页版已经创建好的密码' : '留空表示该号无密码'"
            @keyup.enter="saveEdit"
          />
        </el-form-item>
        <el-form-item v-if="!editPasswordOnly" label="2FA Secret">
          <el-input
            v-model="editSecret"
            class="mono-text"
            placeholder="base32，支持带空格/小写/otpauth:// 链接，会自动规范化"
          />
          <div class="sub-hint" style="margin-top: 6px; line-height: 1.6">
            服务端取不回此值，覆盖后原 secret 永久丢失。清空则该号按无 2FA 处理。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="saveEdit">
          {{ editPasswordOnly ? '保存密码' : '保存' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 分组管理弹窗 -->
    <el-dialog v-model="groupManagerVisible" title="分组管理" width="min(660px, 92vw)" top="10vh" destroy-on-close>
      <div style="display: flex; justify-content: flex-end; margin-bottom: 12px">
        <el-button type="primary" @click="addGroup">
          <Icon icon="lucide:plus" style="margin-right: 4px" /> 新增分组
        </el-button>
      </div>
      <el-table :data="groups" size="default" border class="modern-table">
        <el-table-column prop="name" label="分组名称" min-width="190" />
        <el-table-column prop="total" label="邮箱列表" width="110" align="center" />
        <el-table-column prop="registered_total" label="注册结果" width="110" align="center" />
        <el-table-column label="操作" width="150" align="center">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="renameGroup(row)">改名</el-button>
            <el-button size="small" text type="danger" @click="removeGroup(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty><el-empty description="还没有自定义分组" :image-size="54" /></template>
      </el-table>
    </el-dialog>

    <!-- 导入 2FA 账号弹窗 -->
    <el-dialog v-model="import2faVisible" title="导入 2FA 账号" width="680px" top="8vh" destroy-on-close>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
        title="将已在外部注册好的账号直接导入注册结果表"
        description="每行一个，用 ---- 分隔。支持 2 段（邮箱----密码）或 3 段（邮箱----密码----2FA）。空行和 # 开头的注释行自动跳过。导入后账号状态标记为 active。"
      />
      <p class="sub-hint" style="margin-bottom: 8px">
        格式：<code>邮箱----密码----2FA</code>（2FA 可选，支持 base32、otpauth:// 链接）
      </p>
      <el-input
        v-model="import2faText"
        type="textarea"
        :rows="10"
        class="mono-text"
        placeholder="user@example.com----MyP@ssw0rd----JBSWY3DPEHPK3PXP&#10;user2@example.com----Pass1234"
      />
      <div style="margin-top: 12px; display: flex; align-items: center; gap: 12px">
        <el-button type="primary" :loading="importing2fa" @click="doImport2FA">导入</el-button>
        <span v-if="import2faLineCount" class="sub-hint">待导入 {{ import2faLineCount }} 行</span>
        <span class="sub-hint">{{ import2faResult }}</span>
      </div>
      <el-alert
        v-if="import2faErrors.length"
        type="error"
        :closable="true"
        show-icon
        style="margin-top: 12px"
        title="以下行不合法，整批已拒绝（注册结果表未被改动）"
        @close="import2faErrors = []"
      >
        <ul class="err-list">
          <li v-for="e in import2faErrors" :key="e.line">
            <b>第 {{ e.line }} 行</b>：{{ e.error }}
          </li>
        </ul>
      </el-alert>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-container {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
}

/* Hero KPI Grid */
.hero-kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
}

.kpi-card {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-lg);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  box-shadow: var(--app-shadow-sm);
  transition: transform 0.2s, box-shadow 0.2s;
}

.kpi-card:hover {
  box-shadow: var(--app-shadow-md);
}

.kpi-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.kpi-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.kpi-type-icon {
  font-size: 18px;
  color: var(--el-text-color-placeholder);
}

.kpi-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.dot-primary { background: var(--el-color-primary); }
.dot-warning { background: var(--el-color-warning); }
.dot-success { background: var(--el-color-success); }

.kpi-body {
  margin-bottom: 12px;
}

.kpi-val {
  font-size: 28px;
  font-weight: 700;
  color: var(--el-text-color-primary);
  line-height: 1.2;
}

.kpi-val-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.kpi-sub {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.kpi-hint {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  margin-top: 4px;
}

.kpi-footer {
  display: flex;
  align-items: center;
  gap: 12px;
  border-top: 1px dashed var(--el-border-color-lighter);
  padding-top: 10px;
  margin-top: auto;
}

/* Filter Segment Bar */
.filter-segment-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-md);
  padding: 8px 12px;
  flex-wrap: wrap;
  gap: 10px;
}

.segment-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.segment-tab-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: var(--app-radius-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--el-text-color-regular);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}

.segment-tab-btn:hover {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-primary);
}

.segment-tab-btn.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-5);
  font-weight: 600;
}

.tab-icon {
  font-size: 14px;
}

.segment-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* Workflow Toolbar */
.workflow-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-md);
  padding: 10px 14px;
  flex-wrap: wrap;
  gap: 12px;
}

.toolbar-left, .toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.concurrency-box {
  display: flex;
  align-items: center;
  gap: 4px;
  background: var(--el-fill-color-light);
  border-radius: var(--app-radius-sm);
  padding: 2px 6px;
}

.concurrency-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.check-result-tag {
  font-size: 12px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  padding: 3px 8px;
  border-radius: var(--app-radius-xs);
}

/* Table Container */
.table-container {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-lg);
  overflow: hidden;
  box-shadow: var(--app-shadow-sm);
}

.modern-table {
  width: 100%;
}

.composite-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.primary-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.mono-text {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.account-email {
  font-weight: 600;
  font-size: 13px;
  color: var(--el-text-color-primary);
}

.secondary-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.group-tag {
  font-size: 11px;
}

.meta-dot {
  color: var(--el-text-color-placeholder);
}

.time-hint, .sub-hint {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.mini-copy-btn {
  padding: 2px;
  height: 20px;
  width: 20px;
  color: var(--el-text-color-secondary);
}

.mini-copy-btn:hover {
  color: var(--el-color-primary);
}

.cred-cell {
  display: flex;
  align-items: center;
  gap: 4px;
}

.cred-val {
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.totp-val {
  color: var(--el-color-warning-dark-2);
}

/* Token Badges */
.token-badges {
  display: flex;
  gap: 6px;
}

.token-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: var(--app-radius-xs);
  background: var(--el-fill-color-light);
  color: var(--el-text-color-placeholder);
  cursor: default;
  transition: all 0.15s;
}

.token-badge.has-token {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  cursor: pointer;
}

.token-badge.has-token:hover {
  background: var(--el-color-primary-light-8);
}

.token-len {
  font-size: 10px;
  font-weight: 400;
  opacity: 0.8;
}

.action-cell {
  display: flex;
  align-items: center;
  gap: 4px;
}

.table-pagination-footer {
  display: flex;
  justify-content: flex-end;
  padding: 12px 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.modal-instruction {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
  margin-bottom: 14px;
}

.cred-row-card {
  margin-bottom: 12px;
}

.cred-row-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
}

.cred-row-key {
  font-weight: 600;
  color: var(--el-color-primary);
  font-size: 13px;
}

.err-list {
  margin: 6px 0 0;
  padding-left: 18px;
  max-height: 220px;
  overflow-y: auto;
  line-height: 1.7;
}

.empty-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 0;
}

.empty-icon {
  font-size: 48px;
  color: var(--el-text-color-placeholder);
  margin-bottom: 8px;
}

.empty-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-regular);
}

.empty-sub {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  margin-top: 4px;
}
</style>

<style>
.confirm-multiline .el-message-box__message { white-space: pre-line; }
</style>
