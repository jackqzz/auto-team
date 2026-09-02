<script setup>
import { computed, onActivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Icon } from '@iconify/vue'
import {
  listWorkspaceMasters, importWorkspaceSessions, getWorkspaceMaster,
  deleteWorkspaceMaster, bulkDeleteWorkspaceMasters, updateWorkspaceProxy, syncWorkspace,
  syncWorkspaceMembers,
} from '@/api/workspaces'
import { copyText, fmtTime } from '@/api/request'
import { isValidProxy } from '@/stores/proxy'
import StatusDot from '@/components/StatusDot.vue'

const PAGE_SIZE = 20
const rows = ref([])
const total = ref(0)
const page = ref(1)
const selected = ref([])
const loading = ref(false)
const syncingStats = ref({})
const syncingMembers = ref({})
const importing = ref(false)
const importVisible = ref(false)
const importText = ref('')
const importProxy = ref('')
const searchKeyword = ref('')
const router = useRouter()

function cst(value) {
  if (!value) return '未同步'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function setRowBusy(target, id, busy) {
  target.value = { ...target.value, [id]: busy }
}

const totalDefaultSeats = computed(() => {
  return rows.value.reduce((acc, cur) => acc + (Number(cur.seats_default) || 0), 0)
})

const totalDefaultEntitled = computed(() => {
  return rows.value.reduce((acc, cur) => acc + (Number(cur.seats_default_entitled) || 0), 0)
})

const totalProliteSeats = computed(() => {
  return rows.value.reduce((acc, cur) => acc + (Number(cur.seats_prolite) || 0), 0)
})

const totalProliteEntitled = computed(() => {
  return rows.value.reduce((acc, cur) => acc + (Number(cur.seats_prolite_entitled) || 0), 0)
})

const totalCodexSeats = computed(() => {
  return rows.value.reduce((acc, cur) => acc + (Number(cur.seats_usage_based) || 0), 0)
})

const filteredRows = computed(() => {
  if (!searchKeyword.value.trim()) return rows.value
  const kw = searchKeyword.value.trim().toLowerCase()
  return rows.value.filter(
    (r) =>
      String(r.account || '').toLowerCase().includes(kw) ||
      String(r.workspace_id || '').toLowerCase().includes(kw) ||
      String(r.proxy_preview || '').toLowerCase().includes(kw)
  )
})

async function sync(row) {
  setRowBusy(syncingStats, row.id, true)
  try {
    await syncWorkspace(row.id)
    ElMessage.success('席位统计已同步')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    setRowBusy(syncingStats, row.id, false)
  }
}

async function syncMembers(row) {
  setRowBusy(syncingMembers, row.id, true)
  try {
    const result = await syncWorkspaceMembers(row.id)
    ElMessage.success(
      `成员席位同步完成：更新 ${result.refreshed || 0}，未匹配 ${result.missing || 0}，剩余未知 ${result.remaining || 0}`
    )
    await load()
  } catch (e) {
    ElMessage.error(e.status === 429 ? '上游请求过于频繁，请稍后重试' : e.message)
  } finally {
    setRowBusy(syncingMembers, row.id, false)
  }
}

function candidates(row) {
  router.push({ name: 'workspace-candidates', query: { workspace_id: row.id } })
}

function handleWorkspaceUpdated() {
  load()
}

async function load(resetPage = false) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const result = await listWorkspaceMasters({
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
    })
    rows.value = result.items || []
    total.value = result.total || 0
  } catch (e) {
    ElMessage.error('加载母号列表失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

async function submitImport() {
  if (!importText.value.trim()) {
    ElMessage.warning('请输入母号 Session')
    return
  }
  if (importProxy.value.trim() && !isValidProxy(importProxy.value)) {
    ElMessage.warning('代理格式错误，应为 [协议://][user:pass@]host:port')
    return
  }
  importing.value = true
  try {
    const r = await importWorkspaceSessions(importText.value, importProxy.value.trim())
    ElMessage.success(`解析 ${r.parsed} 条：新增 ${r.inserted} / 更新 ${r.updated} / 跳过 ${r.skipped}`)
    importText.value = ''
    importVisible.value = false
    await load(true)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    importing.value = false
  }
}

async function copySession(row) {
  try {
    const { data } = await getWorkspaceMaster(row.id)
    await copyText(data.session_token || '')
  } catch (e) {
    ElMessage.error('读取 Session 失败: ' + e.message)
  }
}

async function editProxy(row) {
  try {
    const { data } = await getWorkspaceMaster(row.id)
    const { value } = await ElMessageBox.prompt(
      '此代理只属于该母号，后续母号请求不会使用注册代理池。',
      `修改代理 · ${row.account}`,
      {
        inputValue: data.proxy_url || '',
        inputPlaceholder: 'socks5://user:pass@host:port',
        confirmButtonText: '保存',
        cancelButtonText: '取消',
        inputValidator: (v) => isValidProxy(String(v || '')) || '代理格式错误',
      }
    )
    await updateWorkspaceProxy(row.id, value.trim())
    ElMessage.success('母号专属代理已更新')
    await load()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || String(e))
  }
}

async function confirmDelete(message) {
  try {
    await ElMessageBox.confirm(message, '确认删除', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    return true
  } catch (_) {
    return false
  }
}

async function deleteOne(row) {
  if (!(await confirmDelete(`删除母号“${row.account}”及其 Session？`))) return
  try {
    await deleteWorkspaceMaster(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function deleteSelected() {
  const ids = selected.value.map((row) => row.id)
  if (!ids.length) return
  if (!(await confirmDelete(`删除选中的 ${ids.length} 个 Team 母号及其 Session？`))) return
  try {
    const r = await bulkDeleteWorkspaceMasters(ids)
    selected.value = []
    ElMessage.success(`已删除 ${r.deleted} 个母号`)
    await load(true)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

watch(page, () => load())
onMounted(() => window.addEventListener('workspace-master-updated', handleWorkspaceUpdated))
onUnmounted(() => window.removeEventListener('workspace-master-updated', handleWorkspaceUpdated))
onActivated(() => load())
</script>

<template>
  <div class="workspaces-page">
    <!-- 顶部 Hero 统计看板 -->
    <div class="hero-card">
      <div class="hero-header-row">
        <div class="hero-title-area">
          <div class="hero-icon-box">
            <Icon icon="lucide:layers" class="hero-icon" />
          </div>
          <div>
            <h2 class="hero-title">Team 母号空间</h2>
            <p class="hero-desc">管理 Team 工作空间母号凭证、专属代理配置与席位配额全局监控</p>
          </div>
        </div>

        <div class="hero-action-buttons">
          <el-button type="primary" @click="importVisible = true">
            <Icon icon="lucide:upload" class="btn-icon" />
            导入母号 Session
          </el-button>
          <el-button plain :loading="loading" @click="load(false)">
            <Icon icon="lucide:refresh-cw" class="btn-icon" />
            刷新
          </el-button>
        </div>
      </div>

      <!-- KPI 数据卡片 -->
      <div class="hero-kpi-grid">
        <div class="kpi-card">
          <div class="kpi-header">
            <span class="kpi-title">母号总数</span>
            <Icon icon="lucide:building" class="kpi-type-icon" />
          </div>
          <div class="kpi-body">
            <div class="kpi-val">{{ total }}</div>
            <div class="kpi-hint">当前系统已托管母号空间</div>
          </div>
        </div>

        <div class="kpi-card">
          <div class="kpi-header">
            <span class="kpi-title">标准席位总量</span>
            <span class="kpi-dot dot-primary" />
          </div>
          <div class="kpi-body">
            <div class="kpi-val-row">
              <span class="kpi-val">{{ totalDefaultSeats }}</span>
              <span class="kpi-sub">/ {{ totalDefaultEntitled }} 席</span>
            </div>
            <div class="kpi-bar-track">
              <div
                class="kpi-bar-fill fill-primary"
                :style="{
                  width: `${Math.min(100, Math.round((totalDefaultSeats / Math.max(1, totalDefaultEntitled)) * 100))}%`
                }"
              />
            </div>
          </div>
          <div class="kpi-footer">
            <span>当前页占用率 {{ Math.round((totalDefaultSeats / Math.max(1, totalDefaultEntitled)) * 100) }}%</span>
          </div>
        </div>

        <div class="kpi-card">
          <div class="kpi-header">
            <span class="kpi-title">高级席位 (ProLite)</span>
            <span class="kpi-dot dot-warning" />
          </div>
          <div class="kpi-body">
            <div class="kpi-val-row">
              <span class="kpi-val">{{ totalProliteSeats }}</span>
              <span class="kpi-sub">/ {{ totalProliteEntitled }} 席</span>
            </div>
            <div class="kpi-bar-track">
              <div
                class="kpi-bar-fill fill-warning"
                :style="{
                  width: `${Math.min(100, Math.round((totalProliteSeats / Math.max(1, totalProliteEntitled)) * 100))}%`
                }"
              />
            </div>
          </div>
          <div class="kpi-footer">
            <span>当前页占用率 {{ Math.round((totalProliteSeats / Math.max(1, totalProliteEntitled)) * 100) }}%</span>
          </div>
        </div>

        <div class="kpi-card">
          <div class="kpi-header">
            <span class="kpi-title">Codex 席位 (Usage)</span>
            <span class="kpi-dot dot-info" />
          </div>
          <div class="kpi-body">
            <div class="kpi-val-row">
              <span class="kpi-val">{{ totalCodexSeats }}</span>
              <span class="kpi-sub">在用</span>
            </div>
            <div class="kpi-hint">按使用量计费席位</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 列表数据卡片 -->
    <el-card shadow="never" class="main-card">
      <!-- 搜索与批量操作工具栏 -->
      <div class="toolbar-row">
        <div class="search-wrap">
          <el-input
            v-model="searchKeyword"
            placeholder="搜索母号账号、Workspace ID 或代理..."
            clearable
            class="search-input"
          >
            <template #prefix>
              <Icon icon="lucide:search" class="search-icon" />
            </template>
          </el-input>
        </div>

        <div class="batch-action-wrap">
          <el-button
            type="danger"
            plain
            size="small"
            :disabled="!selected.length"
            @click="deleteSelected"
          >
            <Icon icon="lucide:trash-2" class="btn-icon" />
            批量删除 ({{ selected.length }})
          </el-button>
        </div>
      </div>

      <!-- 母号表格 -->
      <el-table
        v-loading="loading"
        :data="filteredRows"
        stripe
        class="modern-table"
        @selection-change="(val) => (selected = val)"
      >
        <el-table-column type="selection" width="46" />

        <!-- 母号账号与 ID -->
        <el-table-column label="母号与空间 ID" min-width="260">
          <template #default="{ row }">
            <div class="account-cell">
              <div class="account-main">
                <span class="account-email">{{ row.account }}</span>
                <button
                  class="mini-copy-btn"
                  title="复制母号邮箱"
                  @click.stop="copyText(row.account)"
                >
                  <Icon icon="lucide:copy" />
                </button>
              </div>
              <div class="account-sub">
                <span class="sub-label">ID:</span>
                <span class="sub-val mono">{{ row.workspace_id || '未提取' }}</span>
                <button
                  v-if="row.workspace_id"
                  class="mini-copy-btn"
                  title="复制 Workspace ID"
                  @click.stop="copyText(row.workspace_id)"
                >
                  <Icon icon="lucide:copy" />
                </button>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 席位状态 -->
        <el-table-column label="席位配额监控" min-width="240">
          <template #default="{ row }">
            <div class="seat-stats-cell">
              <div class="seat-pills-row">
                <div class="seat-pill default-pill">
                  <span class="seat-name">标准</span>
                  <span class="seat-nums">{{ row.seats_default ?? '-' }} / {{ row.seats_default_entitled ?? '-' }}</span>
                </div>
                <div class="seat-pill prolite-pill">
                  <span class="seat-name">ProLite</span>
                  <span class="seat-nums">{{ row.seats_prolite ?? '-' }} / {{ row.seats_prolite_entitled ?? '-' }}</span>
                </div>
                <div class="seat-pill codex-pill">
                  <span class="seat-name">Codex</span>
                  <span class="seat-nums">{{ row.seats_usage_based ?? '-' }}</span>
                </div>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 费用与续费周期 -->
        <el-table-column label="费用与续费" min-width="190">
          <template #default="{ row }">
            <div class="cost-cell">
              <div class="cost-val-row">
                <span class="cost-label">费用:</span>
                <span class="cost-val">{{ row.seat_cost || '未同步' }}</span>
              </div>
              <div class="renewal-row">
                <span class="renewal-label">到期:</span>
                <span class="renewal-val">{{ cst(row.renewal_date) }}</span>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- Session 与专属代理 -->
        <el-table-column label="Session & 专属代理" min-width="240">
          <template #default="{ row }">
            <div class="tech-cell">
              <div class="session-line">
                <el-button
                  text
                  size="small"
                  type="primary"
                  class="mono session-btn"
                  @click="copySession(row)"
                >
                  <Icon icon="lucide:key" class="btn-icon-xs" />
                  <span>{{ row.session_preview || '复制 Session' }}</span>
                  <el-tag size="small" type="info" effect="plain" class="len-tag">
                    len={{ row.session_len }}
                  </el-tag>
                </el-button>
              </div>

              <div class="proxy-line">
                <Icon icon="lucide:network" class="proxy-icon" />
                <span class="proxy-val mono">{{ row.proxy_preview || '未设置专属代理 (使用号池)' }}</span>
                <button
                  class="mini-copy-btn"
                  title="修改专属代理"
                  @click.stop="editProxy(row)"
                >
                  <Icon icon="lucide:edit-3" />
                </button>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 空间状态 -->
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StatusDot type="success" :text="row.status || '正常'" />
          </template>
        </el-table-column>

        <!-- 操作按钮 -->
        <el-table-column label="快捷操作" width="280" fixed="right">
          <template #default="{ row }">
            <div class="table-actions">
              <el-button
                size="small"
                type="primary"
                plain
                @click="candidates(row)"
              >
                <Icon icon="lucide:users" class="btn-icon" />
                候选管理
              </el-button>

              <el-dropdown trigger="click">
                <el-button size="small" plain>
                  更多
                  <Icon icon="lucide:chevron-down" class="btn-icon-end" />
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item
                      :disabled="syncingMembers[row.id]"
                      @click="sync(row)"
                    >
                      <Icon icon="lucide:refresh-cw" class="btn-icon" />
                      {{ syncingStats[row.id] ? '同步中…' : '同步席位统计' }}
                    </el-dropdown-item>
                    <el-dropdown-item
                      :disabled="syncingStats[row.id]"
                      @click="syncMembers(row)"
                    >
                      <Icon icon="lucide:user-check" class="btn-icon" />
                      {{ syncingMembers[row.id] ? '同步中…' : '同步成员席位' }}
                    </el-dropdown-item>
                    <el-dropdown-item @click="editProxy(row)">
                      <Icon icon="lucide:network" class="btn-icon" />
                      修改专属代理
                    </el-dropdown-item>
                    <el-dropdown-item @click="copySession(row)">
                      <Icon icon="lucide:copy" class="btn-icon" />
                      复制 Session Token
                    </el-dropdown-item>
                    <el-dropdown-item divided style="color: var(--el-color-danger)" @click="deleteOne(row)">
                      <Icon icon="lucide:trash" class="btn-icon" />
                      删除母号
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无 Team 母号空间，请先导入 Session" :image-size="70" />
        </template>
      </el-table>

      <!-- 分页栏 -->
      <div class="pagination-row">
        <el-pagination
          v-model:current-page="page"
          :page-size="PAGE_SIZE"
          :total="total"
          layout="total, prev, pager, next, jumper"
          background
        />
      </div>
    </el-card>

    <!-- 导入母号弹窗 -->
    <el-dialog
      v-model="importVisible"
      title="导入 Team 母号 Session"
      width="min(720px, 92vw)"
      top="8vh"
      class="modern-dialog"
    >
      <div class="dialog-notice-box">
        <Icon icon="lucide:shield-alert" class="notice-icon" />
        <div class="notice-content">
          <span class="notice-title">安全提示</span>
          <span class="notice-desc">Session 将以明文保存在本机数据库中，仅用于空间席位同步与自动化邀请，请勿泄露。</span>
        </div>
      </div>

      <el-form label-position="top" class="import-form">
        <el-form-item label="本批共用专属代理 (可选)">
          <el-input
            v-model="importProxy"
            class="mono"
            placeholder="socks5://user:pass@host:port (若单行已包含代理可留空)"
          >
            <template #prefix>
              <Icon icon="lucide:network" />
            </template>
          </el-input>
        </el-form-item>

        <el-form-item label="Session 凭证数据 (支持多格式混合输入)">
          <el-input
            v-model="importText"
            type="textarea"
            :rows="12"
            class="mono"
            placeholder="每行一个，支持以下格式：&#10;1. tmp.session.json (自动提取 email, accessToken, account.id)&#10;2. 母号邮箱----session----专属代理&#10;3. session_token&#10;4. JSON 对象: {&quot;email&quot;:&quot;...&quot;,&quot;session_token&quot;:&quot;...&quot;,&quot;proxy&quot;:&quot;...&quot;}"
          />
        </el-form-item>
      </el-form>

      <div class="dialog-footer-hint">
        共用代理将作为默认值应用；每行第三段或 JSON 中的 proxy 可单独覆盖。导入重复母号将自动更新 Session 和代理。
      </div>

      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport">
          确认导入
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.workspaces-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* Hero Section */
.hero-card {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-lg);
  padding: 18px 22px;
  box-shadow: var(--app-shadow-sm);
}

.hero-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}

.hero-title-area {
  display: flex;
  align-items: center;
  gap: 12px;
}

.hero-icon-box {
  width: 44px;
  height: 44px;
  border-radius: var(--app-radius-md);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
}

.hero-icon {
  font-size: 24px;
}

.hero-title {
  font-size: 18px;
  font-weight: 700;
  margin: 0 0 4px 0;
  color: var(--el-text-color-primary);
}

.hero-desc {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin: 0;
}

.hero-action-buttons {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* Hero KPI Grid */
.hero-kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}

.kpi-card {
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-md);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.kpi-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.kpi-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.kpi-type-icon {
  font-size: 16px;
  color: var(--el-color-primary);
}

.kpi-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.dot-primary { background: var(--el-color-primary); }
.dot-warning { background: var(--el-color-warning); }
.dot-info { background: #8b5cf6; }

.kpi-body {
  margin: 4px 0 6px;
}

.kpi-val {
  font-size: 22px;
  font-weight: 700;
  font-family: ui-monospace, SFMono-Regular, monospace;
  color: var(--el-text-color-primary);
}

.kpi-val-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-bottom: 6px;
}

.kpi-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.kpi-bar-track {
  height: 6px;
  width: 100%;
  background: var(--el-fill-color-dark);
  border-radius: 999px;
  overflow: hidden;
}

.kpi-bar-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.3s ease;
}

.fill-primary { background: var(--el-color-primary); }
.fill-warning { background: var(--el-color-warning); }

.kpi-footer {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.kpi-hint {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
}

/* Main Table Section */
.main-card {
  border-radius: var(--app-radius-lg);
}

.toolbar-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.search-wrap {
  min-width: 320px;
}

.search-icon {
  font-size: 15px;
  color: var(--el-text-color-secondary);
}

.batch-action-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
}

.modern-table :deep(.el-table__header) th {
  background: var(--el-fill-color-light);
  font-weight: 600;
  font-size: 12px;
  color: var(--el-text-color-primary);
}

.account-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.account-main {
  display: flex;
  align-items: center;
  gap: 6px;
}

.account-email {
  font-weight: 600;
  font-size: 13px;
  color: var(--el-text-color-primary);
}

.account-sub {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.sub-label {
  color: var(--el-text-color-placeholder);
}

.sub-val {
  color: var(--el-text-color-regular);
}

.seat-stats-cell {
  display: flex;
  flex-direction: column;
}

.seat-pills-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.seat-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border-radius: var(--app-radius-xs);
  font-size: 11px;
  border: 1px solid transparent;
}

.seat-pill .seat-name {
  font-weight: 500;
}

.seat-pill .seat-nums {
  font-family: ui-monospace, SFMono-Regular, monospace;
}

.default-pill {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-7);
}

.prolite-pill {
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning);
  border-color: var(--el-color-warning-light-7);
}

