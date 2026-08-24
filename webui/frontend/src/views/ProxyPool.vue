<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProxyStore, isValidProxy, proxyScheme } from '@/stores/proxy'
import { getProxyUsage, resetProxyUsage, testProxies } from '@/api/proxy'
import { copyText } from '@/api/request'

const proxyStore = useProxyStore()
const { list, count } = storeToRefs(proxyStore)

const draft = ref('')
const testResults = ref({}) // proxy -> { status:'testing'|'ok'|'fail', latency_ms, ip, error }
const testingAll = ref(false)
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
    index: i + 1, proxy: p, valid: isValidProxy(p), result: testResults.value[p] || null,
  })),
)
const invalidCount = computed(() => rows.value.filter((r) => !r.valid).length)
const usageCategoryMap = computed(() => Object.fromEntries(
  (usage.value.categories || []).map((item) => [item.task_type, Number(item.leased_count || 0)]),
))
const currentProxySet = computed(() => new Set(list.value))
const usageRows = computed(() => (usage.value.proxies || []).map((item) => ({
  ...item,
  in_current_pool: currentProxySet.value.has(item.proxy),
})))

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
  if (!targets.length) return
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    const { results } = await testProxies(targets)
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error('测试失败: ' + e.message)
  }
}
async function testOne(proxy) {
  await runTest([proxy])
}
async function testAll() {
  if (!count.value) return
  testingAll.value = true
  try { await runTest([...list.value]) }
  finally { testingAll.value = false }
}

