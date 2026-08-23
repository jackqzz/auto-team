<script setup>
import { onActivated, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getPublicReloginConfig, savePublicReloginConfig } from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const router = useRouter()
const saving = ref(false)

const form = reactive({
  enabled: false,
  workspaceWhitelist: '',
  proxyPool: '',
  concurrency: 3,
  retryCount: 2,
  quotaTimeout: 30,
  loginTimeout: 180,
  adminPassword: '',
  clearAdminPassword: false,
  authEnabled: false,
})

async function load() {
  try {
    const { config } = await getPublicReloginConfig()
    form.enabled = config.enabled === '1'
    form.workspaceWhitelist = config.workspace_whitelist || ''
    form.proxyPool = config.proxy_pool || ''
    form.concurrency = Number(config.concurrency || 3)
    form.retryCount = Number(config.retry_count || 2)
    form.quotaTimeout = Number(config.quota_timeout || 30)
    form.loginTimeout = Number(config.login_timeout || 180)
    form.adminPassword = ''
    form.clearAdminPassword = false
    form.authEnabled = !!config.auth_enabled
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function save() {
  saving.value = true
  try {
    await savePublicReloginConfig({
      public_relogin_enabled: form.enabled,
      workspace_whitelist: form.workspaceWhitelist.trim(),
      proxy_pool: form.proxyPool.trim(),
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
        公开页无鉴权，导入账号只保存在用户浏览器内；检查额度和重登录时才把本批账号临时发到后端。后端只允许白名单 workspace_id。
      </p>

      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="form.enabled">启用公开 401 重登录页面</el-checkbox>
        </el-form-item>

        <el-form-item label="允许的 Workspace ID 白名单（一行一个，也支持逗号分隔）">
          <el-input
            v-model="form.workspaceWhitelist"
            type="textarea"
            :rows="5"
            placeholder="85f86570-bf64-4c86-8506-2f36c7a87fd6"
          />
        </el-form-item>

        <el-form-item label="后端代理池（公开页不填单账号代理时使用）">
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
</style>
