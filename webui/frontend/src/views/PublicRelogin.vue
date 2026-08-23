<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { checkPublicRelogin, runPublicRelogin } from '@/api/publicRelogin'

const fileInput = ref(null)
const loading = ref(false)
const checking = ref(false)
const relogining = ref(false)
const rawText = ref('')
const accounts = ref([])
const lastResults = ref({})
const checkProgress = ref({ done: 0, total: 0 })
const reloginProgress = ref({ done: 0, total: 0 })

const statusCount = computed(() => ({
  total: accounts.value.length,
  active: accounts.value.filter((a) => a.status === 'active').length,
  unauthorized: accounts.value.filter((a) => a.status === '401').length,
  revived: accounts.value.filter((a) => a.status === 'revived').length,
  deactivated: accounts.value.filter((a) => a.status === 'deactivated').length,
}))

const openFile = () => fileInput.value?.click()

const normalizeAccount = (item, idx) => {
  const credentials = item?.credentials && typeof item.credentials === 'object' ? item.credentials : item
  const extra = item?.extra && typeof item.extra === 'object' ? item.extra : {}
  const email = String(credentials?.email || item?.email || item?.name || extra?.email || '').trim().toLowerCase()
  const accessToken = String(credentials?.access_token || item?.access_token || '').trim()
  const refreshToken = String(credentials?.refresh_token || item?.refresh_token || '').trim()
  const idToken = String(credentials?.id_token || item?.id_token || '').trim()
  const password = String(credentials?.password || item?.password || '').trim()
  const totpSecret = String(credentials?.totp_secret || item?.totp_secret || '').trim()
  const workspaceId = String(
    credentials?.chatgpt_account_id
      || item?.chatgpt_account_id
      || credentials?.workspace_id
      || item?.workspace_id
      || credentials?.account_id
      || item?.account_id
      || extra?.workspace_id
      || '',
  ).trim()
  const userId = String(credentials?.chatgpt_user_id || item?.chatgpt_user_id || '').trim()
  const clientId = String(credentials?.client_id || item?.client_id || '').trim()
  const planType = String(credentials?.plan_type || item?.plan_type || 'team').trim()
  const proxy = String(item?.proxy || extra?.proxy || '').trim()
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
    proxy,
    status: 'unknown',
    quota: null,
    error: '',
    account: item,
  }
}

const parsePayload = (payload) => {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('JSON 格式不正确')
  }
  if (payload.type === 'sub2api-data') {
    return Array.isArray(payload.accounts) ? payload.accounts.map((a, idx) => normalizeAccount(a, idx)) : []
  }
  if (payload.platform === 'openai' && payload.type === 'oauth') {
    return [normalizeAccount(payload, 0)]
  }
  if (payload.type === 'codex' || payload.access_token || payload.refresh_token) {
    return [normalizeAccount(payload, 0)]
  }
  if (Array.isArray(payload.accounts)) {
    return payload.accounts.map((a, idx) => normalizeAccount(a, idx))
  }
  throw new Error('不支持的导入格式，只接受 sub2api-data / cpa 类 JSON')
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
    ElMessage.success(`已导入 ${accounts.value.length} 个账号`)
  } catch (e) {
    ElMessage.error(e.message || '导入失败')
  } finally {
    loading.value = false
  }
}

const updateOne = (targetId, patch) => {
  accounts.value = accounts.value.map((item) => (item.id === targetId ? { ...item, ...patch } : item))
}

const runConcurrent = async (items, limit, worker) => {
  let cursor = 0
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const current = cursor
      cursor += 1
      await worker(items[current], current)
    }
  })
  await Promise.all(workers)
}

const applyCheckResult = (item, result) => {
  if (!result) return
  lastResults.value = { ...lastResults.value, [item.id]: result }
  updateOne(item.id, {
    status: result.status === 'active' ? 'active' : result.status,
    email: result.email || item.email,
    workspace_id: result.workspace_id || item.workspace_id,
    quota: result.quota || null,
    error: result.error || '',
  })
}

const doCheck = async () => {
  if (!accounts.value.length) return ElMessage.warning('请先导入账号')
  checking.value = true
  checkProgress.value = { done: 0, total: accounts.value.length }
  accounts.value = accounts.value.map((item) => ({
    ...item,
    status: 'checking',
    quota: null,
    error: '',
  }))
  try {
    const snapshot = [...accounts.value]
    await runConcurrent(snapshot, 8, async (item) => {
      try {
        const res = await checkPublicRelogin({ accounts: [item], concurrency: 1 })
        const result = res.results?.[item.id] || Object.values(res.results || {})[0]
        applyCheckResult(item, result)
      } catch (e) {
        applyCheckResult(item, {
          ok: false,
          status: e.status === 401 ? '401' : 'error',
          email: item.email,
          workspace_id: item.workspace_id,
          error: e.message || '额度检查失败',
        })
      } finally {
        checkProgress.value = {
          ...checkProgress.value,
          done: checkProgress.value.done + 1,
        }
      }
    })
    ElMessage.success('额度检查完成')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    checking.value = false
  }
}

