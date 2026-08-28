<script setup>
import { onActivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
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
const router = useRouter()
function cst(value) { if (!value) return '未同步'; return new Intl.DateTimeFormat('zh-CN', { timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false }).format(new Date(value)) }
function setRowBusy(target, id, busy) {
  target.value = { ...target.value, [id]: busy }
}
async function sync(row) {
  setRowBusy(syncingStats, row.id, true)
  try {
    await syncWorkspace(row.id)
    ElMessage.success('席位统计已同步')
    await load()
  } catch(e) {
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
      `成员席位同步完成：更新 ${result.refreshed || 0}，未匹配 ${result.missing || 0}，剩余未知 ${result.remaining || 0}`,
    )
    await load()
  } catch(e) {
    ElMessage.error(e.status === 429 ? '上游请求过于频繁，请稍后重试' : e.message)
  } finally {
    setRowBusy(syncingMembers, row.id, false)
  }
}
function candidates(row) { router.push({ name:'workspace-candidates', query:{ workspace_id:row.id } }) }
function handleWorkspaceUpdated() { load() }

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
  } finally { loading.value = false }
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
  } finally { importing.value = false }
}

async function copySession(row) {
  try {
    const { data } = await getWorkspaceMaster(row.id)
    await copyText(data.session_token || '')
  } catch (e) { ElMessage.error('读取 Session 失败: ' + e.message) }
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
        confirmButtonText: '保存', cancelButtonText: '取消',
        inputValidator: (v) => isValidProxy(String(v || '')) || '代理格式错误',
      },
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
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
    return true
  } catch (_) { return false }
}

async function deleteOne(row) {
  if (!(await confirmDelete(`删除母号“${row.account}”及其 Session？`))) return
  try {
    await deleteWorkspaceMaster(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) { ElMessage.error(e.message) }
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
  } catch (e) { ElMessage.error(e.message) }
}

watch(page, () => load())
onMounted(() => window.addEventListener('workspace-master-updated', handleWorkspaceUpdated))
onUnmounted(() => window.removeEventListener('workspace-master-updated', handleWorkspaceUpdated))
onActivated(() => load())
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="header-row">
          <span class="section-title" style="margin: 0">空间管理</span>
          <span class="hint">Team 工作空间母号列表</span>
        </div>
      </template>

      <el-alert
        title="当前为第一阶段：仅保存和管理 Team 母号 Session"
        description="成员邀请、空间信息同步与额度管理将在后续接入。Session 属于敏感登录凭证，请勿泄露。"
        type="info" :closable="false" show-icon style="margin-bottom: 14px"
      />

      <el-space wrap style="margin-bottom: 12px">
        <el-button type="primary" @click="importVisible = true">
          <el-icon><Upload /></el-icon>导入母号 Session
        </el-button>
        <el-button @click="load(false)"><el-icon><Refresh /></el-icon>刷新</el-button>
        <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected">
          删除选中 ({{ selected.length }})
        </el-button>
      </el-space>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated />
      <el-table
        v-else v-loading="loading" :data="rows" stripe size="small"
        @selection-change="(value) => (selected = value)"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column prop="account" label="母号" min-width="230" show-overflow-tooltip />
        <el-table-column prop="workspace_id" label="Workspace ID" min-width="230" show-overflow-tooltip>
          <template #default="{ row }">{{ row.workspace_id || '未提取' }}</template>
        </el-table-column>
        <el-table-column label="席位数量" width="250">
          <template #default="{ row }">
            <div>标准 {{ row.seats_default ?? '-' }} / {{ row.seats_default_entitled ?? '-' }} · ProLite {{ row.seats_prolite ?? '-' }} / {{ row.seats_prolite_entitled ?? '-' }} · Codex {{ row.seats_usage_based ?? '-' }}</div>
            <div class="hint">成员数 / 已购数（标准、ProLite 为订阅席位；Codex=Usage-based）</div>
          </template>
        </el-table-column>
        <el-table-column prop="seat_cost" label="席位费用" width="130"><template #default="{row}">{{ row.seat_cost || '未同步' }}</template></el-table-column>
        <el-table-column label="订阅到期时间 (CST)" width="190"><template #default="{row}">{{ cst(row.renewal_date) }}</template></el-table-column>
        <el-table-column label="Session" min-width="210">
          <template #default="{ row }">
            <el-button text type="primary" class="mono" @click="copySession(row)">
              {{ row.session_preview }}
              <el-tag size="small" type="info" style="margin-left: 8px">len={{ row.session_len }}</el-tag>
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="专属代理" min-width="230" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono">{{ row.proxy_preview }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <StatusDot type="success" :text="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="导入时间" width="180">
          <template #default="{ row }">{{ fmtTime(row.imported_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="520" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="copySession(row)">复制 Session</el-button>
            <el-button size="small" text type="warning" @click="editProxy(row)">修改代理</el-button>
            <el-button
              size="small" text type="success" :loading="syncingStats[row.id]"
              :disabled="syncingMembers[row.id]" @click="sync(row)"
            >同步席位统计</el-button>
            <el-button
              size="small" text type="primary" :loading="syncingMembers[row.id]"
              :disabled="syncingStats[row.id]" @click="syncMembers(row)"
            >同步成员席位</el-button>
            <el-button size="small" text type="primary" @click="candidates(row)">候选管理</el-button>
            <el-button size="small" text type="danger" @click="deleteOne(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无 Team 母号，请先导入 Session" :image-size="70" />
        </template>
      </el-table>

      <div style="display: flex; justify-content: center; margin-top: 14px">
        <el-pagination
          v-model:current-page="page" :page-size="PAGE_SIZE" :total="total"
          layout="prev, pager, next, total" background
        />
      </div>
    </el-card>

    <el-dialog v-model="importVisible" title="导入 Team 母号 Session" width="min(720px, 92vw)" top="8vh">
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 14px">
        <template #title>Session 将以明文保存在本机数据库中</template>
      </el-alert>
      <el-input
        v-model="importProxy" class="mono" style="margin-bottom: 12px"
        placeholder="本批共用代理（如每行第三段已提供可留空）"
      >
        <template #prepend>本批代理</template>
      </el-input>
      <el-input
        v-model="importText" type="textarea" :rows="12" class="mono"
        placeholder="每行一个，支持：&#10;tmp.session.json（自动提取 email、accessToken、account.id）&#10;母号邮箱----session----专属代理&#10;session&#10;或 JSON：{&quot;email&quot;:&quot;母号邮箱&quot;,&quot;session_token&quot;:&quot;session&quot;,&quot;proxy&quot;:&quot;代理&quot;}"
      />
      <div class="hint" style="margin-top: 10px; line-height: 1.7">
        上方代理应用于本批全部母号；每行第三段或 JSON proxy 可单独覆盖。重复母号会同时更新 Session 和代理。
      </div>
      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport">确认导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.header-row { display: flex; align-items: center; gap: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
</style>
