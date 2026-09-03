<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Icon } from '@iconify/vue'
import { useProxyStore, isValidProxy, proxyScheme } from '@/stores/proxy'
import { getProxyUsage, resetProxyUsage, testProxies } from '@/api/proxy'
import { copyText } from '@/api/request'
import { SAMPLE_RATIO, sampleProxies, summarizeLatency } from '@/utils/proxyCheck'

const proxyStore = useProxyStore()
const { list, count } = storeToRefs(proxyStore)

const draft = ref('')
const testResults = ref({}) // proxy -> { status:'testing'|'ok'|'fail', latency_ms, ip, error }
const testingAll = ref(false)
const checking = ref(false)
const assessment = ref(null) // 快速评估汇总，见 utils/proxyCheck.js 的 summarizeLatency
const usageLoading = ref(false)
const usage = ref({
  persistent: true,
  started_at: 0,
  updated_at: 0,
  leased_count: 0,
  categories: [],
  details: [],
  proxies: [],
})
let usageTimer = null

const rows = computed(() =>
  list.value.map((p, i) => ({
    index: i + 1,
    proxy: p,
    valid: isValidProxy(p),
    result: testResults.value[p] || null,
  })),
)
const invalidCount = computed(() => rows.value.filter((r) => !r.valid).length)
const usageCategoryMap = computed(() =>
  Object.fromEntries(
    (usage.value.categories || []).map((item) => [item.task_type, Number(item.leased_count || 0)]),
  ),
)
const currentProxySet = computed(() => new Set(list.value))
const samplePercentLabel = computed(() => `${Math.round(SAMPLE_RATIO * 100)}%`)
// 可用率配色：低于 60% 视为整池有问题，60~90% 提醒关注。
const assessTone = computed(() => {
  const a = assessment.value
  if (!a) return 'text-muted'
  if (a.availability >= 90) return 'text-success'
  if (a.availability >= 60) return 'text-warning'
  return 'text-danger'
})
const usageRows = computed(() =>
  (usage.value.proxies || []).map((item) => ({
    ...item,
    in_current_pool: currentProxySet.value.has(item.proxy),
  })),
)

function usageCount(taskType) {
  return usageCategoryMap.value[taskType] || 0
}

function formatTime(value) {
  if (!value) return '-'
  return new Date(Number(value) * 1000).toLocaleString('zh-CN', { hour12: false })
}

async function loadUsage(silent = false) {
  if (usageLoading.value) return
  usageLoading.value = true
  try {
    const result = await getProxyUsage()
    usage.value = result.usage || usage.value
  } catch (e) {
    if (!silent) ElMessage.error('代理租借统计加载失败: ' + e.message)
  } finally {
    usageLoading.value = false
  }
}

async function clearUsage() {
  try {
    await ElMessageBox.confirm(
      '只会清空全局代理租借次数，不会删除代理池中的任何代理。确定继续？',
      '重置代理租借统计',
      { type: 'warning', confirmButtonText: '重置统计', cancelButtonText: '取消' },
    )
  } catch (_) {
    return
  }
  try {
    const result = await resetProxyUsage()
    usage.value = result.usage || usage.value
    ElMessage.success('全局代理租借统计已重置')
  } catch (e) {
    ElMessage.error('重置失败: ' + e.message)
  }
}

async function runTest(targets) {
  if (!targets.length) return null
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    const { results } = await testProxies(targets)
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
    return results
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error('测试失败: ' + e.message)
    return null
  }
}
async function testOne(proxy) {
  await runTest([proxy])
}
async function testAll() {
  if (!count.value) return
  testingAll.value = true
  try {
    await runTest([...list.value])
  } finally {
    testingAll.value = false
  }
}

/** 快速评估：随机抽 10% 的代理测延迟，用样本估算整池的可用率和延迟水平。 */
async function quickCheck() {
  if (!count.value) return
  const sample = sampleProxies(list.value)
  checking.value = true
  try {
    const results = await runTest(sample)
    if (!results) return
    const rowsForSummary = sample
      .filter((p) => results[p])
      .map((p) => ({ proxy: p, ...results[p] }))
    assessment.value = summarizeLatency(rowsForSummary, count.value)
    const s = assessment.value
    ElMessage.success(
      `抽样 ${s.sampled}/${s.total} 个代理：可用率 ${s.availability}%，中位延迟 ${s.medianMs}ms`,
    )
  } finally {
    checking.value = false
  }
}