const doRelogin = async (onlyRevived = true) => {
  const list = accounts.value.filter((item) => item.status === '401' || (!onlyRevived && item.status !== 'deactivated'))
  if (!list.length) return ElMessage.warning('没有可重新登录的账号')
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
    // 每个账号单独请求，完成一个就立即刷新该行；前端并发数仍保持为 4。
    await runConcurrent(list, 4, async (item) => {
      try {
        const res = await runPublicRelogin({ accounts: [item], concurrency: 1 })
        const result = res.results?.[item.id] || Object.values(res.results || {})[0]
        if (!result) {
          throw new Error('服务器未返回重登录结果')
        }
        lastResults.value = { ...lastResults.value, [item.id]: result }
        if (result.status === 'revived' && result.account) {
          revived += 1
          const refreshed = normalizeAccount(result.account, 0)
          updateOne(item.id, {
            ...refreshed,
            id: item.id,
            proxy: item.proxy || refreshed.proxy,
            status: 'revived',
            error: '',
          })
        } else {
          updateOne(item.id, {
            status: result.status || 'failed',
            error: result.error || '',
          })
        }
      } catch (e) {
        const result = {
          ok: false,
          status: e.status === 401 ? '401' : 'failed',
          email: item.email,
          workspace_id: item.workspace_id,
          error: e.message || '重登录失败',
        }
        lastResults.value = { ...lastResults.value, [item.id]: result }
        updateOne(item.id, { status: result.status, error: result.error })
      } finally {
        reloginProgress.value = {
          ...reloginProgress.value,
          done: reloginProgress.value.done + 1,
        }
      }
    })
    ElMessage.success(`重登完成，成功 ${revived} 个`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    relogining.value = false
  }
}

const buildExport = (rows) => {
  const accountsOut = rows.map((item) => {
    const workspaceId = item.workspace_id || ''
    return {
      name: item.email,
      platform: 'openai',
      type: 'oauth',
      credentials: {
        access_token: item.access_token || '',
        email: item.email || '',
        password: item.password || '',
        totp_secret: item.totp_secret || '',
        expires_at: 0,
        refresh_token: item.refresh_token || '',
        chatgpt_account_id: workspaceId,
        chatgpt_user_id: item.chatgpt_user_id || '',
        client_id: item.client_id || '',
        id_token: item.id_token || '',
        plan_type: item.plan_type || 'team',
      },
      extra: {
        source: 'public_relogin',
        workspace_id: workspaceId,
        email: item.email || '',
      },
      group_ids: [4],
      priority: 1,
      concurrency: 10,
      rate_multiplier: 1,
      auto_pause_on_expired: true,
    }
  })
  return {
    type: 'sub2api-data',
    version: 1,
    exported_at: new Date().toISOString(),
    workspace_id: accountsOut[0]?.credentials?.chatgpt_account_id || '',
    proxies: [],
    accounts: accountsOut,
  }
}

const download = (mode) => {
  const filtered = accounts.value.filter((item) => item.status !== 'deactivated')
  const rows = mode === 'revived' ? filtered.filter((item) => item.status === 'revived') : filtered
  if (!rows.length) return ElMessage.warning('没有可下载的账号')
  const blob = new Blob([JSON.stringify(buildExport(rows), null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = mode === 'revived' ? 'sub2api-revived.json' : `sub2api-accounts-remaining-${rows.length}.json`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <span class="section-title" style="margin:0">公开 401 重登录</span>
      </template>
      <p class="hint">导入 sub2api / cpa JSON 后，在浏览器内完成解析、检查 401、密码 + 2FA 重登录和下载。</p>

      <div class="toolbar">
        <input ref="fileInput" type="file" accept="application/json,.json" class="hidden" @change="handleFile" />
        <el-button :loading="loading" @click="openFile">导入 JSON</el-button>
        <el-button :loading="checking" type="primary" @click="doCheck">检查额度 / 401</el-button>
        <el-button :loading="relogining" type="success" @click="doRelogin(true)">一键重新登录</el-button>
        <el-dropdown>
          <el-button>
            下载
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="download('revived')">仅下载复活项</el-dropdown-item>
              <el-dropdown-item @click="download('all')">下载全部（过滤停用）</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
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

      <el-table :data="accounts" style="margin-top: 16px" height="640" row-key="id">
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
            <el-tag v-else type="info" effect="plain">未知</el-tag>
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
  </div>
</template>

<style scoped>
.page { padding: 16px; }
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
