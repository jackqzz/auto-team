<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { authStatus, loginAdmin } from '@/api/auth'

const router = useRouter()
const password = ref('')
const loading = ref(false)
const authEnabled = ref(true)

async function load() {
  try {
    const res = await authStatus()
    authEnabled.value = !!res.enabled
    if (!res.enabled || res.authenticated) router.replace('/')
  } catch (_) {}
}

async function submit() {
  loading.value = true
  try {
    await loginAdmin(password.value)
    ElMessage.success('登录成功')
    router.replace('/')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

load()
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="never">
      <template #header>
        <span class="section-title" style="margin:0">管理员登录</span>
      </template>
      <el-alert
        v-if="!authEnabled"
        type="info"
        show-icon
        :closable="false"
        title="当前未设置管理员密码，系统管理平台暂未启用鉴权。"
      />
      <el-form style="margin-top: 16px" @submit.prevent="submit">
        <el-form-item label="管理员密码">
          <el-input
            v-model="password"
            type="password"
            show-password
            autofocus
            placeholder="输入管理员密码"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" :loading="loading" style="width:100%" @click="submit">
          登录
        </el-button>
      </el-form>
      <p class="hint" style="margin-top: 12px">
        公开 401 重登录页面不需要登录；系统管理平台在设置管理员密码后需要登录。
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--app-content-bg);
  padding: 24px;
}
.login-card { width: 420px; max-width: 100%; }
</style>