function save() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  const r = proxyStore.setFromText(draft.value)
  draft.value = ''
  ElMessage.success(`已保存 ${r.kept} 个代理${r.duplicated ? `（去重 ${r.duplicated} 个）` : ''}`)
}
function append() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  const r = proxyStore.append(draft.value)
  draft.value = ''
  ElMessage.success(`已追加 ${r.added} 个新代理`)
}
async function clearAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm(`确定清空全部 ${count.value} 个代理？`, '确认', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
    proxyStore.clear()
    ElMessage.success('已清空')
  } catch (_) { /* cancel */ }
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
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">批量导入</span></template>
          <p class="hint">
            每行一个：<span class="mono">[协议://][user:pass@]host:port</span><br />
            不写协议默认按 <b>HTTP 代理</b>；SOCKS5 必须写 <span class="mono">socks5://</span>。<br />
            若某代理裸写能连、加了 <span class="mono">socks5://</span> 反而连不上，说明它其实是 HTTP 代理。
          </p>
          <el-input
            v-model="draft" type="textarea" :rows="12" class="mono"
            placeholder="socks5://127.0.0.1:7890&#10;socks5://user:pass@1.2.3.4:1080&#10;http://5.6.7.8:8080"
          />
          <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap">
            <el-button type="primary" @click="save">覆盖保存</el-button>
            <el-button @click="append">追加合并</el-button>
            <el-button @click="editInDraft">载入当前池</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">
                当前代理池（{{ count }} 个<template v-if="invalidCount">，<span style="color: var(--el-color-danger)">{{ invalidCount }} 个格式异常</span></template>）
              </span>
              <div style="display: flex; gap: 8px">
                <el-button size="small" type="primary" plain :loading="testingAll" :disabled="!count" @click="testAll">测试全部</el-button>
                <el-button size="small" :disabled="!count" @click="copyText(proxyStore.text)">复制全部</el-button>
                <el-button size="small" type="danger" plain :disabled="!count" @click="clearAll">清空</el-button>
              </div>
            </div>
          </template>

          <el-table :data="rows" size="small" stripe max-height="440">
            <el-table-column prop="index" label="#" width="48" />
            <el-table-column prop="proxy" label="代理地址" min-width="200" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.proxy }}</span></template>
            </el-table-column>
            <el-table-column label="格式" width="70">
              <template #default="{ row }">
                <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="light">
                  {{ row.valid ? '正常' : '异常' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="生效协议" width="110">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ proxyScheme(row.proxy) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="连通性" min-width="150">
              <template #default="{ row }">
                <template v-if="!row.result">
                  <span class="hint">未测</span>
                </template>
                <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">测试中…</el-tag>
                <template v-else-if="row.result.status === 'ok'">
                  <el-tag type="success" size="small">正常 {{ row.result.latency_ms }}ms</el-tag>
                  <span v-if="row.result.ip" class="hint mono" style="margin-left: 6px">{{ row.result.ip }}</span>
                </template>
                <el-tooltip v-else :content="row.result.error || '连接失败'" placement="top">
                  <el-tag type="danger" size="small">失败</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" text type="primary"
                  :loading="row.result && row.result.status === 'testing'"
                  @click="testOne(row.proxy)"
                >测试</el-button>
                <el-button size="small" text type="danger" @click="proxyStore.remove(row.proxy)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>暂无代理，请在左侧批量导入</template>
          </el-table>

          <el-alert
            type="info" :closable="false" show-icon style="margin-top: 12px"
            title="全自动批量跑号时，各 worker 会按顺序轮流取用这里的代理；代理池为空则所有 worker 用「单次注册」页填的单个代理。"
          />
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>
        <div class="usage-header">
          <div>
            <span class="section-title" style="margin: 0">全局代理租借计数</span>
            <span class="usage-period">统计起点：{{ formatTime(usage.started_at) }}</span>
          </div>
          <div class="usage-actions">
            <el-button size="small" :loading="usageLoading" @click="loadUsage()">刷新</el-button>
            <el-button size="small" type="danger" plain :disabled="!usage.leased_count" @click="clearUsage">重置统计</el-button>
          </div>
        </div>
      </template>

      <div class="usage-summary-grid">
        <div class="usage-stat usage-stat-total">
          <div class="usage-stat-label">全部任务租借</div>
          <div class="usage-stat-value">{{ usage.leased_count || 0 }}</div>
        </div>
        <div class="usage-stat">
          <div class="usage-stat-label">注册任务</div>
          <div class="usage-stat-value">{{ usageCount('register') }}</div>
        </div>
        <div class="usage-stat">
          <div class="usage-stat-label">登录任务</div>
          <div class="usage-stat-value">{{ usageCount('login') }}</div>
        </div>
        <div class="usage-stat">
          <div class="usage-stat-label">额度查询</div>
          <div class="usage-stat-value">{{ usageCount('quota') }}</div>
        </div>
        <div class="usage-stat">
          <div class="usage-stat-label">候选申请加入</div>
          <div class="usage-stat-value">{{ usageCount('candidate_join') }}</div>
        </div>
      </div>

      <div v-if="usage.details?.length" class="usage-details">
        <span class="hint">任务明细：</span>
        <el-tag v-for="item in usage.details" :key="`${item.task_type}:${item.task_detail}`" size="small" effect="plain">
          {{ item.label }} · {{ item.leased_count }}
        </el-tag>
      </div>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="usage-note"
        title="每次从代理池领取一个代理计 1 次；账号重试或风控换代理会重新计数。同一会话内的多个 HTTP 请求不会重复累计。统计持久化保存，不随单次任务结束或服务重启清零。"
      />
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        class="usage-note"
        title="候选额度手动查询、定时查询和垃圾箱复查均从全局池租取并归入额度查询；公开页 401 重登录归入登录任务。母号专属代理、手工指定的单代理、单账号自带代理及代理连通性测试不属于代理池租借。"
      />

      <el-table :data="usageRows" size="small" stripe max-height="460" style="margin-top: 14px">
        <el-table-column prop="proxy" label="代理地址" min-width="280" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.proxy }}</span></template>
        </el-table-column>
        <el-table-column label="当前池" width="86" align="center">
          <template #default="{ row }">
            <el-tag :type="row.in_current_pool ? 'success' : 'info'" size="small" effect="plain">
              {{ row.in_current_pool ? '在池' : '历史' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="leased_count" label="总租借" width="90" align="right" sortable />
        <el-table-column prop="register" label="注册" width="80" align="right" />
        <el-table-column prop="login" label="登录" width="80" align="right" />
        <el-table-column prop="quota" label="额度查询" width="90" align="right" />
        <el-table-column prop="candidate_join" label="候选申请" width="90" align="right" />
        <el-table-column label="最后租借" width="170">
          <template #default="{ row }">{{ formatTime(row.last_leased_at) }}</template>
        </el-table-column>
        <template #empty>尚无代理池租借记录</template>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.usage-header,
.usage-actions,
.usage-details {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.usage-header { justify-content: space-between; }
.usage-period { margin-left: 12px; color: var(--el-text-color-secondary); font-size: 12px; }
.usage-summary-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(120px, 1fr));
  gap: 12px;
}
.usage-stat {
  padding: 14px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
}
.usage-stat-total { border-color: var(--el-color-primary-light-7); background: var(--el-color-primary-light-9); }
.usage-stat-label { color: var(--el-text-color-secondary); font-size: 13px; }
.usage-stat-value { margin-top: 5px; font-size: 25px; font-weight: 650; line-height: 1.2; }
.usage-details { margin-top: 12px; }
.usage-note { margin-top: 12px; }
@media (max-width: 900px) {
  .usage-summary-grid { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  .usage-period { display: block; margin: 4px 0 0; }
}
</style>
