<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Icon } from '@iconify/vue'
import { redeemCode } from '@/api/redeemCodes'

const route = useRoute()
const codesText = ref('')
const format = ref('sub2api')
const loading = ref(false)
const result = ref(null)

const CODE_RE = /^[A-Z0-9]{12}$/

const parsed = computed(() => {
  const seen = new Set()
  const valid = []
  const invalid = []
  for (const line of codesText.value.split(/\r?\n/)) {
    const code = line.trim().toUpperCase()
    if (!code || seen.has(code)) continue
    seen.add(code)
    ;(CODE_RE.test(code) ? valid : invalid).push(code)
  }
  return { valid, invalid }
})

const FORMATS = [
  { id: 'sub2api', label: 'Sub2API 凭证', hint: 'JSON，兼容 Sub2API 批量导入' },
  { id: 'cpa', label: 'CPA 凭证', hint: '多账号自动打包 ZIP' },
  { id: 'email_pw_2fa', label: '账号密码 + 2FA', hint: '明文文本，需兑换码开启该权限' },
]

onMounted(() => {
  const preset = String(route.query.code || '').trim().toUpperCase()
  if (preset) codesText.value = preset
})

function b64ToBytes(b64) {
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

function saveBlob(bytes, filename, mime) {
  const blob = new Blob([bytes], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

async function redeem() {
  if (!parsed.value.valid.length) {
    return ElMessage.warning('请输入兑换码（每行一个，12 位大写字母+数字）')
  }
  loading.value = true
  try {
    const r = await redeemCode(parsed.value.valid, format.value)
    result.value = {
      emails: r.emails || (r.email ? [r.email] : []),
      filename: r.filename,
      text: r.text,
      failed: r.failed || [],
      invalid: parsed.value.invalid,
    }
    if (r.text === undefined) {
      saveBlob(b64ToBytes(r.b64), r.filename || 'credentials.json', r.mime)
    }
    const okCount = result.value.emails.length
    const badCount = result.value.failed.length + result.value.invalid.length
    ElMessage[badCount ? 'warning' : 'success'](
      `已兑换 ${okCount} 个账号` + (badCount ? `，${badCount} 个码失败` : ''),
    )
  } catch (e) {
    result.value = null
    ElMessage.error(e.message || '兑换失败')
  } finally {
    loading.value = false
  }
}

async function copySecretText() {
  try {
    await navigator.clipboard.writeText(result.value.text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}
</script>

<template>
  <div class="redeem-page">
    <el-card shadow="never" class="redeem-card">
      <div class="redeem-head">
        <span class="redeem-logo"><Icon icon="lucide:ticket" :width="22" /></span>
        <div>
          <div class="redeem-title">凭证兑换</div>
          <div class="redeem-sub">输入兑换码，兑换绑定账号的凭证</div>
        </div>
        <a class="nav-link" href="#/public-relogin">
          401 重登<Icon icon="lucide:arrow-right" />
        </a>
      </div>

      <el-form label-position="top" @submit.prevent>
        <el-form-item>
          <template #label>
            兑换码
            <span class="code-count" v-if="parsed.valid.length">
              已识别 {{ parsed.valid.length }} 个
            </span>
          </template>
          <el-input
            v-model="codesText"
            type="textarea"
            :rows="5"
            placeholder="每行一个 12 位兑换码，支持批量"
            class="code-input"
            @keyup.ctrl.enter="redeem"
          />
        </el-form-item>

        <el-form-item label="凭证格式">
          <el-radio-group v-model="format">
            <el-radio v-for="f in FORMATS" :key="f.id" :value="f.id" class="fmt-radio">
              <span class="fmt-label">{{ f.label }}</span>
              <span class="fmt-hint">{{ f.hint }}</span>
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <el-button
          type="primary"
          size="large"
          class="redeem-btn"
          :loading="loading"
          :disabled="!parsed.valid.length"
          @click="redeem"
        >
          <Icon
            :icon="format === 'email_pw_2fa' ? 'lucide:key-round' : 'lucide:download'"
            class="btn-icon"
          />
          {{ format === 'email_pw_2fa' ? '兑换' : '兑换并下载'
          }}{{ parsed.valid.length > 1 ? `（${parsed.valid.length} 个码）` : '' }}
        </el-button>
      </el-form>

      <template v-if="result">
        <el-alert
          v-if="result.emails.length"
          type="success"
          :closable="false"
          class="redeem-result"
        >
          <template #title>
            已兑换 {{ result.emails.length }} 个账号{{
              result.text === undefined ? `，文件 ${result.filename} 已下载` : '的明文凭证'
            }}
          </template>
          <div v-for="e in result.emails" :key="e" class="result-line">{{ e }}</div>
        </el-alert>
        <template v-if="result.text !== undefined">
          <el-input
            :model-value="result.text"
            type="textarea"
            :rows="Math.min(Math.max(result.emails.length + 1, 4), 14)"
            readonly
            class="secret-area"
          />
          <div class="secret-actions">
            <el-button size="small" @click="copySecretText">复制</el-button>
            <el-button
              size="small"
              @click="
                saveBlob(
                  new TextEncoder().encode(result.text),
                  result.filename || 'credentials.txt',
                  'text/plain;charset=utf-8',
                )
              "
            >
              下载文件
            </el-button>
          </div>
        </template>
        <el-alert
          v-if="result.failed.length || result.invalid.length"
          type="warning"
          :closable="false"
          class="redeem-result"
        >
          <template #title>{{ result.failed.length + result.invalid.length }} 个码未兑换</template>
          <div v-for="f in result.failed" :key="f.code" class="result-line">
            {{ f.code }}：{{ f.error }}
          </div>
          <div v-for="c in result.invalid" :key="c" class="result-line">
            {{ c }}：格式不正确
          </div>
        </el-alert>
      </template>

      <div class="redeem-tips">
        <div>· 每行一个兑换码，批量兑换合并成一个文件下载</div>
        <div>· 兑换码可重复使用，但只能下载它绑定的那一个账号</div>
        <div>· 下载的加密凭证可在「公开重登」页导入、解密、检测额度和重登</div>
        <div>· 「账号密码 + 2FA」为明文兑换，仅对生成时已开启该权限的兑换码可用</div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.redeem-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--app-content-bg);
  padding: 24px;
}
.redeem-card {
  width: 480px;
  max-width: 100%;
  border-radius: var(--app-radius-lg);
}
.redeem-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}
.redeem-logo {
  width: 42px;
  height: 42px;
  border-radius: var(--app-radius-md);
  background: var(--brand);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.redeem-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--app-title);
}
.redeem-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.nav-link {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 13px;
  color: var(--el-color-primary);
  text-decoration: none;
  white-space: nowrap;
}
.nav-link:hover { text-decoration: underline; }
.code-input :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 14px;
  letter-spacing: 2px;
  line-height: 1.8;
}
.code-count {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-color-primary);
}
.fmt-radio {
  display: flex;
  height: auto;
  padding: 6px 0;
}
.fmt-label { font-weight: 500; }
.fmt-hint {
  display: block;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.redeem-btn { width: 100%; }
.btn-icon { margin-right: 6px; }
.redeem-result { margin-top: 16px; }
.secret-area {
  margin-top: 12px;
}
.secret-area :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.7;
}
.secret-actions {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.result-line {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  margin-top: 4px;
}
.redeem-tips {
  margin-top: 18px;
  font-size: 12px;
  line-height: 1.9;
  color: var(--el-text-color-secondary);
}
</style>