function save() {
  if (!draft.value.trim()) {
    ElMessage.warning('请先粘贴代理')
    return
  }
  const r = proxyStore.setFromText(draft.value)
  draft.value = ''
  ElMessage.success(`已保存 ${r.kept} 个代理${r.duplicated ? `（去重 ${r.duplicated} 个）` : ''}`)
}
function append() {
  if (!draft.value.trim()) {
    ElMessage.warning('请先粘贴代理')
    return
  }
  const r = proxyStore.append(draft.value)
  draft.value = ''
  ElMessage.success(`已追加 ${r.added} 个新代理`)
}
async function clearAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm(`确定清空全部 ${count.value} 个代理？`, '确认', {
      type: 'warning',
      confirmButtonText: '确定',
      cancelButtonText: '取消',
    })
    proxyStore.clear()
    ElMessage.success('已清空')
  } catch (_) {
    /* cancel */
  }
}
function editInDraft() {
  draft.value = proxyStore.text
  ElMessage.info('已把当前代理池载入编辑框，改完点「覆盖保存」')
}

onMounted(() => {
  loadUsage()
  usageTimer = window.setInterval(() => loadUsage(true), 3000)
})

onBeforeUnmount(() => {
  if (usageTimer) window.clearInterval(usageTimer)
  usageTimer = null
})
</script>

