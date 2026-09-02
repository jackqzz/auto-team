<script setup>
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { Icon } from '@iconify/vue'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import { useProxyStore } from '@/stores/proxy'

const router = useRouter()
const statsStore = useStatsStore()
const { stats } = storeToRefs(statsStore)
const { autoStatus } = storeToRefs(useRuntimeStore())
const proxyStore = useProxyStore()

const autoStateLabel = computed(() => ({
  stopped: '未运行',
  running: '运行中',
  paused: '已暂停',
}[autoStatus.value.state] || autoStatus.value.state))

const autoStateType = computed(() => ({
  stopped: 'info',
  running: 'success',
  paused: 'warning',
}[autoStatus.value.state] || 'info'))

const autoProgressPercent = computed(() => {
  if (!autoStatus.value.task_total || autoStatus.value.task_total_known === false) return 0
  const completed = autoStatus.value.task_completed || 0
  const total = autoStatus.value.task_total || 1
  return Math.min(100, Math.round((completed / total) * 100))
})

const quickShortcuts = [
  {
    title: '批量导入邮箱',
    desc: '导入接码邮箱/Outlook/自定义格式',
    icon: 'lucide:upload-cloud',
    path: '/import',
    color: '#409eff',
  },
  {
    title: '全自动批量跑号',
    desc: '多并发自动化批量注册与风控处理',
    icon: 'lucide:play-circle',
    path: '/auto',
    color: '#67c23a',
  },
  {
    title: '已注册账号管理',
    desc: '查看注册结果、导出凭证、自动入库',
    icon: 'lucide:users',
    path: '/registered',
    color: '#36cfc9',
  },
  {
    title: '母号空间管理',
    desc: '团队母号、席位配额与邀请管理',
    icon: 'lucide:layout-grid',
    path: '/workspaces',
    color: '#722ed1',
  },
  {
    title: '候选成员管理',
    desc: '子号候选池、席位自动化与额度巡检',
    icon: 'lucide:user-check',
    path: '/candidate-management',
    color: '#13c2c2',
  },
  {
    title: '公开 401 重登录',
    desc: '浏览器直连检测、2FA 重登与号池推送',
    icon: 'lucide:refresh-cw',
    path: '/public-relogin',
    color: '#fa8c16',
  },
  {
    title: '系统代理池',
    desc: '管理动态代理、测试连通性与租借计数',
    icon: 'lucide:network',
    path: '/proxy-pool',
    color: '#eb2f96',
  },
  {
    title: '待注册邮箱池',
    desc: '管理待注册邮箱、重置失败与分组分配',
    icon: 'lucide:mail',
    path: '/pool',
    color: '#1890ff',
  },
]
</script>

