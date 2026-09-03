<script setup>
import { computed, nextTick, onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Icon } from '@iconify/vue'
import {
  listAccounts,
  deleteAccount,
  bulkDeleteAccounts,
  resetFailed,
  resetAccount,
  bulkResetAccounts,
  releaseStale,
  setAccountsGroup,
  createAccountGroup,
  renameAccountGroup,
  deleteAccountGroup,
  updateAccountPassword,
} from '@/api/accounts'
import { getMailProviders } from '@/api/settings'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import { copyText } from '@/api/request'
import { PAGE_SIZE_OPTIONS, SELECT_ALL_FETCH_LIMIT } from '@/utils/pagination'

const router = useRouter()
const statsStore = useStatsStore()
const { stats } = storeToRefs(statsStore)
const runtime = useRuntimeStore()
const { dataVersion } = storeToRefs(runtime)

const pageSize = ref(20)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const statusFilter = ref('')
const kindFilter = ref('')
const groupFilter = ref('__all__')
const selected = ref([])
const loading = ref(false)
const accountTableRef = ref(null)

const providers = ref([])
const byKind = ref({})
const groups = ref([])
const groupManagerVisible = ref(false)

const statusTabs = [
  { label: '全部', value: '', icon: 'lucide:layers' },
  { label: '可用 available', value: 'available', icon: 'lucide:check-circle-2' },
  { label: '进行中 in_use', value: 'in_use', icon: 'lucide:loader-2' },
  { label: '已完成 done', value: 'done', icon: 'lucide:badge-check' },
  { label: '失败 failed', value: 'failed', icon: 'lucide:alert-octagon' },
]

const kindOptions = computed(() =>
  providers.value
    .filter((p) => p.pooled)
    .map((p) => ({
      kind: p.kind,
      label: p.display_name,
      count: byKind.value[p.kind]?.total || 0,
    })),
)

function kindLabel(k) {
  return providers.value.find((p) => p.kind === k)?.display_name || k || 'outlook'
}

async function loadProviders() {
  try {
    providers.value = (await getMailProviders()).providers || []
  } catch (_) {
    /* ignore */
  }
}

async function load(resetPage) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const { items, total: t, by_kind, groups: groupItems } = await listAccounts({
      status: statusFilter.value,
      kind: kindFilter.value,
      group_name: groupFilter.value,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    rows.value = items
    total.value = t
    byKind.value = by_kind || {}
    groups.value = groupItems || []
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function clearSelection() {
  // 开了 reserve-selection 后，只清 selected 不会取消表格里已勾的行，
  // 必须走表格实例的 clearSelection 才能把跨页保留的勾选一起清掉。
  accountTableRef.value?.clearSelection()
  selected.value = []
}

async function selectAllFiltered() {
  try {
    const { items, total: t } = await listAccounts({
      status: statusFilter.value,
      kind: kindFilter.value,
      group_name: groupFilter.value,
      limit: SELECT_ALL_FETCH_LIMIT,
      offset: 0,
    })
    const all = items || []
    const table = accountTableRef.value
    if (!table) return ElMessage.warning('列表尚未加载完成')
    table.clearSelection()
    await nextTick()
    // 全量结果里只有当前页那部分行存在于表格中；靠 row-key="email" 匹配，
    // 其余行由 selected 兜住，翻页时 reserve-selection 会自动补上勾选态。
    all.forEach((row) => table.toggleRowSelection(row, true))
    selected.value = all
    if (Number(t || 0) > all.length) {
      // 真超过单次拉取上限时必须明说，否则用户以为选全了、批量操作却只落到前一批。
      ElMessage.warning(`已选 ${all.length} 个，但当前筛选共 ${t} 个，超出单次上限未全部选中，请收窄筛选条件`)
    } else {
      ElMessage.success(`已全选当前筛选条件下的 ${all.length} 个邮箱`)
    }
  } catch (e) {
    ElMessage.error('全选失败: ' + e.message)
  }
}

function afterMutate() {
  // reserve-selection 会跨页记住勾选，但增删改之后被选中的行可能已经不存在了，
  // 留着会让后续批量操作打到已删的号上，所以每次变更后都清干净重新选。
  clearSelection()
  load()
  statsStore.refresh()
}

async function confirm(msg, title = '确认') {
  try {
    await ElMessageBox.confirm(msg, title, {
      type: 'warning',
      confirmButtonText: '确定',
      cancelButtonText: '取消',
    })
    return true
  } catch (_) {
    return false
  }
}

async function resetFailedAll() {
  if (!(await confirm('把所有 failed 号重置为 available？'))) return
  try {
    const r = await resetFailed()
    ElMessage.success(`重置 ${r.reset} 个`)
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function releaseStaleAll() {
  try {
    const r = await releaseStale()
    ElMessage.success(`释放 ${r.released} 个卡死号`)
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function resetSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`重置选中的 ${emails.length} 个号为 available？（已保存凭证不变）`))) return
  try {
    const r = await bulkResetAccounts(emails)
    ElMessage.success(`已重置 ${r.reset} 个`)
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`确定删除选中的 ${emails.length} 个号？(不可恢复)`))) return
  try {
    const r = await bulkDeleteAccounts({ emails })
    ElMessage.success(`已删除 ${r.deleted} 个`)
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
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
    ElMessage.success(`已更新 ${r.updated} 个邮箱的分组`)
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function addGroup() {
  try {
    const { value } = await ElMessageBox.prompt('分组可先为空，之后再移动账号进去。', '新增分组', {
      inputPlaceholder: '例如：8月采购',
      confirmButtonText: '新增',
      cancelButtonText: '取消',
      inputValidator: (v) => String(v || '').trim().length > 0 || '分组名称不能为空',
    })
    await createAccountGroup(value.trim())
    ElMessage.success('分组已新增')
    afterMutate()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || String(e))
  }
}

async function renameGroup(group) {
  try {
    const { value } = await ElMessageBox.prompt('账号会保留在改名后的分组。', '重命名分组', {
      inputValue: group.name,
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValidator: (v) => String(v || '').trim().length > 0 || '分组名称不能为空',
    })
    const r = await renameAccountGroup(group.name, value.trim())
    if (groupFilter.value === group.name) groupFilter.value = value.trim()
    ElMessage.success(`分组已改名，移动 ${r.moved} 个账号`)
    afterMutate()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || String(e))
  }
}