<template>
  <div class="page-container">
    <!-- Hero KPI Metrics Grid -->
    <div class="hero-kpi-grid">
      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">当前代理总数</span>
          <Icon icon="lucide:network" class="kpi-type-icon text-primary" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-primary">{{ count }}</div>
          <div class="kpi-hint">内存/持久化全局代理池</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item">
            <i class="dot" :class="invalidCount ? 'dot-danger' : 'dot-success'" />
            {{ invalidCount ? `${invalidCount} 个格式异常` : '全部格式正常' }}
          </span>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">累计租借总次数</span>
          <Icon icon="lucide:refresh-ccw" class="kpi-type-icon text-success" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-success">{{ usage.leased_count || 0 }}</div>
          <div class="kpi-hint">跨会话持久化累计计数</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item">注册: {{ usageCount('register') }}</span>
          <span class="kpi-sub-item">登录: {{ usageCount('login') }}</span>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">额度查询 / 候选申请</span>
          <Icon icon="lucide:activity" class="kpi-type-icon text-warning" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-warning">{{ usageCount('quota') + usageCount('candidate_join') }}</div>
          <div class="kpi-hint">额度巡检及母号候选加入</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item">额度: {{ usageCount('quota') }}</span>
          <span class="kpi-sub-item">候选加入: {{ usageCount('candidate_join') }}</span>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">代理池快速评估</span>
          <Icon icon="lucide:gauge" class="kpi-type-icon" :class="assessTone" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val" :class="assessTone">
            {{ assessment ? assessment.availability + '%' : '—' }}
          </div>
          <div class="kpi-hint">
            <template v-if="assessment">
              样本 {{ assessment.sampled }}/{{ assessment.total }} · 可用 {{ assessment.okCount }}
            </template>
            <template v-else>随机抽取 {{ samplePercentLabel }} 节点测延迟</template>
          </div>
        </div>
        <div class="kpi-footer">
          <template v-if="assessment">
            <span class="kpi-sub-item">中位 {{ assessment.medianMs }}ms</span>
            <span class="kpi-sub-item">P95 {{ assessment.p95Ms }}ms</span>
            <span class="kpi-sub-item" :class="assessment.failCount ? 'text-danger' : ''">
              失败 {{ assessment.failCount }}
            </span>
          </template>
          <el-button v-else link size="small" type="primary" :loading="checking" :disabled="!count" @click="quickCheck">
            立即评估
          </el-button>
        </div>
      </div>

      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">统计运行时间</span>
          <Icon icon="lucide:clock" class="kpi-type-icon" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val" style="font-size: 16px; margin-top: 6px">
            {{ formatTime(usage.started_at) }}
          </div>
          <div class="kpi-hint">计数周期起始时间</div>
        </div>
        <div class="kpi-footer">
          <el-button link size="small" type="primary" :loading="usageLoading" @click="loadUsage()">刷新</el-button>
          <el-button link size="small" type="danger" :disabled="!usage.leased_count" @click="clearUsage">重置统计</el-button>
        </div>
      </div>
    </div>

    <!-- Main Layout: Import Card & Pool Table Card -->
    <div class="proxy-columns">
      <!-- Import & Config Card -->
      <el-card shadow="never" class="dash-card import-col">
        <template #header>
          <div class="card-header-flex">
            <div class="header-left">
              <Icon icon="lucide:file-input" class="header-icon" />
              <span class="header-title">批量导入代理</span>
            </div>
          </div>
        </template>

        <div class="import-body">
          <p class="hint-box">
            每行一个：<span class="mono-tag">[协议://][user:pass@]host:port</span><br />
            默认协议为 <b>HTTP</b>；SOCKS5 必须显式声明 <span class="mono-tag">socks5://</span>
          </p>

          <el-input
            v-model="draft"
            type="textarea"
            :rows="12"
            class="mono-textarea"
            placeholder="socks5://127.0.0.1:7890&#10;socks5://user:pass@1.2.3.4:1080&#10;http://5.6.7.8:8080"
          />

          <div class="import-actions">
            <el-button type="primary" class="action-btn" @click="save">
              <Icon icon="lucide:save" class="btn-icon" /> 覆盖保存
            </el-button>
            <el-button class="action-btn" @click="append">
              <Icon icon="lucide:plus" class="btn-icon" /> 追加合并
            </el-button>
            <el-button class="action-btn" @click="editInDraft">
              <Icon icon="lucide:edit-3" class="btn-icon" /> 载入当前池
            </el-button>
          </div>
        </div>
      </el-card>

      <!-- Proxy Pool Active Table Card -->
      <el-card shadow="never" class="dash-card table-col">
        <template #header>
          <div class="card-header-flex">
            <div class="header-left">
              <Icon icon="lucide:list" class="header-icon" />
              <span class="header-title">活跃代理池明细 ({{ count }} 条)</span>
            </div>
            <div class="header-actions">
              <el-button size="small" type="primary" :loading="checking" :disabled="!count" @click="quickCheck">
                <Icon icon="lucide:gauge" style="margin-right: 4px" /> 快速评估 {{ samplePercentLabel }}
              </el-button>
              <el-button size="small" type="primary" plain :loading="testingAll" :disabled="!count" @click="testAll">
                <Icon icon="lucide:zap" style="margin-right: 4px" /> 测试全部
              </el-button>
              <el-button size="small" :disabled="!count" @click="copyText(proxyStore.text)">
                <Icon icon="lucide:copy" style="margin-right: 4px" /> 复制全部
              </el-button>
              <el-button size="small" type="danger" plain :disabled="!count" @click="clearAll">
                <Icon icon="lucide:trash-2" style="margin-right: 4px" /> 清空
              </el-button>
            </div>
          </div>
        </template>

        <el-table :data="rows" size="small" stripe max-height="460" class="modern-table">
          <el-table-column prop="index" label="#" width="50" align="center" />
          <el-table-column prop="proxy" label="代理地址" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="proxy-text mono">{{ row.proxy }}</span>
            </template>
          </el-table-column>
          <el-table-column label="协议" width="90">
            <template #default="{ row }">
              <span class="scheme-badge">{{ proxyScheme(row.proxy) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="格式" width="80" align="center">
            <template #default="{ row }">
              <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="plain">
                {{ row.valid ? '有效' : '异常' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="连通性测试" min-width="160">
            <template #default="{ row }">
              <span v-if="!row.result" class="text-muted" style="font-size: 12px">未测试</span>
              <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">
                <el-icon class="is-loading"><Loading /></el-icon> 测试中…
              </el-tag>
              <template v-else-if="row.result.status === 'ok'">
                <el-tag type="success" size="small" effect="light">
                  正常 {{ row.result.latency_ms }}ms
                </el-tag>
                <span v-if="row.result.ip" class="ip-hint mono">{{ row.result.ip }}</span>
              </template>
              <el-tooltip v-else :content="row.result.error || '连接超时或握手失败'" placement="top">
                <el-tag type="danger" size="small" effect="light">连接失败</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="120" fixed="right" align="center">
            <template #default="{ row }">
              <el-button
                size="small"
                text
                type="primary"
                :loading="row.result && row.result.status === 'testing'"
                @click="testOne(row.proxy)"
              >
                测试
              </el-button>
              <el-button size="small" text type="danger" @click="proxyStore.remove(row.proxy)">
                删除
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无代理，请在左侧粘贴并导入" :image-size="60" />
          </template>
        </el-table>
      </el-card>
    </div>

    <!-- Proxy Lease Analytics Table Card -->
    <el-card shadow="never" class="dash-card">
      <template #header>
        <div class="card-header-flex">
          <div class="header-left">
            <Icon icon="lucide:bar-chart-2" class="header-icon" />
            <span class="header-title">代理租借使用统计明细</span>
          </div>
          <div class="header-actions">
            <el-button size="small" :loading="usageLoading" @click="loadUsage()">刷新明细</el-button>
          </div>
        </div>
      </template>

      <el-table :data="usageRows" size="small" stripe max-height="400" class="modern-table">
        <el-table-column prop="proxy" label="代理地址" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="proxy-text mono">{{ row.proxy }}</span>
          </template>
        </el-table-column>
        <el-table-column label="当前状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="row.in_current_pool ? 'success' : 'info'" size="small" effect="plain">
              {{ row.in_current_pool ? '池内使用中' : '历史代理' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="leased_count" label="总租借" width="100" align="right" sortable>
          <template #default="{ row }">
            <b class="text-primary">{{ row.leased_count }}</b>
          </template>
        </el-table-column>
        <el-table-column prop="register" label="注册任务" width="90" align="right" />
        <el-table-column prop="login" label="登录任务" width="90" align="right" />
        <el-table-column prop="quota" label="额度查询" width="100" align="right" />
        <el-table-column prop="candidate_join" label="候选申请" width="100" align="right" />
        <el-table-column label="最近租借时间" width="180" align="center">
          <template #default="{ row }">
            <span class="text-muted" style="font-size: 12px">{{ formatTime(row.last_leased_at) }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="尚无代理池租借记录" :image-size="60" />
        </template>
      </el-table>
    </el-card>
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

/* Layout Columns */
.proxy-columns {
  display: grid;
  grid-template-columns: minmax(320px, 420px) 1fr;
  gap: 16px;
}

@media (max-width: 960px) {
  .proxy-columns {
    grid-template-columns: 1fr;
  }
}

.dash-card {
  border-radius: var(--app-radius-md);
}

.card-header-flex {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-icon {
  font-size: 16px;
  color: var(--el-color-primary, #409eff);
}

.header-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary, #303133);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* Import Body */
.import-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.hint-box {
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary, #909399);
  background: var(--el-fill-color-light, #f5f7fa);
  padding: 8px 12px;
  border-radius: var(--app-radius-sm);
  margin: 0;
}

.mono-tag {
  font-family: var(--el-font-family-monospace, monospace);
  color: var(--el-color-primary, #409eff);
}

.mono-textarea :deep(textarea) {
  font-family: var(--el-font-family-monospace, monospace);
  font-size: 12px;
}

.import-actions {
  display: flex;
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

/* Table elements */
.proxy-text {
  font-size: 12px;
  color: var(--el-text-color-primary, #303133);
}

.mono {
  font-family: var(--el-font-family-monospace, monospace);
}

.scheme-badge {
  font-family: var(--el-font-family-monospace, monospace);
  font-size: 11px;
  background: var(--el-fill-color-light, #f5f7fa);
  padding: 2px 6px;
  border-radius: var(--app-radius-xs);
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
}

.ip-hint {
  font-size: 11px;
  color: var(--el-text-color-placeholder, #c0c4cc);
  margin-left: 6px;
}
</style>