.codex-pill {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  border-color: var(--el-border-color-lighter);
}

.cost-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
}

.cost-val-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.cost-label, .renewal-label {
  color: var(--el-text-color-secondary);
}

.cost-val {
  font-weight: 600;
  color: var(--el-color-success);
  font-family: ui-monospace, SFMono-Regular, monospace;
}

.renewal-row {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
}

.renewal-val {
  font-family: ui-monospace, SFMono-Regular, monospace;
  color: var(--el-text-color-regular);
}

.tech-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.session-line {
  display: flex;
  align-items: center;
}

.session-btn {
  padding: 0;
  height: auto;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.len-tag {
  font-size: 10px;
  padding: 0 4px;
}

.proxy-line {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.proxy-icon {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.proxy-val {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mini-copy-btn {
  background: transparent;
  border: none;
  padding: 2px;
  cursor: pointer;
  color: var(--el-text-color-placeholder);
  display: inline-flex;
  align-items: center;
  border-radius: var(--app-radius-xs);
  transition: color 0.15s;
}

.mini-copy-btn:hover {
  color: var(--el-color-primary);
  background: var(--el-fill-color);
}

.table-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pagination-row {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}

/* Dialog Styles */
.dialog-notice-box {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  background: var(--el-color-warning-light-9);
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: var(--app-radius-md);
  margin-bottom: 16px;
}

.notice-icon {
  font-size: 18px;
  color: var(--el-color-warning);
  flex-shrink: 0;
  margin-top: 2px;
}

.notice-content {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.notice-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.notice-desc {
  font-size: 12px;
  color: var(--el-text-color-regular);
  line-height: 1.4;
}

.dialog-footer-hint {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
  margin-top: 8px;
}

.btn-icon {
  margin-right: 4px;
  font-size: 14px;
}

.btn-icon-xs {
  font-size: 12px;
}

.btn-icon-end {
  margin-left: 4px;
  font-size: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
