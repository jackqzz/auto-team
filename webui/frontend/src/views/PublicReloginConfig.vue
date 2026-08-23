<script setup>
import { onActivated, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getPublicReloginConfig,
  savePublicReloginConfig,
  createPublicReloginAccessKey,
  revokePublicReloginAccessKey,
} from '@/api/settings'
import { copyText, fmtTime } from '@/api/request'
import FooterToolbar from '@/components/FooterToolbar.vue'

const router = useRouter()
const saving = ref(false)
const creatingKey = ref(false)
const accessKeys = ref([])
const newAccessKey = ref('')

const form = reactive({
  enabled: false,
  proxyPool: '',
  useSystemProxyPool: true,
  concurrency: 3,
  retryCount: 2,
  quotaTimeout: 30,
  loginTimeout: 180,
  adminPassword: '',
  clearAdminPassword: false,
  authEnabled: false,
})

const keyForm = reactive({
  name: '',
  expiresInDays: 3,
  permanent: false,
})

function formatKeyTime(value) {
  return value ? fmtTime(value) : '永久'
}

async function load() {
  try {
    const { config } = await getPublicReloginConfig()
    form.enabled = config.enabled === '1'
    form.proxyPool = config.proxy_pool || ''
    form.useSystemProxyPool = config.use_system_proxy_pool !== false && config.use_system_proxy_pool !== '0'
    form.concurrency = Number(config.concurrency || 3)
    form.retryCount = Number(config.retry_count || 2)
    form.quotaTimeout = Number(config.quota_timeout || 30)
    form.loginTimeout = Number(config.login_timeout || 180)
    form.adminPassword = ''
    form.clearAdminPassword = false
    form.authEnabled = !!config.auth_enabled
    accessKeys.value = config.access_keys || []
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function save() {
  saving.value = true
  try {
    await savePublicReloginConfig({
      public_relogin_enabled: form.enabled,
      proxy_pool: form.proxyPool.trim(),
      use_system_proxy_pool: form.useSystemProxyPool,
      concurrency: form.concurrency,
      retry_count: form.retryCount,
      quota_timeout: form.quotaTimeout,
      login_timeout: form.loginTimeout,
      admin_password: form.adminPassword.trim(),
      clear_admin_password: form.clearAdminPassword,
    })
    ElMessage.success('保存成功')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function createKey() {
  creatingKey.value = true
  try {
    const res = await createPublicReloginAccessKey({
      name: keyForm.name.trim(),
      expires_in_days: keyForm.permanent ? 0 : keyForm.expiresInDays,
    })
    accessKeys.value = res.access_keys || []
    newAccessKey.value = res.access_key?.key || ''
    keyForm.name = ''
    keyForm.expiresInDays = 3
    keyForm.permanent = false
    if (newAccessKey.value) {
      await copyText(newAccessKey.value)
      ElMessage.success('访问密钥已创建并复制，请立即保存；后端不会再次显示完整密钥')
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    creatingKey.value = false
  }
}

async function revokeKey(row) {
  try {
    await ElMessageBox.confirm(`确定撤销访问密钥 ${row.name || row.prefix}？撤销后无法恢复。`, '撤销访问密钥', {
      type: 'warning',
    })
    const res = await revokePublicReloginAccessKey(row.id)
    accessKeys.value = res.access_keys || []
    ElMessage.success('已撤销')
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.message || e)
  }
}

onActivated(() => load())
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="max-width: 860px">
      <template #header>
        <span class="section-title" style="margin: 0">公开 401 重登录配置</span>
        <el-tag type="info" size="small" effect="plain" style="margin-left: 8px">
          /#/public-relogin
        </el-tag>
      </template>

      <p class="hint">
        公开页通过访问密钥鉴权。导入账号只保存在用户浏览器内；检查额度和重登录时才把本批账号临时发到后端。
      </p>

      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="form.enabled">启用公开 401 重登录页面</el-checkbox>
        </el-form-item>

        <el-form-item>
          <el-checkbox v-model="form.useSystemProxyPool">复用系统代理池</el-checkbox>
          <div class="hint">开启后，公开重登会使用“代理池”页面保存的代理；单账号代理仍优先。</div>
        </el-form-item>

        <el-form-item label="后端备用代理池（系统代理池为空或关闭复用时使用）">
          <el-input
            v-model="form.proxyPool"
            type="textarea"
            :rows="6"
            placeholder="socks5://user:pass@host:port"
          />
        </el-form-item>

        <div class="grid">
          <el-form-item label="最大并发">
            <el-input-number v-model="form.concurrency" :min="1" :max="20" />
          </el-form-item>
          <el-form-item label="401 重登录失败重试">
            <el-input-number v-model="form.retryCount" :min="0" :max="5" />
          </el-form-item>
          <el-form-item label="额度查询超时(秒)">
            <el-input-number v-model="form.quotaTimeout" :min="5" :max="120" />
          </el-form-item>
          <el-form-item label="登录超时(秒)">
            <el-input-number v-model="form.loginTimeout" :min="30" :max="900" />
          </el-form-item>
        </div>

        <el-divider content-position="left">公开页访问密钥</el-divider>
        <div class="hint" style="margin-bottom: 12px">
          创建后只显示一次完整密钥；公开页用户输入一次后会缓存在浏览器本地。
        </div>
        <div class="key-create">
          <el-input v-model="keyForm.name" placeholder="密钥备注，可空" clearable style="max-width: 220px" />
          <span class="hint">有效天数</span>
          <el-input-number
            v-model="keyForm.expiresInDays"
            :min="1"
            :max="3650"
            :disabled="keyForm.permanent"
          />
          <el-checkbox v-model="keyForm.permanent">永久有效</el-checkbox>
          <el-button type="primary" :loading="creatingKey" @click="createKey">
            创建访问密钥
          </el-button>
        </div>
        <el-alert
          v-if="newAccessKey"
          type="success"
          show-icon
          :closable="false"
          style="margin: 12px 0"
        >
          <template #title>
            新密钥：<span class="mono">{{ newAccessKey }}</span>
            <el-button link type="primary" @click="copyText(newAccessKey)">复制</el-button>
          </template>
        </el-alert>
        <el-table :data="accessKeys" size="small" border style="margin-bottom: 12px">
          <el-table-column label="备注" prop="name" min-width="140" />
          <el-table-column label="前缀" prop="prefix" width="130" />
          <el-table-column label="创建时间" width="180">
            <template #default="{ row }">{{ formatKeyTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="过期时间" width="180">
            <template #default="{ row }">{{ formatKeyTime(row.expires_at) }}</template>
          </el-table-column>
          <el-table-column label="上次使用" width="180">
            <template #default="{ row }">{{ row.last_used_at ? fmtTime(row.last_used_at) : '-' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.active" type="success">可用</el-tag>
              <el-tag v-else-if="row.expired" type="warning">已过期</el-tag>
              <el-tag v-else type="danger">已撤销</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-button size="small" type="danger" link :disabled="row.revoked" @click="revokeKey(row)">
                撤销
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-divider content-position="left">系统管理平台鉴权</el-divider>
        <el-alert
          :type="form.authEnabled ? 'success' : 'warning'"
          show-icon
          :closable="false"
          :title="form.authEnabled ? '管理端鉴权已启用' : '尚未设置管理员密码，管理端 API 暂不鉴权'"
          style="margin-bottom: 12px"
        />

        <el-form-item label="设置/更新管理员密码（留空不修改）">
          <el-input v-model="form.adminPassword" type="password" show-password placeholder="新管理员密码" />
        </el-form-item>

        <el-form-item>
          <el-checkbox v-model="form.clearAdminPassword">清空管理员密码并关闭管理端鉴权</el-checkbox>
        </el-form-item>
      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>
        公开页：{{ form.enabled ? '已启用' : '未启用' }} · 管理鉴权：{{ form.authEnabled ? '已启用' : '未启用' }}
      </template>
      <el-button @click="router.push('/public-relogin')">打开公开页</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </FooterToolbar>
  </div>
</template>

<style scoped>
.grid {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.key-create {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  word-break: break-all;
}
</style>