<template>
  <div class="page-container">
    <!-- Hero KPI Metrics Grid -->
    <div class="hero-kpi-grid">
      <div class="kpi-card" @click="router.push('/pool')">
        <div class="kpi-header">
          <span class="kpi-title">待注册邮箱总数</span>
          <Icon icon="lucide:mail" class="kpi-type-icon" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val">{{ stats.total || 0 }}</div>
          <div class="kpi-hint">系统当前导入的接码邮箱总量</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item"><i class="dot dot-success" /> 可用: {{ stats.available || 0 }}</span>
          <span class="kpi-sub-item"><i class="dot dot-warning" /> 占用: {{ stats.in_use || 0 }}</span>
        </div>
      </div>

      <div class="kpi-card" @click="router.push('/registered')">
        <div class="kpi-header">
          <span class="kpi-title">注册成功 (Done)</span>
          <Icon icon="lucide:check-circle-2" class="kpi-type-icon text-success" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-success">{{ stats.done || 0 }}</div>
          <div class="kpi-hint">已完成 OpenAI 账号注册</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item">
            成功率: {{ stats.total ? Math.round(((stats.done || 0) / stats.total) * 100) : 0 }}%
          </span>
        </div>
      </div>

      <div class="kpi-card" @click="router.push('/pool')">
        <div class="kpi-header">
          <span class="kpi-title">注册失败 (Failed)</span>
          <Icon icon="lucide:x-circle" class="kpi-type-icon text-danger" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-danger">{{ stats.failed || 0 }}</div>
          <div class="kpi-hint">支持一键重置为 available 重试</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item"><i class="dot dot-danger" /> 待处理/重试</span>
        </div>
      </div>

      <div class="kpi-card" @click="router.push('/proxy-pool')">
        <div class="kpi-header">
          <span class="kpi-title">系统代理池</span>
          <Icon icon="lucide:network" class="kpi-type-icon text-primary" />
        </div>
        <div class="kpi-body">
          <div class="kpi-val text-primary">{{ proxyStore.count }}</div>
          <div class="kpi-hint">全局轮询代理与风控切换</div>
        </div>
        <div class="kpi-footer">
          <span class="kpi-sub-item">活跃代理池配置</span>
        </div>
      </div>
    </div>

    <!-- Status & Workflows Row -->
    <div class="dashboard-columns">
      <!-- Auto Loop Status Card -->
      <el-card shadow="never" class="dash-card">
        <template #header>
          <div class="card-header-flex">
            <div class="header-left">
              <Icon icon="lucide:activity" class="header-icon" />
              <span class="header-title">自动批量跑号监控</span>
            </div>
            <el-tag :type="autoStateType" size="small" effect="plain" class="status-tag">
              <i class="dot" :class="`dot-${autoStateType}`" />
              {{ autoStateLabel }}
            </el-tag>
          </div>
        </template>

        <div class="auto-status-content">
          <div class="auto-metrics-row">
            <div class="metric-box">
              <span class="m-label">并发工作协程</span>
              <span class="m-val">{{ autoStatus.concurrency || 1 }}</span>
            </div>
            <div class="metric-box">
              <span class="m-label">注册成功</span>
              <span class="m-val text-success">{{ autoStatus.registered_ok || 0 }}</span>
            </div>
            <div class="metric-box">
              <span class="m-label">最终失败</span>
              <span class="m-val text-danger">{{ autoStatus.registered_fail || 0 }}</span>
            </div>
            <div class="metric-box">
              <span class="m-label">重试次数</span>
              <span class="m-val text-warning">{{ autoStatus.retry_count || 0 }}</span>
            </div>
          </div>

          <div class="progress-box">
            <div class="progress-info">
              <span>任务进度</span>
              <span>
                {{ autoStatus.task_completed || 0 }} /
                {{ autoStatus.task_total_known === false || autoStatus.task_total == null ? '不限' : autoStatus.task_total }}
              </span>
            </div>
            <el-progress
              :percentage="autoProgressPercent"
              :stroke-width="8"
              :status="autoStatus.state === 'running' ? '' : autoStatus.state === 'paused' ? 'warning' : 'info'"
            />
          </div>

          <div class="card-action-footer">
            <el-button type="primary" class="action-btn" @click="router.push('/auto')">
              <Icon icon="lucide:play" class="btn-icon" /> 前往自动批量控制台
            </el-button>
            <el-button class="action-btn" @click="router.push('/register')">
              <Icon icon="lucide:sliders" class="btn-icon" /> 单次注册测试
            </el-button>
          </div>
        </div>
      </el-card>

      <!-- System Quick Shortcuts Grid Card -->
      <el-card shadow="never" class="dash-card">
        <template #header>
          <div class="card-header-flex">
            <div class="header-left">
              <Icon icon="lucide:compass" class="header-icon" />
              <span class="header-title">系统核心功能导航</span>
            </div>
          </div>
        </template>

        <div class="shortcuts-grid">
          <div
            v-for="item in quickShortcuts"
            :key="item.path"
            class="shortcut-item"
            @click="router.push(item.path)"
          >
            <div class="shortcut-icon-box" :style="{ backgroundColor: item.color + '15', color: item.color }">
              <Icon :icon="item.icon" class="shortcut-icon" />
            </div>
            <div class="shortcut-info">
              <div class="shortcut-title">{{ item.title }}</div>
              <div class="shortcut-desc">{{ item.desc }}</div>
            </div>
            <Icon icon="lucide:arrow-right" class="shortcut-arrow" />
          </div>
        </div>
      </el-card>
    </div>
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
  cursor: pointer;
  box-shadow: var(--app-shadow-sm);
  transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s;
}

.kpi-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--app-shadow-md);
  border-color: var(--el-color-primary-light-5, #a0cfff);
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
  font-size: 26px;
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
.dot-info { background-color: var(--el-text-color-placeholder, #c0c4cc); }

.text-primary { color: var(--el-color-primary, #409eff); }
.text-success { color: var(--el-color-success, #67c23a); }
.text-warning { color: var(--el-color-warning, #e6a23c); }
.text-danger { color: var(--el-color-danger, #f56c6c); }

/* Dashboard Columns */
.dashboard-columns {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
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

.status-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

/* Auto status content */
.auto-status-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.auto-metrics-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}

.metric-box {
  background: var(--el-fill-color-light, #f5f7fa);
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: var(--app-radius-sm);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.m-label {
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
}

.m-val {
  font-size: 18px;
  font-weight: 700;
  color: var(--el-text-color-primary, #303133);
}

.progress-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.progress-info {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--el-text-color-regular, #606266);
}

.card-action-footer {
  display: flex;
  gap: 10px;
  padding-top: 8px;
}

.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.btn-icon {
  font-size: 14px;
}

/* Shortcuts Grid */
.shortcuts-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px;
}

.shortcut-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: var(--app-radius-sm);
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  background: var(--el-fill-color-lighter, #fafafa);
  cursor: pointer;
  transition: all 0.2s;
}

.shortcut-item:hover {
  background: var(--el-bg-color-overlay, #ffffff);
  border-color: var(--el-color-primary-light-5, #a0cfff);
  box-shadow: var(--app-shadow-md);
  transform: translateX(2px);
}

.shortcut-icon-box {
  width: 36px;
  height: 36px;
  border-radius: var(--app-radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.shortcut-icon {
  font-size: 18px;
}

.shortcut-info {
  flex: 1;
  min-width: 0;
}

.shortcut-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary, #303133);
  margin-bottom: 2px;
}

.shortcut-desc {
  font-size: 11px;
  color: var(--el-text-color-secondary, #909399);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.shortcut-arrow {
  font-size: 14px;
  color: var(--el-text-color-placeholder, #c0c4cc);
  transition: transform 0.2s;
}

.shortcut-item:hover .shortcut-arrow {
  transform: translateX(3px);
  color: var(--el-color-primary, #409eff);
}

@media (max-width: 768px) {
  .auto-metrics-row {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