async function removeGroup(group) {
  if (
    !(await confirm(`删除分组“${group.name}”？其中 ${group.total} 个账号会保留并归入未分组。`, '删除分组'))
  )
    return
  try {
    const r = await deleteAccountGroup(group.name)
    if (groupFilter.value === group.name) groupFilter.value = ''
    ElMessage.success(`已删除分组，${r.ungrouped} 个账号归入未分组`)
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function bulkDeleteByStatus(statusVal) {
  const tip =
    statusVal === 'all'
      ? '这会删除邮箱列表里所有号（含未注册的），确定？'
      : `确定删除全部 ${statusVal} 状态的号？`
  if (!(await confirm(tip))) return
  try {
    const r = await bulkDeleteAccounts({ status: statusVal })
    ElMessage.success(`已删除 ${r.deleted} 个 ${statusVal} 号`)
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function useAccount(email) {
  router.push({ path: '/register', query: { email } })
}

async function resetOne(email) {
  if (!(await confirm(`重置 ${email} 为 available？`))) return
  try {
    await resetAccount(email)
    ElMessage.success('已重置')
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function deleteOne(email) {
  if (!(await confirm(`删除 ${email}？`))) return
  try {
    await deleteAccount(email)
    ElMessage.success('已删除')
    afterMutate()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function enterPassword(row) {
  try {
    const { value } = await ElMessageBox.prompt(
      `为 ${row.email} 录入 OpenAI 登录密码。该操作只修改本地记录，不会修改远端账号密码。`,
      '录入账号密码',
      {
        inputType: 'password',
        inputValue: '',
        inputPlaceholder: '请输入网页版已经创建好的密码',
        confirmButtonText: '保存',
        cancelButtonText: '取消',
        inputValidator: (v) => String(v || '').trim().length > 0 || '密码不能为空',
      },
    )
    await updateAccountPassword(row.email, String(value || '').trim())
    ElMessage.success('密码已录入')
    await load(false)
    statsStore.refresh()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || String(e))
  }
}

watch(page, () => load())
watch(pageSize, () => { page.value = 1; clearSelection(); load() })
watch([statusFilter, kindFilter, groupFilter], () => clearSelection())
watch(dataVersion, () => load())
onActivated(() => load())
loadProviders()
</script>

<template>
  <div class="page-container">
    <!-- Hero KPI Metrics Grid -->
    <div class="hero-kpi-grid">
      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">邮箱总数</span>
          <Icon icon="lucide:mail" class="kpi-type-icon" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val">{{ stats.total || 0 }}</div>
          <div class="kpi-hint">系统当前导入的待注册/已注册邮箱</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item"><i class="dot dot-success" /> 可用: {{ stats.available || 0 }}</span>
          <span class="kpi-sub-item"><i class="dot dot-warning" /> 占用: {{ stats.in_use || 0 }}</span>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">已完成注册</span>
          <Icon icon="lucide:check-circle-2" class="kpi-type-icon text-success" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-success">{{ stats.done || 0 }}</div>
          <div class="kpi-hint">已转为已注册账号托管</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item">
            完成率: {{ stats.total ? Math.round(((stats.done || 0) / stats.total) * 100) : 0 }}%
          </span>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">失败或异常</span>
          <Icon icon="lucide:alert-circle" class="kpi-type-icon text-danger" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-danger">{{ stats.failed || 0 }}</div>
          <div class="kpi-hint">支持一键重置为 available 重跑</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item"><i class="dot dot-danger" /> 待重置: {{ stats.failed || 0 }}</span>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">分组及来源分类</span>
          <Icon icon="lucide:folder-tree" class="kpi-type-icon" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-primary">{{ groups.length }} <span style="font-size: 14px; font-weight: normal; color: var(--el-text-color-secondary)">个分组</span></div>
          <div class="kpi-hint">{{ kindOptions.length > 1 ? `${kindOptions.length} 种邮箱协议混放` : '单一协议邮箱池' }}</div>
        </div>
        <div class="kpi-footer">
          <el-button link size="small" type="primary" @click="groupManagerVisible = true">管理分组</el-button>
          <el-button link size="small" @click="router.push('/import')">导入新号</el-button>
        </div>
      </div>
    </div>

    <!-- Segment Filter Bar -->
    <div class="filter-segment-bar">
      <div class="segment-tabs">
        <button
          v-for="item in statusTabs"
          :key="item.value"
          class="segment-tab-btn"
          :class="{ active: statusFilter === item.value }"
          @click="statusFilter = item.value; load(true)"
        >
          <Icon :icon="item.icon" class="tab-icon" />
          <span>{{ item.label }}</span>
        </button>
      </div>

      <div class="filter-selectors">
        <!-- Mail Provider Source Select -->
        <el-select
          v-if="kindOptions.length > 1"
          v-model="kindFilter"
          placeholder="全部来源"
          size="small"
          style="width: 170px"
          @change="load(true)"
        >
          <el-option label="全部来源" value="" />
          <el-option
            v-for="o in kindOptions"
            :key="o.kind"
            :label="`${o.label} (${o.count})`"
            :value="o.kind"
          />
        </el-select>

        <!-- Group Selector -->
        <el-select v-model="groupFilter" size="small" style="width: 170px" @change="load(true)">
          <el-option label="全部分组" value="__all__" />
          <el-option label="未分组" value="" />
          <el-option
            v-for="g in groups.filter((g) => g.name)"
            :key="g.name"
            :label="`${g.name} (${g.total})`"
            :value="g.name"
          />
        </el-select>

        <el-button size="small" class="refresh-btn" :loading="loading" @click="load(false)">
          <Icon icon="lucide:refresh-cw" />
        </el-button>
      </div>
    </div>

    <!-- Main Table & Actions Card -->
    <el-card shadow="never" class="main-card">
      <!-- Toolbar Section -->
      <div class="workflow-action-bar">
        <div class="action-left">
          <el-button plain :disabled="!total" @click="selectAllFiltered" class="action-btn">
            <Icon icon="lucide:list-checks" class="btn-icon" /> 全选当前筛选
          </el-button>

          <el-button plain :disabled="!selected.length" @click="clearSelection" class="action-btn">
            <Icon icon="lucide:square-dashed" class="btn-icon" /> 清空选择
          </el-button>

          <el-button type="primary" plain :disabled="!selected.length" @click="resetSelected" class="action-btn">
            <Icon icon="lucide:refresh-ccw" class="btn-icon" /> 重置选中 ({{ selected.length }})
          </el-button>

          <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected" class="action-btn">
            <Icon icon="lucide:trash-2" class="btn-icon" /> 删除选中 ({{ selected.length }})
          </el-button>

          <el-dropdown trigger="click" :disabled="!selected.length" @command="moveSelectedToGroup">
            <el-button plain :disabled="!selected.length" class="action-btn">
              <Icon icon="lucide:folder-input" class="btn-icon" /> 移动分组
              <Icon icon="lucide:chevron-down" style="margin-left: 4px" />
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="__ungrouped__">移动到未分组</el-dropdown-item>
                <el-dropdown-item v-for="g in groups" :key="g.name" :command="g.name">
                  移动到 {{ g.name }}
                </el-dropdown-item>
                <el-dropdown-item divided command="__manage__">
                  <Icon icon="lucide:settings" style="margin-right: 4px" /> 管理分组
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <el-button @click="resetFailedAll" class="action-btn">
            <Icon icon="lucide:rotate-ccw" class="btn-icon" /> 一键重置 Failed
          </el-button>

          <el-button @click="releaseStaleAll" class="action-btn">
            <Icon icon="lucide:unlock" class="btn-icon" /> 释放卡死号
          </el-button>
        </div>

        <div class="action-right">
          <el-dropdown @command="bulkDeleteByStatus">
            <el-button type="danger" plain size="small" class="action-btn">
              <Icon icon="lucide:trash" class="btn-icon" /> 批量按状态清理
              <Icon icon="lucide:chevron-down" style="margin-left: 4px" />
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="failed">删除全部 failed 邮箱</el-dropdown-item>
                <el-dropdown-item command="done">删除全部 done 邮箱</el-dropdown-item>
                <el-dropdown-item command="available">删除全部 available 邮箱</el-dropdown-item>
                <el-dropdown-item command="in_use">删除全部 in_use 邮箱</el-dropdown-item>
                <el-dropdown-item divided command="all" style="color: var(--el-color-danger)">
                  删除全部邮箱（清空池）
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <!-- Account Table -->
      <el-table
        ref="accountTableRef"
        v-loading="loading"
        :data="rows"
        style="width: 100%; margin-top: 12px"
        height="600"
        row-key="email"
        class="modern-table"
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="46" align="center" :reserve-selection="true" />

        <el-table-column prop="email" label="邮箱地址" min-width="260">
          <template #default="{ row }">
            <div class="account-cell">
              <div class="email-row">
                <span class="email-text">{{ row.email }}</span>
                <el-button link size="small" class="mini-copy-btn" @click="copyText(row.email)" title="复制邮箱">
                  <Icon icon="lucide:copy" />
                </el-button>
              </div>
              <div class="meta-row">
                <span v-if="row.group_name" class="meta-tag group-tag">
                  <Icon icon="lucide:folder" style="font-size: 11px" /> {{ row.group_name }}
                </span>
                <span v-else class="meta-tag ungrouped-tag">未分组</span>
                <span v-if="kindOptions.length > 1" class="meta-tag source-tag">
                  {{ kindLabel(row.kind) }}
                </span>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag
              v-if="row.status === 'available'"
              type="success"
              effect="plain"
              class="status-badge"
            >
              <i class="dot dot-success" /> 可用 available
            </el-tag>
            <el-tag
              v-else-if="row.status === 'in_use'"
              type="warning"
              effect="plain"
              class="status-badge"
            >
              <i class="dot dot-warning" /> 占用 in_use
            </el-tag>
            <el-tag
              v-else-if="row.status === 'done'"
              type="primary"
              effect="plain"
              class="status-badge"
            >
              <i class="dot dot-primary" /> 完成 done
            </el-tag>
            <el-tag
              v-else-if="row.status === 'failed'"
              type="danger"
              effect="plain"
              class="status-badge"
            >
              <i class="dot dot-danger" /> 失败 failed
            </el-tag>
            <el-tag v-else type="info" effect="plain" class="status-badge">{{ row.status }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="fail_reason" label="失败原因 / 备注" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.fail_reason" class="error-reason text-danger">
              <Icon icon="lucide:alert-circle" /> {{ row.fail_reason }}
            </span>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="220" fixed="right" align="center">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="useAccount(row.email)">
              测试单注册
            </el-button>
            <el-button size="small" text @click="enterPassword(row)">
              录入密码
            </el-button>
            <el-button
              v-if="row.status === 'done' || row.status === 'failed'"
              size="small"
              text
              type="primary"
              @click="resetOne(row.email)"
            >
              重置
            </el-button>
            <el-button size="small" text type="danger" @click="deleteOne(row.email)">
              删除
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无数据，可前往「批量导入」添加邮箱" :image-size="70" />
        </template>
      </el-table>

      <!-- Pagination -->
      <div class="pagination-footer">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="PAGE_SIZE_OPTIONS"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </el-card>

    <!-- Group Management Dialog -->
    <el-dialog v-model="groupManagerVisible" title="分组管理" width="min(600px, 92vw)" append-to-body>
      <div class="group-dialog-header">
        <span class="text-muted" style="font-size: 13px">管理已创建的账号分组与容量</span>
        <el-button type="primary" size="small" @click="addGroup">
          <Icon icon="lucide:plus" style="margin-right: 4px" /> 新增分组
        </el-button>
      </div>

      <el-table :data="groups" size="small" border class="modern-table" style="margin-top: 12px">
        <el-table-column prop="name" label="分组名称" min-width="180">
          <template #default="{ row }">
            <span class="group-name-text">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="total" label="总账号数" width="90" align="center" />
        <el-table-column prop="available" label="可用数" width="90" align="center">
          <template #default="{ row }">
            <span class="text-success">{{ row.available }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" align="center">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="renameGroup(row)">改名</el-button>
            <el-button size="small" text type="danger" @click="removeGroup(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有自定义分组" :image-size="50" />
        </template>
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-container {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* Hero KPI Grid */
.hero-kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.kpi-card {
  background: var(--el-bg-color-overlay, #ffffff);
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: var(--app-radius-md);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  box-shadow: var(--app-shadow-sm);
  transition: transform 0.2s, box-shadow 0.2s;
}

.kpi-card:hover {
  transform: translateY(-1px);
  box-shadow: var(--app-shadow-md);
}

.kpi-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.kpi-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-secondary, #909399);
}

.kpi-type-icon {
  font-size: 18px;
  color: var(--el-text-color-secondary, #909399);
}

.kpi-body {
  margin-bottom: 8px;
}

.kpi-val {
  font-size: 24px;
  font-weight: 700;
  color: var(--el-text-color-primary, #303133);
  line-height: 1.2;
}

.kpi-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
  margin-top: 4px;
}

.kpi-footer {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: var(--el-text-color-regular, #606266);
  border-top: 1px dashed var(--el-border-color-lighter, #ebeef5);
  padding-top: 8px;
  margin-top: auto;
}

.kpi-sub-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}

.dot-primary { background-color: var(--el-color-primary, #409eff); }
.dot-success { background-color: var(--el-color-success, #67c23a); }
.dot-warning { background-color: var(--el-color-warning, #e6a23c); }
.dot-danger { background-color: var(--el-color-danger, #f56c6c); }

.text-primary { color: var(--el-color-primary, #409eff); }
.text-success { color: var(--el-color-success, #67c23a); }
.text-warning { color: var(--el-color-warning, #e6a23c); }
.text-danger { color: var(--el-color-danger, #f56c6c); }
.text-muted { color: var(--el-text-color-secondary, #909399); }

/* Segment Filter Bar */
.filter-segment-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--el-bg-color-overlay, #ffffff);
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: var(--app-radius-md);
  padding: 6px 12px;
  flex-wrap: wrap;
  gap: 10px;
}

.segment-tabs {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.segment-tab-btn {
  border: none;
  background: transparent;
  padding: 6px 12px;
  border-radius: var(--app-radius-sm);
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-regular, #606266);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition: all 0.2s;
}

.segment-tab-btn:hover {
  background: var(--el-fill-color-light, #f5f7fa);
  color: var(--el-color-primary, #409eff);
}

.segment-tab-btn.active {
  background: var(--el-color-primary-light-9, #ecf5ff);
  color: var(--el-color-primary, #409eff);
  font-weight: 600;
}

.tab-icon {
  font-size: 14px;
}

.filter-selectors {
  display: flex;
  align-items: center;
  gap: 8px;
}

.refresh-btn {
  padding: 8px;
}

/* Workflow Action Bar */
.main-card {
  border-radius: var(--app-radius-md);
}

.workflow-action-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter, #ebeef5);
}

.action-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.btn-icon {
  font-size: 14px;
}

/* Table cells */
.account-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.email-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.email-text {
  font-family: var(--el-font-family-monospace, monospace);
  font-weight: 600;
  font-size: 13px;
  color: var(--el-text-color-primary, #303133);
}

.mini-copy-btn {
  padding: 0;
  height: auto;
  color: var(--el-text-color-secondary, #909399);
}

.mini-copy-btn:hover {
  color: var(--el-color-primary, #409eff);
}

.meta-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.meta-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: var(--app-radius-xs);
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.group-tag {
  background: var(--el-color-primary-light-9, #ecf5ff);
  color: var(--el-color-primary, #409eff);
  border: 1px solid var(--el-color-primary-light-7, #b3d8ff);
}

.ungrouped-tag {
  background: var(--el-fill-color-light, #f5f7fa);
  color: var(--el-text-color-secondary, #909399);
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
}

.source-tag {
  background: var(--el-fill-color-light, #f5f7fa);
  color: var(--el-text-color-regular, #606266);
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
}

.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
}

.error-reason {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}

.pagination-footer {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.group-dialog-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.group-name-text {
  font-weight: 600;
}
</style>
