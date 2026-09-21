<script setup>
import { computed, onActivated, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Icon } from '@iconify/vue'
import { listRedeemCodes, deleteRedeemCodes } from '@/api/redeemCodes'
import { listWorkspaceMasters } from '@/api/workspaces'
import { copyText, fmtTime } from '@/api/request'

const rows = ref([])
const workspaces = ref([])
const loading = ref(false)
const deleting = ref(false)
const workspaceFilter = ref(0)
const keyword = ref('')
const selected = ref([])

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return rows.value
  return rows.value.filter(
    (r) =>
      String(r.code || '').toLowerCase().includes(kw) ||
      String(r.email || '').toLowerCase().includes(kw) ||
      String(r.master_account || '').toLowerCase().includes(kw),
  )
})

async function load() {
  loading.value = true
  try {
    const r = await listRedeemCodes(workspaceFilter.value || 0)
    rows.value = r.codes || []
  } catch (e) {
    ElMessage.error('加载兑换码失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

async function loadWorkspaces() {
  try {
    const r = await listWorkspaceMasters({ limit: 500, offset: 0 })
    workspaces.value = r.items || []
  } catch {
    // 空间筛选只是便利项，失败不挡主列表
  }
}

function onWorkspaceFilter() {
  keyword.value = ''
  load()
}

async function removeCodes(codes) {
  if (!codes.length) return
  try {
    await ElMessageBox.confirm(
      `将作废 ${codes.length} 个兑换码，作废后持码人无法再用它下载凭证（账号下次导出会生成新码）。确定？`,
      '作废兑换码',
      { type: 'warning', confirmButtonText: '作废', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  deleting.value = true
  try {
    const r = await deleteRedeemCodes(codes)
    rows.value = r.codes || rows.value.filter((x) => !codes.includes(x.code))
    selected.value = []
    ElMessage.success(`已作废 ${r.deleted || codes.length} 个兑换码`)
  } catch (e) {
    ElMessage.error('作废失败: ' + e.message)
  } finally {
    deleting.value = false
  }
}

function removeSelected() {
  removeCodes(selected.value.map((r) => r.code))
}

function saveBlob(text, filename) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function exportAll() {
  if (!filtered.value.length) return ElMessage.warning('当前没有可导出的兑换码')
  const text = filtered.value.map((r) => r.code).join('\n') + '\n'
  saveBlob(text, `redeem-codes-${filtered.value.length}.txt`)
  ElMessage.success(`已导出 ${filtered.value.length} 个兑换码`)
}

function copyRedeemLink() {
  copyText(`${location.origin}${location.pathname}#/redeem`)
}

onActivated(() => {
  load()
  loadWorkspaces()
})
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="head">
          <span class="section-title">兑换管理</span>
          <div class="head-actions">
            <el-select
              v-model="workspaceFilter"
              size="small"
              filterable
              placeholder="全部空间"
              class="ws-filter"
              @change="onWorkspaceFilter"
            >
              <el-option :value="0" label="全部空间" />
              <el-option
                v-for="w in workspaces"
                :key="w.id"
                :value="w.id"
                :label="`${w.account || w.email || w.id}（${w.workspace_id || '-'}）`"
              />
            </el-select>
            <el-input
              v-model="keyword"
              size="small"
              clearable
              placeholder="搜索兑换码 / 账号 / 母号"
              class="kw-input"
            >
              <template #prefix><Icon icon="lucide:search" /></template>
            </el-input>
            <el-button size="small" @click="copyRedeemLink">
              <Icon icon="lucide:link" class="btn-icon" />兑换页链接
            </el-button>
            <el-button size="small" :disabled="!filtered.length" @click="exportAll">
              <Icon icon="lucide:download" class="btn-icon" />导出码
            </el-button>
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="!selected.length"
              :loading="deleting"
              @click="removeSelected"
            >
              作废选中（{{ selected.length }}）
            </el-button>
            <el-button size="small" @click="load">
              <el-icon><Refresh /></el-icon>刷新
            </el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else
        v-loading="loading"
        :data="filtered"
        size="small"
        stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="40" />
        <el-table-column label="兑换码" width="180">
          <template #default="{ row }">
            <span class="mono code-text">{{ row.code }}</span>
            <el-button link size="small" class="mini-copy" @click="copyText(row.code)">
              <Icon icon="lucide:copy" />
            </el-button>
          </template>
        </el-table-column>
        <el-table-column prop="email" label="绑定账号" min-width="200" show-overflow-tooltip />
        <el-table-column label="所属空间" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.master_account || `#${row.workspace_master_id}` }}</span>
            <span v-if="row.workspace_id" class="ws-id">（{{ row.workspace_id }}）</span>
          </template>
        </el-table-column>
        <el-table-column label="空间凭证" width="90">
          <template #default="{ row }">
            <el-tag :type="row.has_credential ? 'success' : 'danger'" size="small" effect="plain">
              {{ row.has_credential ? '有效' : '已失效' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="明文兑换" width="96">
          <template #default="{ row }">
            <el-tag
              :type="row.allow_secret ? 'warning' : 'info'"
              size="small"
              effect="plain"
              :title="row.allow_secret ? '可兑换账号密码+2FA 明文' : '仅可兑换加密 Sub2/CPA 凭证'"
            >
              {{ row.allow_secret ? '密码+2FA' : '已关闭' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="redeem_count" label="兑换次数" width="90" />
        <el-table-column label="最近兑换" width="160">
          <template #default="{ row }">{{ fmtTime(row.last_redeemed_at) }}</template>
        </el-table-column>
        <el-table-column label="生成时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="removeCodes([row.code])">
              作废
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无兑换码，去候选管理页勾选已有空间凭证的账号生成" :image-size="70" />
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ws-filter { width: 240px; }
.kw-input { width: 220px; }
.btn-icon { margin-right: 4px; }
.code-text { font-size: 13px; letter-spacing: 1px; }
.mini-copy { margin-left: 4px; vertical-align: middle; }
.ws-id { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
