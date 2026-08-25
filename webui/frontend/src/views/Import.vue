<script setup>
// 导入邮箱号池。
//
// 来源下拉、格式提示、placeholder 全部来自后端 provider 声明，
// 加一种邮箱这里不用改。
//
// 校验策略是"全对才写"：只要有一行不合法，后端返 422 并列出每一行的
// 行号和原因，一个号都不会写进库 —— 免得导进去一半对不上账。
import { computed, onActivated, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { importAccounts, listAccountGroups, createAccountGroup } from '@/api/accounts'
import { getMailProviders } from '@/api/settings'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'

const statsStore = useStatsStore()
const runtime = useRuntimeStore()

const providers = ref([])
const kind = ref('')
const groupName = ref('')
const groups = ref([])
const text = ref('')
const loading = ref(false)
const result = ref('')
const errors = ref([])      // [{ line, error }]

const current = computed(
  () => providers.value.find((p) => p.kind === kind.value) || null,
)

const lineCount = computed(
  () => text.value.split('\n').filter((l) => l.trim() && !l.trim().startsWith('#')).length,
)

async function loadProviders() {
  try {
    // pooled_only：只列能导号的，CF 这种自己造地址的没号可导
    const r = await getMailProviders(true)
    providers.value = r.providers || []
    // 默认选当前正在用的；它要是不支持导入就退回第一个
    const cur = r.current
    kind.value = providers.value.some((p) => p.kind === cur)
      ? cur
      : (providers.value[0]?.kind || '')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function loadGroups() {
  try {
    groups.value = (await listAccountGroups()).groups || []
  } catch (e) {
    ElMessage.error('加载分组失败: ' + e.message)
  }
}

async function addGroup() {
  try {
    const { value } = await ElMessageBox.prompt(
      '新分组创建后会自动选中，本次邮箱将直接导入该分组。',
      '新增分组',
      {
        inputPlaceholder: '例如：8月采购',
        confirmButtonText: '新增',
        cancelButtonText: '取消',
        inputValidator: (v) => String(v || '').trim().length > 0 || '分组名称不能为空',
      },
    )
    const name = value.trim()
    const r = await createAccountGroup(name)
    groups.value = r.groups || []
    groupName.value = name
    ElMessage.success('分组已新增并选中')
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || String(e))
  }
}

// 页面在 keep-alive 里，首次挂载和每次切回来都要拉一次 ——
// 否则主人在「邮箱配置」加了新来源，切过来下拉框还是旧的
onMounted(() => { loadProviders(); loadGroups() })
onActivated(() => { loadProviders(); loadGroups() })

async function doImport() {
  if (!text.value.trim()) {
    ElMessage.warning('请输入要导入的号')
    return
  }
  if (!kind.value) {
    ElMessage.warning('请先选择邮箱来源')
    return
  }
  loading.value = true
  result.value = ''
  errors.value = []
  try {
    const r = await importAccounts(text.value.trim(), kind.value, groupName.value)
    const groupLabel = groupName.value || '未分组'
    result.value = `导入到“${groupLabel}”：解析 ${r.parsed} 行，新增 ${r.inserted}，更新 ${r.updated}，跳过 ${r.skipped}`
    ElMessage.success('导入完成')
    text.value = ''
    statsStore.refresh()
    runtime.bumpData()
    loadGroups()
  } catch (e) {
    // 422 带逐行详情；其他错误只有一句话
    if (e.status === 422 && e.data?.errors?.length) {
      errors.value = e.data.errors
      result.value = `有 ${e.data.errors.length} 行不合法，已全部拒绝，一个都没导入`
      ElMessage.error('导入被拒绝，请修正后重试')
    } else {
      result.value = '导入失败: ' + e.message
      ElMessage.error(e.message)
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <span class="section-title" style="margin: 0">导入邮箱</span>
      </template>

      <el-form label-position="top" style="margin-bottom: 4px">
        <el-form-item label="邮箱来源">
          <el-select v-model="kind" style="width: 260px" placeholder="请选择">
            <el-option
              v-for="p in providers"
              :key="p.kind"
              :label="p.display_name"
              :value="p.kind"
            />
          </el-select>
          <span class="hint" style="margin-left: 12px">
            必须选对来源 —— 各来源的导入段数和取码方式可能不同
          </span>
        </el-form-item>
        <el-form-item label="导入分组">
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <el-select v-model="groupName" style="width: 260px" placeholder="请选择分组">
              <el-option label="未分组" value="" />
              <el-option
                v-for="g in groups"
                :key="g.name"
                :label="`${g.name} (${g.total})`"
                :value="g.name"
              />
            </el-select>
            <el-button @click="addGroup">新增分组</el-button>
          </div>
          <div class="hint" style="margin-top: 6px">
            已存在的邮箱再次导入时，也会移动到本次选择的分组。
            “补齐2FA”要求已有 OpenAI 密码；通用 OTP 外部账号请导入“邮箱----OpenAI密码----OTP 中转链接”，旧的两段格式仍可导入但会被补齐任务跳过。
          </div>
        </el-form-item>
      </el-form>

      <p class="hint" v-if="current">
        每行一个（用 <code>----</code> 分隔，支持 {{ current.import_segments_label || current.line_segments }} 段）：<br />
        <code>{{ current.import_hint || '' }}</code>
      </p>

      <el-input
        v-model="text"
        type="textarea"
        :rows="12"
        class="mono"
        :placeholder="current?.import_placeholder || ''"
      />

      <div style="margin-top: 12px; display: flex; align-items: center; gap: 12px">
        <el-button type="primary" :loading="loading" @click="doImport">导入</el-button>
        <span class="hint" v-if="lineCount">待导入 {{ lineCount }} 行</span>
        <span class="hint">{{ result }}</span>
      </div>

      <!-- 逐行错误：告诉主人第几行错在哪，而不是笼统一句"导入失败" -->
      <el-alert
        v-if="errors.length"
        type="error"
        :closable="true"
        show-icon
        style="margin-top: 12px"
        title="以下行不合法，整批已拒绝（号池未被改动）"
        @close="errors = []"
      >
        <ul class="err-list">
          <li v-for="e in errors" :key="e.line">
            <b>第 {{ e.line }} 行</b>：{{ e.error }}
          </li>
        </ul>
      </el-alert>
    </el-card>
  </div>
</template>

<style scoped>
.err-list {
  margin: 6px 0 0;
  padding-left: 18px;
  max-height: 220px;
  overflow-y: auto;
  line-height: 1.7;
}
</style>
