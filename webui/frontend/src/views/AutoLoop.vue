<script setup>
import { computed, onActivated, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { autoStart, autoPause, autoResume, autoStop } from '@/api/register'
import { listAccountGroups } from '@/api/accounts'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'
import StatusDot from '@/components/StatusDot.vue'
import FieldHint from '@/components/FieldHint.vue'

const router = useRouter()
const { form } = storeToRefs(useFormStore())
const proxyStore = useProxyStore()
const { count: proxyCount } = storeToRefs(proxyStore)
const runtime = useRuntimeStore()
const { autoStatus } = storeToRefs(runtime)

const st = computed(() => autoStatus.value.state || 'stopped')
const canStart = computed(() => st.value === 'stopped')
const canPause = computed(() => st.value === 'running')
const canResume = computed(() => st.value === 'paused')
const canStop = computed(() => st.value !== 'stopped')

const stateLabel = computed(() => ({
  stopped: '未运行', running: '运行中', paused: '已暂停',
}[st.value] || st.value))
const stateType = computed(() => ({
  stopped: 'info', running: 'success', paused: 'warning',
}[st.value] || 'info'))

const workers = computed(() => Array.isArray(autoStatus.value.workers) ? autoStatus.value.workers : [])
const proxyUsage = computed(() => (
  Array.isArray(autoStatus.value.proxy_pool_usage)
    ? autoStatus.value.proxy_pool_usage
    : []
))
const taskTotalKnown = computed(() => autoStatus.value.task_total_known !== false && autoStatus.value.task_total != null)
const taskTotal = computed(() => taskTotalKnown.value ? Number(autoStatus.value.task_total || 0) : null)
const taskCompleted = computed(() => Number(autoStatus.value.task_completed || 0))
const taskProgress = computed(() => {
  if (!taskTotalKnown.value) return 0
  if (!taskTotal.value) return st.value === 'stopped' ? 100 : 0
  const value = Number(autoStatus.value.progress_percent)
  if (Number.isFinite(value)) return Math.max(0, Math.min(100, value))
  return Math.max(0, Math.min(100, taskCompleted.value * 100 / taskTotal.value))
})
const taskInProgress = computed(() => Number(autoStatus.value.task_in_progress || workers.value.length || 0))
const taskRemaining = computed(() => taskTotalKnown.value
  ? Math.max(0, Number(autoStatus.value.task_remaining ?? (taskTotal.value - taskCompleted.value)))
  : null)
const progressStatus = computed(() => (
  st.value === 'stopped' && taskTotalKnown.value && taskCompleted.value >= (taskTotal.value || 0)
    ? 'success'
    : undefined
))
const groups = ref([])

// 高级选项、代理租借表默认折叠：多数任务不需要展开，
// 展开状态只存在于本次会话，不做持久化。
const advancedOpen = ref([])
const usageOpen = ref([])

// 折叠时也要能看出里面有没有设成非默认值，否则用户会忘记自己改过什么
const advancedSummary = computed(() => {
  const parts = []
  if (form.value.autoAddPhoneMode !== 'api') parts.push('add-phone: Camoufox')
  if (!form.value.autoLoginOnly) {
    if (form.value.autoRegisterMode !== 'protocol') parts.push('注册: Camoufox')
    if (form.value.autoDebugMode) parts.push('调试截图')
    if (!form.value.autoWantPassword) parts.push('不强制密码')
  }
  return parts.length ? parts.join(' · ') : '默认配置'
})

const proxyUsageActive = computed(
  () => proxyUsage.value.filter((r) => Number(r.active_count) > 0).length,
)

async function loadGroups() {
  try {
    groups.value = (await listAccountGroups()).groups || []
    if (
      form.value.autoGroupName
      && form.value.autoGroupName !== '__all__'
      && !groups.value.some((g) => g.name === form.value.autoGroupName)
    ) form.value.autoGroupName = ''
  } catch (_) {}
}

onActivated(loadGroups)

async function start() {
  try {
      await autoStart({
        proxy: proxyText(form.value),
        proxy_pool: proxyStore.text,
        concurrency: parseInt(form.value.autoConcurrency, 10) || 1,
        otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
        want_access_token: true,
      want_session_token: true,
      want_refresh_token: form.value.autoWantOauthRt,
      add_phone_mode: form.value.autoAddPhoneMode,
      register_mode: form.value.autoRegisterMode,
      debug_mode: form.value.autoDebugMode,
      want_password: form.value.autoWantPassword,
      login_only: form.value.autoLoginOnly,
      ensure_credentials: form.value.autoEnsureCredentials,
      login_no_rt_only: form.value.autoLoginNoRtOnly,
      group_name: form.value.autoGroupName,
      cool_down_seconds: parseFloat(form.value.autoCoolDown) || 0,
      target_count: parseInt(form.value.autoTargetCount, 10) || 0,
        account_retry_count: parseInt(form.value.autoAccountRetryCount, 10) || 0,
        auto_export: form.value.autoExport,
        export_refresh_oauth: form.value.autoExportRefreshOauth,
      // 批量默认绑 2FA（后端默认是 false，这个字段以前压根没传，
      // 所以批量跑出来的号一个都没 2FA）。留开关是因为绑定不可逆。
      want_2fa: form.value.autoWant2fa,
    })
    ElMessage.success('自动跑号已启动')
  } catch (e) { ElMessage.error('启动失败: ' + e.message) }
}
async function call(fn, name) {
  try { await fn(); ElMessage.success(name + ' 成功') }
  catch (e) { ElMessage.error(name + ' 失败: ' + e.message) }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <span class="section-title" style="margin: 0">
          {{ form.autoLoginOnly ? '批量仅登录' : '全自动批量注册' }}
        </span>
      </template>

      <!-- ① 任务模式：决定跑什么，最先要定的事 -->
      <div class="cfg-group">
        <div class="cfg-group-title">任务模式</div>
        <div class="switch-grid">
          <div class="switch-item">
            <el-switch v-model="form.autoLoginOnly" />
            <span class="switch-label">仅登录</span>
            <FieldHint>
              开启后投送所选分组内的注册结果；“补齐2FA”开关打开时，只处理已有 OpenAI 密码、但缺少 TOTP
              的账号。通用 OTP 外部账号还必须带 OTP 中转链接，不会创建密码。
            </FieldHint>
          </div>

          <div v-if="form.autoLoginOnly" class="switch-item">
            <el-switch v-model="form.autoLoginNoRtOnly" />
            <span class="switch-label">仅执行无 RT 账号</span>
            <FieldHint>
              开启后仅从注册结果里挑选 refresh_token 为空的账号执行，仅影响“仅登录”任务。
            </FieldHint>
          </div>

          <div class="switch-item">
            <el-switch v-model="form.autoEnsureCredentials" />
            <span class="switch-label">
              {{ form.autoLoginOnly ? '补齐缺失的 2FA' : '已有账号时补齐 2FA' }}
            </span>
            <FieldHint>
              {{ form.autoLoginOnly
                ? '开启后只对已有 OpenAI 密码、但本地缺少 TOTP 的账号操作；绑定过程会再次使用邮箱 OTP，因此通用 OTP 导入必须带中转链接。系统不会创建密码，2FA secret 只下发一次，请及时备份。'
                : '开启后普通注册遇到服务端已存在的邮箱会切换为密码登录并补绑缺失的 2FA；新邮箱仍按正常注册执行。没有密码的已有账号会跳过并提示。' }}
            </FieldHint>
          </div>
        </div>
      </div>

      <!-- ② 运行参数：每次跑之前最常调的数值和开关 -->
      <div class="cfg-group">
        <div class="cfg-group-title">运行参数</div>
        <div class="num-grid">
          <div class="num-item">
            <label class="num-label">并发</label>
            <el-input-number v-model="form.autoConcurrency" :min="1" :max="20" controls-position="right" />
          </div>
          <div class="num-item">
            <label class="num-label">
              执行数量限制
              <FieldHint>0 = 不限量，跑到号池耗尽或手动停止为止。</FieldHint>
            </label>
            <el-input-number v-model="form.autoTargetCount" :min="0" :max="100000" controls-position="right" />
          </div>
          <div class="num-item">
            <label class="num-label">冷却(秒)</label>
            <el-input-number v-model="form.autoCoolDown" :min="0" :max="120" controls-position="right" />
          </div>
          <div class="num-item">
            <label class="num-label">OTP 等待(秒)</label>
            <el-input-number v-model="form.otpTimeout" :min="10" :max="600" controls-position="right" />
          </div>
          <div class="num-item">
            <label class="num-label">账号失败重试</label>
            <el-input-number v-model="form.autoAccountRetryCount" :min="0" :max="10" controls-position="right" />
          </div>
          <div class="num-item num-item-wide">
            <label class="num-label">执行分组</label>
            <el-select v-model="form.autoGroupName" class="group-select">
              <el-option label="未分组" value="" />
              <el-option label="全部分组" value="__all__" />
              <el-option
                v-for="g in groups.filter((g) => g.name)" :key="g.name"
                :label="`${g.name}（${form.autoLoginOnly ? `可登录/补齐 ${(g.active_registered_total ?? g.registered_total) + (g.mailbox_only_total || 0)}` : `可用 ${g.available}`}）`" :value="g.name"
              />
            </el-select>
          </div>
        </div>

        <div class="switch-grid" style="margin-top: 14px">
          <div v-if="!form.autoLoginOnly" class="switch-item">
            <el-switch v-model="form.autoWant2fa" />
            <span class="switch-label">自动绑定 2FA</span>
            <!-- 「不可逆」留在页面上，不藏进悬停：这是会造成不可恢复后果的警告 -->
            <span class="switch-warn">绑定不可逆</span>
            <FieldHint type="warning">
              绑定不可逆：之后该号所有登录都需 6 位动态码；secret 仅下发<b>一次</b>、服务端取不回，
              跑完请到「注册结果」页<b>导出备份</b>。绑定失败<b>不会废号</b>（仅日志告警、账号照常入库）；
              <b>无密码的号会自动跳过</b>，所以「每个号」实际是「每个有密码的号」。
            </FieldHint>
          </div>

          <div class="switch-item">
            <el-switch v-model="form.autoWantOauthRt" />
            <span class="switch-label">获取 refresh_token</span>
            <FieldHint>
              关闭后跳过最后的 Codex OAuth，可缩短每个任务耗时；access token 和 session token 不受影响。
            </FieldHint>
          </div>

          <div class="switch-item">
            <el-switch v-model="form.autoExport" />
            <span class="switch-label">自动推送号池</span>
            <FieldHint>
              任务成功后自动推送到已启用的 CPA / SUB2API。关闭只跳过本次任务，不会修改“自动导出”里的
              全局配置；注册和仅登录模式都生效。
            </FieldHint>
          </div>

          <div v-if="form.autoExport" class="switch-item">
            <el-switch v-model="form.autoExportRefreshOauth" />
            <span class="switch-label">推送前刷新 OAuth</span>
            <FieldHint>
              默认关闭，直接使用现有 access token；开启后用 RT 换取新的 Codex access token，
              可能受 OpenAI 出口地区限制影响。
            </FieldHint>
          </div>
        </div>
      </div>

      <!-- ③ 高级选项：默认折叠，多数任务不需要动 -->
      <el-collapse v-model="advancedOpen" class="cfg-collapse">
        <el-collapse-item name="advanced">
          <template #title>
            <span class="collapse-title">高级选项</span>
            <span class="collapse-sub">{{ advancedSummary }}</span>
          </template>

          <div class="num-grid">
            <div class="num-item num-item-wide">
              <label class="num-label">
                add-phone 模式
                <FieldHint>
                  仅在任务命中 add-phone 验证分支时使用；默认走接口模式。Linux 无桌面环境可改为 Camoufox。
                </FieldHint>
              </label>
              <el-select v-model="form.autoAddPhoneMode" class="group-select">
                <el-option label="API / SMS 接口" value="api" />
                <el-option label="Camoufox 浏览器模式" value="camoufox" />
              </el-select>
            </div>
            <div v-if="!form.autoLoginOnly" class="num-item num-item-wide">
              <label class="num-label">
                注册流程
                <FieldHint>Camoufox 浏览器注册；仅登录任务始终使用协议登录流程。</FieldHint>
              </label>
              <el-select v-model="form.autoRegisterMode" class="group-select">
                <el-option label="协议直连" value="protocol" />
                <el-option label="Camoufox 浏览器" value="camoufox" />
              </el-select>
            </div>
          </div>

          <div v-if="!form.autoLoginOnly" class="switch-grid" style="margin-top: 14px">
            <div class="switch-item">
              <el-switch v-model="form.autoDebugMode" />
              <span class="switch-label">失败时保存截图</span>
              <FieldHint>
                仅在 Camoufox 注册遇到页面超时或提交失败时截图；默认关闭。
                截图保存在服务端 logs/camoufox_screenshots 目录。
              </FieldHint>
            </div>
            <div class="switch-item">
              <el-switch v-model="form.autoWantPassword" />
              <span class="switch-label">强制创建账号密码</span>
              <FieldHint>
                新邮箱开启时要求创建长期密码；该选项只影响新账号注册。
                已有账号的“补齐2FA”不会创建或修改密码。
              </FieldHint>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>

      <div class="proxy-row">
        <el-tag :type="proxyCount ? 'success' : 'info'" effect="light">
          当前 {{ proxyCount }} 个代理
        </el-tag>
        <span class="hint">
          {{ proxyCount ? '每个任务优先取使用次数最少的代理' : '为空：所有任务用「单次注册」页填的单代理' }}
        </span>
        <el-button size="small" @click="router.push('/proxy')">管理代理池</el-button>
      </div>

      <el-space wrap style="margin-top: 8px">
        <el-button type="primary" :disabled="!canStart" @click="start">
          {{ form.autoLoginOnly ? '开始登录' : '开始' }}
        </el-button>
        <el-button :disabled="!canPause" @click="call(autoPause, '暂停')">暂停</el-button>
        <el-button :disabled="!canResume" @click="call(autoResume, '恢复')">恢复</el-button>
        <el-button type="danger" :disabled="!canStop" @click="call(autoStop, '停止')">停止</el-button>
      </el-space>

      <el-descriptions :column="6" border size="small" style="margin-top: 16px">
        <el-descriptions-item label="状态"><StatusDot :type="stateType" :text="stateLabel" /></el-descriptions-item>
        <el-descriptions-item label="任务对象">
          <b>{{ taskTotalKnown ? taskTotal : '不限量' }}</b>
        </el-descriptions-item>
        <el-descriptions-item label="已完成">
          <b>{{ taskCompleted }}</b>
          <span v-if="taskTotalKnown"> / {{ taskTotal }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="成功">
          <b style="color: var(--el-color-success)">{{ autoStatus.registered_ok || 0 }}</b>
          <span v-if="autoStatus.target_count"> / {{ autoStatus.target_count }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="最终失败">
          <b style="color: var(--el-color-danger)">{{ autoStatus.registered_fail || 0 }}</b>
        </el-descriptions-item>
        <el-descriptions-item label="重试账号">
          <b style="color: var(--el-color-warning)">{{ autoStatus.retry_count || 0 }}</b>
          <span v-if="autoStatus.retry_attempts">（{{ autoStatus.retry_attempts }} 次）</span>
        </el-descriptions-item>
        <el-descriptions-item label="并发">{{ autoStatus.concurrency || 1 }}</el-descriptions-item>
      </el-descriptions>

      <div style="margin-top: 16px">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px">
          <span>任务进度</span>
          <span class="hint">
            {{ taskTotalKnown
              ? `${taskCompleted} / ${taskTotal}（剩余 ${taskRemaining}）`
              : `${taskCompleted} 个已完成（总数不限）` }}
            <span v-if="taskInProgress"> · {{ taskInProgress }} 个执行中</span>
            <span v-if="autoStatus.task_retrying"> · {{ autoStatus.task_retrying }} 个待重试</span>
          </span>
        </div>
        <el-progress
          :percentage="taskProgress"
          :indeterminate="!taskTotalKnown && st !== 'stopped'"
          :duration="2"
          :status="progressStatus"
          :format="(p) => taskTotalKnown ? `${p}%` : (st === 'stopped' ? `${taskCompleted} 个` : '进行中')"
        />
        <p v-if="!taskTotalKnown" class="hint" style="margin: 6px 0 0">
          当前邮箱来源没有有限的任务对象；可用“执行数量限制”设定确定的进度总数。
        </p>
        <p class="hint" style="margin: 6px 0 0">
          统计按账号归并：重试中的中间失败不会计入“最终失败”，仅在重试耗尽后计入。
        </p>
      </div>

      <div v-if="workers.length" style="margin-top: 12px">
        <el-tag v-for="w in workers" :key="w.id" type="warning" effect="plain" style="margin: 0 6px 6px 0">
          worker-{{ w.id }} · {{ w.email }} · {{ w.proxy || '直连' }}
        </el-tag>
      </div>
      <p v-if="autoStatus.last_message" class="hint" style="margin-top: 8px">{{ autoStatus.last_message }}</p>
    </el-card>

    <el-card v-if="proxyUsage.length" shadow="never" style="margin-bottom: 16px">
      <el-collapse v-model="usageOpen" class="usage-collapse">
        <el-collapse-item name="usage">
          <template #title>
            <span class="collapse-title">当前代理租用计数</span>
            <span class="collapse-sub">
              {{ proxyUsage.length }} 个代理 · {{ proxyUsageActive }} 个使用中
            </span>
          </template>
          <p class="hint" style="margin: 0 0 10px">仅统计本次任务快照，任务结束后自动清空</p>
          <el-table :data="proxyUsage" border size="small" max-height="320" style="width: 100%">
            <el-table-column prop="index" label="#" width="60" />
            <el-table-column prop="proxy" label="代理" min-width="260" show-overflow-tooltip />
            <el-table-column prop="leased_count" label="租用次数" width="110" align="center" />
            <el-table-column prop="active_count" label="当前执行" width="100" align="center" />
            <el-table-column label="状态" width="100" align="center">
              <template #default="scope">
                <el-tag :type="scope.row.active_count ? 'warning' : 'info'" size="small">
                  {{ scope.row.active_count ? '使用中' : '空闲' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <el-card shadow="never">
      <LogPanel />
    </el-card>
  </div>
</template>

<style scoped>
/* 配置分区：用细分隔线分组，避免十几个选项糊成一片 */
.cfg-group {
  padding: 14px 0;
  border-top: 1px solid var(--el-border-color-lighter);
}

.cfg-group:first-of-type {
  padding-top: 0;
  border-top: none;
}

.cfg-group-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 12px;
}

/* 数值/下拉项：自适应列数，窄屏自动换行而不是挤成一行 */
.num-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 12px 16px;
}

.num-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.num-item-wide {
  grid-column: span 2;
}

@media (max-width: 700px) {
  .num-item-wide {
    grid-column: span 1;
  }
}

.num-label {
  font-size: 12px;
  color: var(--el-text-color-regular);
  display: flex;
  align-items: center;
  gap: 4px;
}

.num-item :deep(.el-input-number) {
  width: 100%;
}

.group-select {
  width: 100%;
}

/* 开关项：一行一个，说明收进 ⓘ，所以每项只占单行高度 */
.switch-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px 16px;
}

.switch-item {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.switch-label {
  font-size: 13px;
  color: var(--el-text-color-primary);
}

/* 不可逆操作的警示留在页面上，不靠悬停才能看到 */
.switch-warn {
  font-size: 11px;
  color: var(--el-color-warning);
  border: 1px solid var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
  border-radius: var(--app-radius-xs);
  padding: 0 5px;
  line-height: 17px;
  white-space: nowrap;
}

.cfg-collapse {
  border-top: 1px solid var(--el-border-color-lighter);
  border-bottom: none;
}

.cfg-collapse :deep(.el-collapse-item__header),
.usage-collapse :deep(.el-collapse-item__header) {
  border-bottom: none;
  font-weight: 600;
}

.cfg-collapse :deep(.el-collapse-item__wrap),
.usage-collapse :deep(.el-collapse-item__wrap) {
  border-bottom: none;
}

.usage-collapse {
  border-top: none;
  border-bottom: none;
}

.collapse-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.collapse-sub {
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
  margin-left: 10px;
}

.proxy-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 14px 0;
  border-top: 1px solid var(--el-border-color-lighter);
}
</style>
