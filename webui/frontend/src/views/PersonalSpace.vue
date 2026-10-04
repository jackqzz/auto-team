<script setup>
import { computed, onActivated, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { fmtTime } from "@/api/request";
import { listRegistered } from "@/api/register";
import {
  listPersonalCandidates,
  assignPersonalCandidates,
  removePersonalCandidates,
  getPersonalSettings,
  savePersonalSettings,
  personalQuotaStatus,
  startPersonalQuota,
  stopPersonalQuota,
  refreshPersonalQuota,
  reloginPersonal,
  trashPersonal,
  restorePersonal,
  deletePersonalRows,
  pushPersonalToCpa,
  pushPersonalToSub2api,
} from "@/api/personalSpace";

const rows = ref([]);
const loading = ref(false);
const keyword = ref("");
const groupFilter = ref("");
const trashView = ref(false); // false=成员列表 true=垃圾箱
const totalActive = ref(0);
const totalTrashed = ref(0);
const selected = ref([]);
const tableRef = ref(null);

const settings = ref({});
const settingsReady = ref(false);
const settingsVisible = ref(false);
const quotaStatus = ref({ running: false, enabled: false, next_at: null });

const assignVisible = ref(false);
const assignLoading = ref(false);
const assignRows = ref([]);
const assignKeyword = ref("");
const assignSelected = ref([]);
const assignPage = ref(1);
const assignTotal = ref(0);
const assignBusy = ref(false);

const quotaBusy = ref(false);
const reloginBusy = ref(false);
const pushBusy = ref("");
const trashBusy = ref(false);
let statusTimer = null;

// ── 数据加载 ──

async function loadRows() {
  loading.value = true;
  try {
    const resp = await listPersonalCandidates({
      trash_status: trashView.value ? "trashed" : "active",
      keyword: keyword.value,
      group_name: groupFilter.value,
      limit: 2000,
    });
    rows.value = resp.items || [];
    totalActive.value = resp.total_active ?? 0;
    totalTrashed.value = resp.total_trashed ?? 0;
  } catch (e) {
    ElMessage.error("加载个人空间成员失败: " + (e.message || e));
  } finally {
    loading.value = false;
  }
}

async function loadSettings() {
  try {
    const resp = await getPersonalSettings();
    settings.value = { ...resp.settings };
    settingsReady.value = true;
  } catch (e) {
    ElMessage.error("加载个人空间设置失败: " + (e.message || e));
  }
}

async function loadQuotaStatus() {
  try {
    quotaStatus.value = await personalQuotaStatus();
  } catch (_) { /* 状态轮询失败静默 */ }
}

const groupOptions = computed(() => {
  const s = new Set();
  for (const r of rows.value) if (r.group_name) s.add(r.group_name);
  return [...s];
});

// ── 额度展示 ──

function windowRemain(w) {
  if (!w || w.used_percent == null) return null;
  return Math.max(0, Math.round(100 - Number(w.used_percent)));
}

function quotaInfo(row) {
  const q = row?.quota;
  if (!q || typeof q !== "object" || !Object.keys(q).length) return null;
  if (q.error_code) return { isError: true, errorCode: q.error_code, updatedAt: fmtTime(q.updated_at) };
  const p = q.primary || {};
  const s = q.secondary || {};
  const fiveHour = Number(p.window_seconds) === 18000 ? windowRemain(p)
    : Number(s.window_seconds) === 18000 ? windowRemain(s) : null;
  const weekly = Number(p.window_seconds) === 604800 ? windowRemain(p)
    : Number(s.window_seconds) === 604800 ? windowRemain(s) : null;
  const unknown = fiveHour == null && weekly == null
    ? (windowRemain(p) ?? windowRemain(s)) : null;
  return {
    isError: false, fiveHour, weekly, unknown,
    credits: q.credits_balance ?? "",
    plan: q.plan_type || "",
    updatedAt: fmtTime(q.updated_at),
  };
}

function quotaClass(pct) {
  if (pct == null) return "";
  if (pct >= 80) return "text-success";
  if (pct >= 30) return "text-warning";
  return "text-danger";
}

const credentialText = { personal_credential: "个人凭证", none: "无凭证", unavailable: "已失效" };
const credentialType = { personal_credential: "success", none: "info", unavailable: "danger" };

// ── 设置保存 ──

async function saveSettings() {
  try {
    const resp = await savePersonalSettings(settings.value);
    settings.value = { ...resp.settings };
    ElMessage.success("个人空间设置已保存");
    loadQuotaStatus();
  } catch (e) {
    ElMessage.error("保存失败: " + (e.message || e));
  }
}

// ── 定时额度 ──

async function toggleQuotaSchedule() {
  try {
    if (quotaStatus.value.enabled) {
      await stopPersonalQuota();
      ElMessage.success("已停止个人空间定时额度刷新");
    } else {
      await startPersonalQuota();
      ElMessage.success("已开启个人空间定时额度刷新");
    }
    loadQuotaStatus();
    loadSettings();
  } catch (e) {
    ElMessage.error("操作失败: " + (e.message || e));
  }
}

const nextAtText = computed(() => {
  const t = quotaStatus.value?.next_at;
  if (!t) return "";
  const diff = Math.round(t - Date.now() / 1000);
  if (diff <= 0) return "即将刷新";
  return `${Math.floor(diff / 60)}分${diff % 60}秒后`;
});

// ── 划入 ──

async function openAssign() {
  assignVisible.value = true;
  assignPage.value = 1;
  assignKeyword.value = "";
  await loadAssignRows();
}

async function loadAssignRows() {
  assignLoading.value = true;
  try {
    const resp = await listRegistered({
      limit: 50,
      offset: (assignPage.value - 1) * 50,
      filter: "all",
    });
    const inPool = new Set(rows.value.map((r) => r.email));
    assignRows.value = (resp.items || []).map((r) => ({
      ...r,
      _in_pool: inPool.has((r.email || "").toLowerCase()),
    }));
    assignTotal.value = resp.total || 0;
  } catch (e) {
    ElMessage.error("加载注册结果失败: " + (e.message || e));
  } finally {
    assignLoading.value = false;
  }
}

const assignFiltered = computed(() => {
  const kw = assignKeyword.value.trim().toLowerCase();
  if (!kw) return assignRows.value;
  return assignRows.value.filter((r) =>
    (r.email || "").toLowerCase().includes(kw)
    || (r.group_name || "").toLowerCase().includes(kw)
  );
});

async function doAssign() {
  const emails = assignSelected.value.map((r) => r.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择要划入的账号");
  assignBusy.value = true;
  try {
    const resp = await assignPersonalCandidates(emails);
    ElMessage.success(`已划入 ${resp.added} 个账号到个人空间`);
    assignVisible.value = false;
    loadRows();
  } catch (e) {
    ElMessage.error("划入失败: " + (e.message || e));
  } finally {
    assignBusy.value = false;
  }
}

// ── 批量操作 ──

function checkedEmails() {
  return selected.value.map((r) => r.email).filter(Boolean);
}

async function batchQuota() {
  const emails = checkedEmails();
  if (!emails.length) return ElMessage.warning("请先勾选账号");
  quotaBusy.value = true;
  try {
    const resp = await refreshPersonalQuota(emails);
    const ok = (resp.results || []).filter((r) => r.ok).length;
    const fail = (resp.results || []).length - ok;
    if (fail) ElMessage.warning(`额度刷新完成：成功 ${ok}，失败 ${fail}`);
    else ElMessage.success(`额度刷新完成：${ok} 个账号`);
    loadRows();
  } catch (e) {
    ElMessage.error("额度刷新失败: " + (e.message || e));
  } finally {
    quotaBusy.value = false;
  }
}

async function batchRelogin() {
  const emails = checkedEmails();
  if (!emails.length) return ElMessage.warning("请先勾选账号");
  reloginBusy.value = true;
  try {
    await reloginPersonal(emails);
    ElMessage.success(`已入队 ${emails.length} 个账号的凭证刷新（登录任务在后台执行）`);
  } catch (e) {
    ElMessage.error("凭证刷新入队失败: " + (e.message || e));
  } finally {
    reloginBusy.value = false;
  }
}

async function batchPush(target) {
  const emails = checkedEmails();
  if (!emails.length) return ElMessage.warning("请先勾选账号");
  pushBusy.value = target;
  try {
    const fn = target === "cpa" ? pushPersonalToCpa : pushPersonalToSub2api;
    const resp = await fn({ emails });
    if (resp.failed) ElMessage.warning(`推送完成：成功 ${resp.succeeded}，失败 ${resp.failed}`);
    else ElMessage.success(`推送完成：${resp.succeeded} 个账号已推到 ${target === "cpa" ? "CPA" : "Sub2API"}`);
  } catch (e) {
    ElMessage.error("推送失败: " + (e.message || e));
  } finally {
    pushBusy.value = "";
  }
}

async function batchTrash() {
  const emails = checkedEmails();
  if (!emails.length) return ElMessage.warning("请先勾选账号");
  try {
    await ElMessageBox.confirm(
      `确认把 ${emails.length} 个账号移入个人空间垃圾箱？已推送到 CPA 的凭证会一并删除。`,
      "移入垃圾箱", { type: "warning" },
    );
  } catch (_) { return; }
  trashBusy.value = true;
  try {
    await trashPersonal(emails);
    ElMessage.success(`已入箱 ${emails.length} 个账号`);
    loadRows();
  } catch (e) {
    ElMessage.error("入箱失败: " + (e.message || e));
  } finally {
    trashBusy.value = false;
  }
}

async function batchRemove() {
  const emails = checkedEmails();
  if (!emails.length) return ElMessage.warning("请先勾选账号");
  try {
    await ElMessageBox.confirm(
      `确认把 ${emails.length} 个账号从个人空间移除？账号本身仍在注册结果里。`,
      "移出个人空间", { type: "warning" },
    );
  } catch (_) { return; }
  try {
    await removePersonalCandidates(emails);
    ElMessage.success(`已移除 ${emails.length} 个账号`);
    loadRows();
  } catch (e) {
    ElMessage.error("移除失败: " + (e.message || e));
  }
}

async function batchRestore() {
  const emails = checkedEmails();
  if (!emails.length) return ElMessage.warning("请先勾选账号");
  try {
    await restorePersonal(emails);
    ElMessage.success(`已还原 ${emails.length} 个账号`);
    loadRows();
  } catch (e) {
    ElMessage.error("还原失败: " + (e.message || e));
  }
}

async function batchDeleteRows() {
  const emails = checkedEmails();
  if (!emails.length) return ElMessage.warning("请先勾选账号");
  try {
    await ElMessageBox.confirm(
      `确认把 ${emails.length} 个账号从垃圾箱彻底移除？账号仍在注册结果里。`,
      "彻底移除", { type: "error" },
    );
  } catch (_) { return; }
  try {
    await deletePersonalRows(emails);
    ElMessage.success(`已移除 ${emails.length} 个账号`);
    loadRows();
  } catch (e) {
    ElMessage.error("移除失败: " + (e.message || e));
  }
}

// ── 生命周期 ──

async function reload() {
  await Promise.all([loadRows(), loadSettings(), loadQuotaStatus()]);
}

onMounted(() => {
  reload();
  statusTimer = setInterval(loadQuotaStatus, 15000);
});
onActivated(reload);
onBeforeUnmount(() => clearInterval(statusTimer));
</script>

<template>
  <div class="personal-space page-wrap">
    <el-card shadow="never" class="head-card">
      <div class="head-row">
        <div>
          <div class="page-title">个人空间</div>
          <div class="page-desc">Free 账号池：划分注册的免费账号进来，定时刷额度、401 自动重登、推送号池。登录固定走个人空间，凭证就是个人 token。</div>
        </div>
        <div class="head-actions">
          <div class="quota-status" :class="{ on: quotaStatus.enabled }">
            <span class="dot" :class="{ live: quotaStatus.running }" />
            定时额度 {{ quotaStatus.enabled ? (quotaStatus.running ? `开启 · ${nextAtText || '运行中'}` : '已开启待启动') : '关闭' }}
          </div>
          <el-button size="small" :type="quotaStatus.enabled ? 'warning' : 'primary'" @click="toggleQuotaSchedule">
            {{ quotaStatus.enabled ? '停止定时刷新' : '开启定时刷新' }}
          </el-button>
          <el-button size="small" @click="settingsVisible = true">空间设置</el-button>
          <el-button size="small" type="primary" @click="openAssign">划入账号</el-button>
        </div>
      </div>
      <div class="stat-row">
        <span>成员 <strong>{{ totalActive }}</strong></span>
        <span>垃圾箱 <strong>{{ totalTrashed }}</strong></span>
        <el-radio-group v-model="trashView" size="small" @change="loadRows" class="view-switch">
          <el-radio-button :value="false">成员列表</el-radio-button>
          <el-radio-button :value="true">垃圾箱</el-radio-button>
        </el-radio-group>
      </div>
    </el-card>

    <el-card shadow="never">
      <div class="toolbar">
        <el-input v-model="keyword" placeholder="搜索邮箱 / 分组" clearable size="small" style="width: 220px" @change="loadRows" @clear="loadRows" />
        <el-select v-model="groupFilter" placeholder="分组" clearable size="small" style="width: 140px" @change="loadRows">
          <el-option v-for="g in groupOptions" :key="g" :label="g" :value="g" />
        </el-select>
        <el-button size="small" @click="loadRows">刷新</el-button>
        <div class="toolbar-right" v-if="!trashView">
          <el-button size="small" :loading="quotaBusy" @click="batchQuota">查额度</el-button>
          <el-button size="small" :loading="reloginBusy" @click="batchRelogin">刷新凭证</el-button>
          <el-button size="small" :loading="pushBusy === 'cpa'" @click="batchPush('cpa')">推 CPA</el-button>
          <el-button size="small" :loading="pushBusy === 'sub2api'" @click="batchPush('sub2api')">推 Sub2API</el-button>
          <el-button size="small" type="warning" :loading="trashBusy" @click="batchTrash">入箱</el-button>
          <el-button size="small" type="danger" plain @click="batchRemove">移出</el-button>
        </div>
        <div class="toolbar-right" v-else>
          <el-button size="small" type="primary" plain @click="batchRestore">还原</el-button>
          <el-button size="small" type="danger" @click="batchDeleteRows">彻底移除</el-button>
        </div>
      </div>

      <el-table :data="rows" v-loading="loading" size="small" ref="tableRef"
        @selection-change="(v) => (selected = v)" row-key="email" stripe>
        <el-table-column type="selection" width="40" />
        <el-table-column prop="email" label="邮箱" min-width="220" show-overflow-tooltip />
        <el-table-column prop="group_name" label="分组" width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ row.group_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="凭证" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="credentialType[row.credential_status] || 'info'" effect="light">
              {{ credentialText[row.credential_status] || row.credential_status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="额度" min-width="240">
          <template #default="{ row }">
            <template v-if="quotaInfo(row)">
              <div v-if="quotaInfo(row).isError" class="quota-err">
                <el-tag size="small" type="danger" effect="light">HTTP {{ quotaInfo(row).errorCode }}</el-tag>
              </div>
              <div v-else class="quota-pills">
                <span v-if="quotaInfo(row).fiveHour != null" class="pill">
                  5h剩余 <b :class="quotaClass(quotaInfo(row).fiveHour)">{{ quotaInfo(row).fiveHour }}%</b>
                </span>
                <span v-if="quotaInfo(row).weekly != null" class="pill">
                  周剩余 <b :class="quotaClass(quotaInfo(row).weekly)">{{ quotaInfo(row).weekly }}%</b>
                </span>
                <span v-if="quotaInfo(row).unknown != null" class="pill">
                  剩余 <b class="text-primary">{{ quotaInfo(row).unknown }}%</b>
                </span>
                <span v-if="quotaInfo(row).credits !== '' && quotaInfo(row).credits != null" class="pill">
                  余额 <b>{{ quotaInfo(row).credits }}</b>
                </span>
                <span class="quota-time">{{ quotaInfo(row).updatedAt }}</span>
              </div>
            </template>
            <span v-else class="quota-empty">未查询</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.trash_status === 'trashed'" size="small" type="danger" effect="plain">
              已入箱{{ row.trash_reason ? `·${row.trash_reason}` : '' }}
            </el-tag>
            <el-tag v-else-if="row.trash_status === 'scheduled'" size="small" type="warning" effect="plain">排队入箱</el-tag>
            <el-tag v-else size="small" type="success" effect="plain">在池</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="加入时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <div v-if="!loading && !rows.length" class="empty-hint">
        {{ trashView ? '垃圾箱是空的' : '还没有成员，点右上角「划入账号」把注册的免费账号划进来' }}
      </div>
    </el-card>

    <!-- 划入对话框 -->
    <el-dialog v-model="assignVisible" title="划入账号到个人空间" width="640px">
      <div class="assign-bar">
        <el-input v-model="assignKeyword" placeholder="本页内搜索邮箱/分组" clearable size="small" style="width: 240px" />
        <span class="assign-hint">每页 50 条，已划入的会标灰</span>
      </div>
      <el-table :data="assignFiltered" v-loading="assignLoading" size="small" height="380"
        @selection-change="(v) => (assignSelected = v)">
        <el-table-column type="selection" width="40" :selectable="(row) => !row._in_pool" />
        <el-table-column prop="email" label="邮箱" min-width="220" show-overflow-tooltip />
        <el-table-column prop="group_name" label="分组" width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ row.group_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="row._in_pool" size="small" type="info" effect="plain">已在池</el-tag>
            <el-tag v-else-if="row.account_status === 'permanently_invalid'" size="small" type="danger" effect="plain">已失效</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div class="assign-foot">
        <el-pagination layout="prev, pager, next, total" :total="assignTotal" :page-size="50"
          :current-page="assignPage" small @current-change="(p) => { assignPage = p; loadAssignRows(); }" />
      </div>
      <template #footer>
        <el-button @click="assignVisible = false">取消</el-button>
        <el-button type="primary" :loading="assignBusy" @click="doAssign">划入所选</el-button>
      </template>
    </el-dialog>

    <!-- 设置抽屉 -->
    <el-drawer v-model="settingsVisible" title="个人空间设置" size="420px">
      <div class="settings-form" v-if="settingsReady">
        <div class="sec-title">额度刷新</div>
        <el-form label-position="top" size="small">
          <el-form-item label="定时额度刷新">
            <el-switch v-model="settings.quota_enabled" @change="saveSettings" />
            <span class="field-hint">开启后按间隔批量刷新成员额度</span>
          </el-form-item>
          <el-form-item label="刷新间隔（分钟）">
            <el-input-number v-model="settings.quota_interval_minutes" :min="1" :max="1440" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="401 自动重登录">
            <el-switch v-model="settings.relogin_on_401" @change="saveSettings" />
            <span class="field-hint">额度查询遇到 401 时自动走登录链路重取个人凭证</span>
          </el-form-item>
          <el-form-item label="自动化暂停">
            <el-switch v-model="settings.automation_paused" @change="saveSettings" />
          </el-form-item>

          <div class="sec-title">网络</div>
          <el-form-item label="候选人代理池（每行一条）">
            <el-input v-model="settings.proxy_pool" type="textarea" :rows="4"
              placeholder="socks5://user:pass@host:port" @change="saveSettings" />
            <span class="field-hint">额度查询与登录重登都从这里租代理</span>
          </el-form-item>
          <el-form-item label="并发">
            <el-input-number v-model="settings.concurrency" :min="1" :max="20" @change="saveSettings" />
          </el-form-item>

          <div class="sec-title">号池推送</div>
          <el-form-item label="凭证获取后自动推送号池">
            <el-switch v-model="settings.auto_push" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="CPA 推送">
            <el-switch v-model="settings.auto_push_cpa_enabled" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="CPA URL（留空跟随全局）">
            <el-input v-model="settings.auto_push_cpa_url" placeholder="https://…" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="CPA 管理密钥">
            <el-input v-model="settings.auto_push_cpa_mgmt_key" type="password" show-password @change="saveSettings" />
          </el-form-item>
          <el-form-item label="CPA 账号优先级（可为负数，默认 0）">
            <el-input-number v-model="settings.auto_push_cpa_priority" :max="1000" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="Sub2API 推送">
            <el-switch v-model="settings.auto_push_sub2api_enabled" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="Sub2API URL（留空跟随全局）">
            <el-input v-model="settings.auto_push_sub2api_url" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="Sub2API API Key">
            <el-input v-model="settings.auto_push_sub2api_api_key" type="password" show-password @change="saveSettings" />
          </el-form-item>
          <el-form-item label="Sub2API 分组 ID（逗号分隔）">
            <el-input v-model="settings.auto_push_sub2api_group_ids" @change="saveSettings" />
          </el-form-item>

          <div class="sec-title">垃圾箱</div>
          <el-form-item label="零额度自动入箱">
            <el-switch v-model="settings.trash_enabled" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="手动入箱删除 CPA 凭证">
            <el-switch v-model="settings.trash_cleanup_cpa_on_manual" @change="saveSettings" />
            <span class="field-hint">手动移入垃圾箱时同步删除已推送到 CPA 的凭证；零额度自动入箱始终删除</span>
          </el-form-item>
          <el-form-item label="入箱延迟（分钟）">
            <el-input-number v-model="settings.trash_zero_delay_minutes" :min="0" :max="1440" @change="saveSettings" />
          </el-form-item>
          <el-form-item label="耗尽判定窗口">
            <el-select v-model="settings.trash_zero_quota_window" @change="saveSettings" style="width: 100%">
              <el-option label="任一窗口耗尽" value="any" />
              <el-option label="仅 5 小时窗口" value="five_hour" />
              <el-option label="仅周窗口" value="weekly" />
            </el-select>
          </el-form-item>
        </el-form>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.personal-space { padding: 16px; }
.head-card { margin-bottom: 12px; }
.head-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
.page-title { font-size: 18px; font-weight: 600; }
.page-desc { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 4px; max-width: 560px; }
.head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.quota-status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--el-text-color-secondary); }
.quota-status.on { color: var(--el-color-primary); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--el-color-info-light-5); }
.dot.live { background: var(--el-color-success); box-shadow: 0 0 4px var(--el-color-success); }
.stat-row { display: flex; align-items: center; gap: 20px; margin-top: 12px; font-size: 13px; color: var(--el-text-color-secondary); }
.view-switch { margin-left: auto; }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.toolbar-right { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }
.quota-pills { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; font-size: 12px; }
.pill b { margin-left: 2px; }
.quota-time { color: var(--el-text-color-secondary); font-size: 11px; }
.quota-empty, .quota-err { font-size: 12px; color: var(--el-text-color-secondary); }
.text-success { color: var(--el-color-success); }
.text-warning { color: var(--el-color-warning); }
.text-danger { color: var(--el-color-danger); }
.text-primary { color: var(--el-color-primary); }
.empty-hint { text-align: center; color: var(--el-text-color-secondary); font-size: 13px; padding: 24px 0; }
.assign-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.assign-hint { font-size: 12px; color: var(--el-text-color-secondary); }
.assign-foot { display: flex; justify-content: flex-end; margin-top: 8px; }
.settings-form .sec-title { font-weight: 600; margin: 16px 0 8px; padding-top: 8px; border-top: 1px solid var(--el-border-color-lighter); }
.settings-form .sec-title:first-child { border-top: none; padding-top: 0; margin-top: 0; }
.field-hint { display: block; font-size: 12px; color: var(--el-text-color-secondary); margin-top: 4px; }
</style>
