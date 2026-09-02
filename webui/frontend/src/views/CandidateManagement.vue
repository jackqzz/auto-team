<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Icon } from "@iconify/vue";
import { useRoute } from "vue-router";
import { storeToRefs } from "pinia";
import { useProxyStore } from "@/stores/proxy";
import { listWorkspaceMasters, syncWorkspace, syncWorkspaceMembers } from "@/api/workspaces";
import { listExportFormats, exportRegistered, pushRegisteredToCpa } from "@/api/register";
import { copyText, fmtTime } from "@/api/request";
import { PLAIN_CREDENTIAL_MODE_STORAGE_KEY } from "@/utils/credentialCrypto";
import {
  listCandidateOptions,
  getCandidateStats,
  listCandidateGroups,
  removeCandidates,
  updateCandidateTagStatus,
  inviteCandidates,
  setCandidateInviteStatus,
  requestJoin,
  checkCandidates,
  fetchWorkspaceCredentials,
  loginOnlyWorkspace,
  queryCandidateQuota,
  updateCandidateSeat,
  startQuotaSchedule,
  stopQuotaSchedule,
  quotaScheduleStatus,
  startAutoStandardSeatSchedule,
  stopAutoStandardSeatSchedule,
  autoStandardSeatScheduleStatus,
  startAutoProliteSeatSchedule,
  stopAutoProliteSeatSchedule,
  autoProliteSeatScheduleStatus,
  listWorkspaceTaskLogs,
  saveCandidateSettings,
  trashCandidates,
  restoreCandidatesFromTrash,
} from "@/api/workspaceCandidates";

const spaces = ref([]);
const workspaceId = ref(null);
const options = ref([]);
const selected = ref([]);
const candidateTableRef = ref(null);
const loading = ref(false);
const seatType = ref("default");
const { list: proxyList } = storeToRefs(useProxyStore());
const route = useRoute();

const exportFormats = ref([]);
const exporting = ref(false);
const exportVisible = ref(false);
const exportText = ref("");
const exportFilename = ref("export.txt");
const exportLabel = ref("导出结果");
const exportCount = ref(0);
const pushing = ref(false);

const plainCredentialMode = ref(false);
const encryptCredentials = computed(() => !plainCredentialMode.value);

const taskLogs = ref([]);
const taskLogLoading = ref(false);
const taskLogAutoRefresh = ref(true);
const taskLogBoxRef = ref(null);
let taskLogTimer = null;

const quotaRunning = ref(false);
const quotaInterval = ref(30);
const reloginOn401 = ref(false);
const autoPush = ref(false);
const nextQuotaAt = ref(0);
const taskConcurrency = ref(1);
const taskOtpTimeout = ref(180);
const taskRetry = ref(1);
const taskCooldown = ref(0);
const quotaNetworkRetries = ref(2);
const quotaProxyPool = ref("");

// 专属池为空即回退全局池，所以提示要说清当前实际生效的是哪一份。
const quotaProxyPoolHint = computed(() => {
  const lines = quotaProxyPool.value.split("\n").map((x) => x.trim()).filter(Boolean);
  if (!lines.length) return `未配置，回退全局池（${proxyList.value.length} 条）`;
  return `已配置 ${new Set(lines).size} 条，不再使用全局池`;
});

function importGlobalProxyPool() {
  if (!proxyList.value.length) return ElMessage.warning("全局代理池为空");
  quotaProxyPool.value = proxyList.value.join("\n");
  ElMessage.success(`已导入 ${proxyList.value.length} 条代理，可在此基础上删改`);
}

const trashEnabled = ref(true);
const trashInvalidEnabled = ref(true);
const trashZeroDelayMinutes = ref(60);

const seatProtectEnabled = ref(false);
const seatProtectThreshold = ref(8);
const seatProtectRefreshTime = ref("00:00");
const seatProtectUsedCount = ref(0);

const proliteSeatProtectEnabled = ref(false);
const proliteSeatProtectThreshold = ref(8);
const proliteSeatProtectRefreshTime = ref("00:00");
const proliteSeatProtectUsedCount = ref(0);

const autoStandardSeatEnabled = ref(false);
const autoStandardSeatNextAt = ref(0);
const autoProliteSeatEnabled = ref(false);
const autoProliteSeatNextAt = ref(0);
const autoSeatIntervalMinutes = ref(5);
const autoProliteCandidateSeatType = ref("default");

const candidateStats = ref({
  workspace_id: null,
  total_candidates: 0,
  trash: {
    trashed_count: 0,
    scheduled_count: 0,
    due_scheduled_count: 0,
    invalid_pending_trash_count: 0,
    invalid_total_count: 0,
    trash_enabled: true,
    trash_invalid_enabled: true,
    trash_zero_delay_minutes: 60,
  },
  seat_fulfillment: {
    standard: {
      count: 0,
      fulfilled_total: 0,
      auto_enabled: false,
      protect_enabled: false,
      protect_used_count: 0,
      protect_threshold: 8,
      protect_refresh_time: "00:00",
      protect_window_key: "",
    },
    prolite: {
      count: 0,
      fulfilled_total: 0,
      auto_enabled: false,
      protect_enabled: false,
      protect_used_count: 0,
      protect_threshold: 8,
      protect_refresh_time: "00:00",
      protect_window_key: "",
    },
    codex: {
      count: 0,
    },
    outbound_count: 0,
    auto_interval_minutes: 5,
    auto_prolite_candidate_seat_type: "default",
  },
});

const operationStatus = ref({});
const quotaTaskRunning = ref(false);
const quotaProgress = ref({ done: 0, total: 0, active: 0, succeeded: 0, failed: 0, relogged: 0 });

const page = ref(1);
const pageSize = ref(100);
const total = ref(0);

const accountStatusFilter = ref("");
const joinStatusFilter = ref("");
const credentialStatusFilter = ref("");
const seatTypeFilter = ref("");
const trashStatusFilter = ref("active");
const tagStatusFilter = ref("");
const groupNameFilter = ref("");
const searchKeyword = ref("");
const quickTab = ref("all");

const candidateGroups = ref([]);
const settingsVisible = ref(false);
const settingsActiveTab = ref("quota");
const settingsReady = ref(false);
const settingsWorkspaceId = ref(null);

const syncingWorkspace = ref(false);
const syncingWorkspaceMembers = ref(false);
const membershipTaskRunning = ref(false);
const candidateCheckRunning = ref(false);
const seatSwitchRunning = ref(false);

const candidateMembershipBusy = computed(
  () => membershipTaskRunning.value || candidateCheckRunning.value || seatSwitchRunning.value
);

let settingsLoadGeneration = 0;
let settingsSaveTimer = null;
let credentialModeLoaded = false;

function setOperation(emails, text) {
  const next = { ...operationStatus.value };
  emails.forEach((e) => { next[e] = text; });
  operationStatus.value = next;
}
function clearOperation(emails) {
  const next = { ...operationStatus.value };
  emails.forEach((e) => { delete next[e]; });
  operationStatus.value = next;
}
function setOneOperation(email, text) {
  operationStatus.value = { ...operationStatus.value, [email]: text };
}

async function runRollingPool(items, concurrency, worker) {
  let cursor = 0;
  const workerCount = Math.min(Math.max(1, Number(concurrency) || 1), items.length);
  await Promise.all(
    Array.from({ length: workerCount }, async () => {
      while (cursor < items.length) {
        const index = cursor;
        cursor += 1;
        await worker(items[index], index);
      }
    })
  );
}

function activeSelectedEmails() {
  return selected.value
    .filter((row) => row.account_status !== "permanently_invalid" && row.trash_status !== "trashed")
    .map((row) => row.email)
    .filter(Boolean);
}

function accountStatusLabel(value) {
  return value === "permanently_invalid" ? "已永久失效" : "正常";
}

function displayStatus(row) {
  if (operationStatus.value[row.email]) return operationStatus.value[row.email];
  const status = row.display_status || "not_invited";
  if (status.startsWith("quota_error_")) return `额度查询失败（${status.slice(12)}）`;
  return (
    {
      not_invited: "未邀请",
      pending_invite: "待接受邀请",
      pending_request: "待处理申请",
      joined: "已加入",
      workspace_credential: "已获得空间凭证",
      trash_scheduled: "垃圾箱待处理",
      trashed: "已入垃圾箱",
      candidate: "未邀请",
    }[status] || "未邀请"
  );
}

function workspaceJoinStatusLabel(value) {
  if (String(value || "").startsWith("quota_error_")) return `额度查询失败（${String(value).slice(12)}）`;
  return (
    {
      not_invited: "未邀请",
      pending_invite: "待接受邀请",
      pending_request: "待处理申请",
      joined: "已加入",
      join_requested: "已申请加入",
      approved: "已批准，待加入",
    }[value] || "未邀请"
  );
}

function seatLabel(value) {
  const v = String(value || "").toLowerCase().replace("-", "_");
  if (v === "default" || v === "gpt席位" || v === "标准席位") return "标准席位";
  if (v === "usage_based" || v === "usagebased" || v === "codex席位") return "Codex席位";
  if (["prolite", "pro_lite", "advanced", "advanced_seat", "premium", "premium_seat", "pro", "高级", "高级席位"].includes(v)) return "高级席位（ProLite）";
  return "—";
}

function seatTypeTagType(value) {
  const v = String(value || "").toLowerCase().replace("-", "_");
  if (v === "default" || v === "gpt席位" || v === "标准席位") return "primary";
  if (["prolite", "pro_lite", "advanced", "advanced_seat", "premium", "premium_seat", "pro", "高级", "高级席位"].includes(v)) return "warning";
  if (v === "usage_based" || v === "usagebased" || v === "codex席位") return "info";
  return "info";
}

function trashStatusLabel(value) {
  return { active: "正常", scheduled: "待入箱", trashed: "已入箱" }[String(value || "active")] || "正常";
}

function trashStatusHint(row) {
  if (!row || row.trash_status !== "scheduled" || !row.trash_due_at) return "";
  return `到期 ${new Date(row.trash_due_at * 1000).toLocaleString()}`;
}

function tagStatusLabel(value) {
  return String(value || "active") === "outbound" ? "已出库" : "正常";
}

function isSelectableCandidate(row) {
  return tagStatusFilter.value === "outbound" || String(row?.tag_status || "active") !== "outbound";
}

function quotaIneligibleReason(row) {
  if (row?.quota_ineligible_reason) return row.quota_ineligible_reason;
  if (row?.account_status === "permanently_invalid") return "账号已永久失效";
  if (row?.trash_status === "trashed") return "候选人已在垃圾箱";
  if (!row?.has_workspace_access_token) return "未获得当前空间凭证";
  const seat = String(row?.seat_label || row?.seat_type || "").trim().toLowerCase().replace(/-/g, "_");
  if (["usage_based", "usagebased", "codex", "codex席位"].includes(seat)) return "Codex席位不参与额度查询";
  return "";
}

function quotaSkipSummary(rows) {
  const counts = new Map();
  rows.forEach((row) => {
    const reason = quotaIneligibleReason(row) || "不可查询";
    counts.set(reason, (counts.get(reason) || 0) + 1);
  });
  return [...counts.entries()].map(([reason, count]) => `${reason} ${count} 个`).join("；");
}

const currentWorkspace = computed(() => spaces.value.find((x) => x.id === workspaceId.value) || null);

const currentSpaceLabel = () => {
  const s = currentWorkspace.value;
  return s ? `${s.account} · ${s.workspace_id || "无空间ID"}` : "未选择母号空间";
};

function cst(value) {
  if (!value) return "未同步";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function autoProliteCandidateLabel(value) {
  return value === "usage_based" ? "Codex席位" : value === "all" ? "全部可升级席位" : "标准席位";
}

function quotaUpdated(row) {
  try {
    const q = JSON.parse(row.quota_json || "");
    return q.error_code
      ? `失败 (HTTP ${q.error_code})`
      : q.updated_at
      ? new Date(q.updated_at * 1000).toLocaleString()
      : "未查询";
  } catch (_) {
    return "未查询";
  }
}

function parseQuotaInfo(row) {
  try {
    if (!row?.quota_json) return null;
    const q = JSON.parse(row.quota_json);
    if (!q) return null;
    const isError = Boolean(q.error_code);
    const errorCode = q.error_code ? String(q.error_code) : "";
    const primaryRemain = q.primary?.used_percent != null ? Math.max(0, 100 - Number(q.primary.used_percent)) : null;
    const secondaryRemain = q.secondary?.used_percent != null ? Math.max(0, 100 - Number(q.secondary.used_percent)) : null;
    const credits = q.credits_balance || "";
    const updatedAt = q.updated_at ? new Date(q.updated_at * 1000).toLocaleString() : "";
    return { isError, errorCode, primaryRemain, secondaryRemain, credits, updatedAt };
  } catch (_) {
    return null;
  }
}

function quotaRemainingPercent(row) {
  try {
    const q = JSON.parse(row?.quota_json || "");
    if (!q || q.error_code) return null;
    const values = [];
    if (q.primary?.used_percent != null) values.push(Math.max(0, 100 - Number(q.primary.used_percent)));
    if (q.secondary?.used_percent != null) values.push(Math.max(0, 100 - Number(q.secondary.used_percent)));
    if (!values.length) return null;
    return Math.min(...values);
  } catch (_) {
    return null;
  }
}

function isFullQuotaRow(row) {
  return quotaRemainingPercent(row) === 100;
}

function quotaErrorCode(row) {
  try {
    const q = JSON.parse(row?.quota_json || "");
    if (q?.error_code != null) return String(q.error_code);
  } catch (_) {}
  const status = String(row?.display_status || row?.status || "");
  const matched = status.match(/quota_error_(\d{3})/i);
  return matched ? matched[1] : "";
}

function isQuota401Row(row) {
  return quotaErrorCode(row) === "401";
}

function nextQuotaText() {
  return nextQuotaAt.value ? `下次额度刷新时间：${new Date(nextQuotaAt.value * 1000).toLocaleString()}` : "";
}

function taskLogTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

async function scrollTaskLogsToBottom() {
  await nextTick();
  const el = taskLogBoxRef.value;
  if (el) el.scrollTop = el.scrollHeight;
}

async function loadTaskLogs(silent = false) {
  if (!workspaceId.value) {
    taskLogs.value = [];
    return;
  }
  if (taskLogLoading.value) return;
  taskLogLoading.value = true;
  try {
    const r = await listWorkspaceTaskLogs(workspaceId.value, 120);
    taskLogs.value = r.items || [];
    await scrollTaskLogsToBottom();
  } catch (e) {
    if (!silent) ElMessage.error("加载空间任务日志失败: " + e.message);
  } finally {
    taskLogLoading.value = false;
  }
}

function stopTaskLogPolling() {
  if (taskLogTimer) {
    clearInterval(taskLogTimer);
    taskLogTimer = null;
  }
}

async function startTaskLogPolling() {
  stopTaskLogPolling();
  if (!workspaceId.value || !taskLogAutoRefresh.value) return;
  taskLogTimer = setInterval(() => {
    if (!workspaceId.value || !taskLogAutoRefresh.value) return;
    loadTaskLogs(true);
  }, 5000);
}

async function syncCurrentWorkspace() {
  if (!workspaceId.value) return ElMessage.warning("请选择母号空间");
  syncingWorkspace.value = true;
  try {
    await syncWorkspace(workspaceId.value);
    await loadSpaces();
    await load();
    window.dispatchEvent(new CustomEvent("workspace-master-updated", { detail: { id: workspaceId.value } }));
    ElMessage.success("席位统计已同步");
  } catch (e) {
    ElMessage.error("同步母号信息失败: " + e.message);
  } finally {
    syncingWorkspace.value = false;
  }
}

async function syncCurrentWorkspaceMembers() {
  if (!workspaceId.value) return ElMessage.warning("请选择母号空间");
  syncingWorkspaceMembers.value = true;
  try {
    const result = await syncWorkspaceMembers(workspaceId.value);
    await load();
    ElMessage.success(
      `成员席位同步完成：更新 ${result.refreshed || 0}，未匹配 ${result.missing || 0}，剩余未知 ${result.remaining || 0}`
    );
  } catch (e) {
    ElMessage.error(e.status === 429 ? "上游请求过于频繁，请稍后重试" : e.message);
  } finally {
    syncingWorkspaceMembers.value = false;
  }
}

async function loadStats() {
  if (!workspaceId.value) return;
  try {
    const res = await getCandidateStats(workspaceId.value);
    if (res.stats) {
      candidateStats.value = res.stats;
    }
  } catch (_) {
    // 忽略非关键统计错误
  }
}

async function load() {
  if (!workspaceId.value) return;
  loading.value = true;
  try {
    const a = await listCandidateOptions(workspaceId.value, {
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
      account_status: accountStatusFilter.value,
      join_status: joinStatusFilter.value,
      credential_status: credentialStatusFilter.value,
      seat_type: seatTypeFilter.value,
      trash_status: trashStatusFilter.value,
      tag_status: tagStatusFilter.value,
      group_name: groupNameFilter.value,
      keyword: searchKeyword.value || undefined,
    });
    options.value = a.items || [];
    total.value = Number(a.total || 0);
    if (a.stats) {
      candidateStats.value = a.stats;
    } else {
      loadStats();
    }
  } catch (e) {
    ElMessage.error(e.message);
  } finally {
    loading.value = false;
  }
}

async function loadCandidateGroups() {
  if (!workspaceId.value) {
    candidateGroups.value = [];
    return;
  }
  try {
    const r = await listCandidateGroups(workspaceId.value);
    candidateGroups.value = r.groups || [];
  } catch (_) {
    candidateGroups.value = [];
  }
}

async function loadSpaces() {
  try {
    const r = await listWorkspaceMasters({ limit: 200, offset: 0 });
    spaces.value = r.items || [];
    const requested = Number(route.query.workspace_id || 0);
    if (!workspaceId.value && spaces.value.length) {
      workspaceId.value = spaces.value.some((x) => x.id === requested) ? requested : spaces.value[0].id;
    }
  } catch (e) {
    ElMessage.error(e.message);
  }
}

function handleQuickTabChange(tab) {
  quickTab.value = tab;
  if (tab === "all") {
    trashStatusFilter.value = "active";
    tagStatusFilter.value = "";
    joinStatusFilter.value = "";
    credentialStatusFilter.value = "";
  } else if (tab === "pending") {
    trashStatusFilter.value = "active";
    tagStatusFilter.value = "";
    joinStatusFilter.value = "pending_invite";
    credentialStatusFilter.value = "";
  } else if (tab === "joined") {
    trashStatusFilter.value = "active";
    tagStatusFilter.value = "";
    joinStatusFilter.value = "joined";
    credentialStatusFilter.value = "";
  } else if (tab === "token") {
    trashStatusFilter.value = "active";
    tagStatusFilter.value = "";
    credentialStatusFilter.value = "workspace_credential";
    joinStatusFilter.value = "";
  } else if (tab === "outbound") {
    trashStatusFilter.value = "active";
    tagStatusFilter.value = "outbound";
    joinStatusFilter.value = "";
    credentialStatusFilter.value = "";
  } else if (tab === "trash") {
    trashStatusFilter.value = "trashed";
    tagStatusFilter.value = "";
    joinStatusFilter.value = "";
    credentialStatusFilter.value = "";
  }
}

function resetFilters() {
  accountStatusFilter.value = "";
  joinStatusFilter.value = "";
  credentialStatusFilter.value = "";
  seatTypeFilter.value = "";
  trashStatusFilter.value = "active";
  tagStatusFilter.value = "";
  groupNameFilter.value = "";
  searchKeyword.value = "";
  quickTab.value = "all";
}

async function remove() {
  const emails = selected.value.map((x) => x.email);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  setOperation(emails, "移除中…");
  try {
    await removeCandidates(workspaceId.value, emails);
    ElMessage.success("已移除候选划分");
    selected.value = [];
    await load();
  } catch (e) {
    ElMessage.error(e.message);
  } finally {
    clearOperation(emails);
  }
}

async function setOutboundStatus(tagStatus, label) {
  const rows = selected.value.filter((x) => isSelectableCandidate(x));
  if (!rows.length) return ElMessage.warning("请选择候选人");
  const emails = rows.map((x) => x.email);
  setOperation(emails, `${label}中…`);
  try {
    const r = await updateCandidateTagStatus(workspaceId.value, emails, tagStatus);
    ElMessage.success(`${label}完成：${r.changed || 0} 个`);
    selected.value = [];
    await load();
  } catch (e) {
    ElMessage.error(`${label}失败: ` + e.message);
  } finally {
    clearOperation(emails);
  }
}

async function moveToTrash() {
  const rows = selected.value.filter((x) => x.account_status !== "permanently_invalid" && x.trash_status !== "trashed");
  if (!rows.length) return ElMessage.warning("请选择未进入垃圾箱的候选人");
  const emails = rows.map((x) => x.email);
  setOperation(emails, "移入垃圾箱中…");
  try {
    const r = await trashCandidates(workspaceId.value, emails);
    ElMessage[r.failed ? "warning" : "success"](`已移入垃圾箱 ${r.trashed || 0} 个${r.failed ? `，失败 ${r.failed}` : ""}`);
    selected.value = [];
    await load();
  } catch (e) {
    ElMessage.error("移入垃圾箱失败: " + e.message);
  } finally {
    clearOperation(emails);
  }
}

async function restoreFromTrash() {
  const rows = selected.value.filter((x) => x.trash_status === "trashed");
  if (!rows.length) return ElMessage.warning("请选择垃圾箱中的候选人");
  const emails = rows.map((x) => x.email);
  setOperation(emails, "移出垃圾箱中…");
  try {
    const r = await restoreCandidatesFromTrash(workspaceId.value, emails);
    const skipped = Number(r.skipped || 0);
    ElMessage[skipped ? "warning" : "success"](`已移出垃圾箱 ${r.restored || 0} 个${skipped ? `，跳过 ${skipped}` : ""}`);
    selected.value = [];
    await load();
  } catch (e) {
    ElMessage.error("移出垃圾箱失败: " + e.message);
  } finally {
    clearOperation(emails);
  }
}

async function invite() {
  if (candidateMembershipBusy.value) return ElMessage.warning("空间加入或候选校验正在执行中");
  const emails = selected.value
    .filter((x) => x.assigned && x.account_status !== "permanently_invalid" && x.trash_status !== "trashed")
    .map((x) => x.email);
  if (!emails.length) return ElMessage.warning("请先将账号划分为当前空间的候选人");
  membershipTaskRunning.value = true;
  setOperation(emails, "邀请中…");
  try {
    const r = await inviteCandidates(workspaceId.value, emails, seatType.value);
    const states = Object.values(r.states || {});
    const confirmed = states.filter((x) => x !== "not_invited").length;
    const pending = states.filter((x) => x === "not_invited").length;
    const seatName = seatType.value === "default" ? "标准席位" : seatType.value === "prolite" ? "高级席位（ProLite）" : "Codex席位";
    if (r.recheck_error) {
      ElMessage.warning(`邀请已提交，但状态复查受上游限流影响，请稍后执行候选状态校验`);
    } else if (pending === 0) {
      ElMessage.success(`邀请完成并已复查状态（${seatName}）${r.invite_error ? "，上游请求超时但状态已确认" : ""}`);
    } else {
      ElMessage.warning(`邀请已复查：确认 ${confirmed}/${emails.length}${r.invite_error ? "（上游请求超时）" : ""}，仍有 ${pending} 个未确认`);
    }
    await load();
  } catch (e) {
    ElMessage.error(e.message);
  } finally {
    membershipTaskRunning.value = false;
    clearOperation(emails);
  }
}

async function join() {
  if (candidateMembershipBusy.value) return ElMessage.warning("空间加入或候选校验正在执行中");
  const emails = selected.value
    .filter((x) => x.assigned && x.account_status !== "permanently_invalid" && x.trash_status !== "trashed")
    .map((x) => x.email);
  if (!emails.length) return ElMessage.warning("请先选择当前空间的候选人");
  if (!proxyList.value.length) return ElMessage.warning("全局代理池为空，请先在代理池页面配置代理");
  membershipTaskRunning.value = true;
  setOperation(emails, "申请加入中…");
  try {
    const r = await requestJoin(
      workspaceId.value,
      emails,
      "",
      proxyList.value.join("\n"),
      seatType.value,
      { concurrency: taskConcurrency.value }
    );
    ElMessage.success(
      `申请完成（${seatType.value === "default" ? "标准席位" : seatType.value === "prolite" ? "高级席位（ProLite）" : "Codex席位"}）：成功 ${r.succeeded}，失败 ${r.failed}`
    );
    await load();
  } catch (e) {
    ElMessage.error(e.message);
  } finally {
    membershipTaskRunning.value = false;
    clearOperation(emails);
  }
}

async function check() {
  if (candidateMembershipBusy.value) return ElMessage.warning("空间加入或候选校验正在执行中");
  const emails = selected.value.filter((x) => x.account_status !== "permanently_invalid").map((x) => x.email);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  candidateCheckRunning.value = true;
  let succeeded = 0;
  let failed = 0;
  setOperation(emails, "排队中…");
  try {
    for (const email of emails) {
      setOneOperation(email, "校验中…");
      try {
        const result = await checkCandidates(workspaceId.value, [email]);
        const states = result.states || {};
        const seats = result.seats || {};
        options.value = options.value.map((row) => {
          const key = String(row.email || "").toLowerCase();
          if (key !== email.toLowerCase()) return row;
          const patch = {};
          const newJoinStatus = states[key];
          if (newJoinStatus) {
            patch.workspace_join_status = newJoinStatus;
            if (newJoinStatus === "joined") patch.display_status = row.has_workspace_access_token ? "workspace_credential" : "joined";
            else patch.display_status = newJoinStatus;
          }
          const seatInfo = seats[key];
          if (seatInfo) {
            if (seatInfo.raw_seat_type) { patch.seat_label = seatInfo.raw_seat_type; patch.seat_type = seatInfo.raw_seat_type; }
            if (seatInfo.member_id) patch.member_id = seatInfo.member_id;
            if (seatInfo.codex_seat != null) patch.codex_seat = seatInfo.codex_seat;
            if (seatInfo.gpt_seat != null) patch.gpt_seat = seatInfo.gpt_seat;
          }
          if (!Object.keys(patch).length) return row;
          return { ...row, ...patch };
        });
        succeeded += 1;
      } catch (e) {
        failed += 1;
      }
      clearOperation([email]);
    }
    if (failed) {
      ElMessage.warning(`候选状态校验完成：成功 ${succeeded}，失败 ${failed}`);
    } else {
      ElMessage.success(`候选状态校验完成：${succeeded} 个`);
    }
    await load();
  } catch (e) {
    ElMessage.error("候选状态校验失败: " + (e.message || e));
  } finally {
    candidateCheckRunning.value = false;
    clearOperation(emails);
  }
}

async function setInviteStatus(joinStatus, label) {
  const emails = selected.value.filter((x) => x.account_status !== "permanently_invalid" && x.trash_status !== "trashed").map((x) => x.email);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  setOperation(emails, `${label}中…`);
  try {
    const r = await setCandidateInviteStatus(workspaceId.value, emails, joinStatus);
    ElMessage.success(`已手动设置邀请状态：${r.changed || 0} 个`);
    await load();
  } catch (e) {
    ElMessage.error("手动设置邀请状态失败: " + e.message);
  } finally {
    clearOperation(emails);
  }
}

async function quota() {
  if (!selected.value.length) return ElMessage.warning("请选择候选人");
  const skippedRows = selected.value.filter((row) => quotaIneligibleReason(row));
  const emails = selected.value.filter((row) => !quotaIneligibleReason(row)).map((row) => row.email);
  if (!emails.length) return ElMessage.warning(`所选候选人不可查询额度：${quotaSkipSummary(skippedRows)}`);
  const quotaProxies = [...new Set(proxyList.value.map((value) => String(value || "").trim()).filter(Boolean))];
  if (!quotaProxies.length) return ElMessage.warning("全局代理池为空，无法查询候选额度");
  if (quotaTaskRunning.value) return ElMessage.warning("额度查询任务正在执行中");

  const concurrency = Math.min(Math.max(1, Number(taskConcurrency.value) || 1), 20);
  const workspace = workspaceId.value;
  const results = {};
  const quotaProxyUsage = quotaProxies.map(() => 0);
  const leaseQuotaProxy = () => {
    const minimum = Math.min(...quotaProxyUsage);
    const index = quotaProxyUsage.findIndex((count) => count === minimum);
    quotaProxyUsage[index] += 1;
    return quotaProxies[index];
  };

  quotaTaskRunning.value = true;
  quotaProgress.value = { done: 0, total: emails.length, active: 0, succeeded: 0, failed: 0, relogged: 0 };
  setOperation(emails, "排队中…");

  try {
    await runRollingPool(emails, concurrency, async (email) => {
      setOneOperation(email, reloginOn401.value ? "额度查询 / 401重登中…" : "额度查询中…");
      quotaProgress.value = { ...quotaProgress.value, active: quotaProgress.value.active + 1 };
      let result;
      try {
        const quotaProxy = leaseQuotaProxy();
        const response = await queryCandidateQuota(
          workspace,
          [email],
          reloginOn401.value,
          proxyList.value.join("\n"),
          autoPush.value,
          {
            concurrency,
            otp_timeout: taskOtpTimeout.value,
            account_retry_count: taskRetry.value,
            cool_down_seconds: taskCooldown.value,
            quota_network_retries: quotaNetworkRetries.value,
            quota_proxy_pool: quotaProxyPool.value,
            quota_proxy: quotaProxy,
          }
        );
        result = response.results?.[email.toLowerCase()] || Object.values(response.results || {})[0];
        if (!result) result = { ok: false, error: "服务器未返回该账号的查询结果" };
      } catch (e) {
        result = { ok: false, error: e.message || "额度查询失败" };
      }
      results[email] = result;

      const now = Date.now() / 1000;
      options.value = options.value.map((row) => {
        if (String(row.email || "").toLowerCase() !== email.toLowerCase()) return row;
        const patch = {};
        if (result.quota) {
          patch.quota_json = JSON.stringify(result.quota);
          if (String(row.display_status || "").startsWith("quota_error_")) {
            patch.display_status = row.has_workspace_access_token ? "workspace_credential" : row.display_status;
          }
        } else {
          const errorText = String(result.relogin_error || result.error || "额度查询失败");
          const errorCode = errorText.match(/(?:HTTP\s*)?(401|403|\d{3})/i)?.[1] || "error";
          patch.quota_json = JSON.stringify({ error_code: errorCode, error: errorText, updated_at: now });
          patch.display_status = `quota_error_${errorCode}`;
        }
        if (result.trash_scheduled) {
          patch.trash_status = "scheduled";
          patch.trash_due_at = result.trash_due_at || 0;
          patch.display_status = "trash_scheduled";
        }
        if (result.trashed) {
          patch.trash_status = "trashed";
          patch.display_status = "trashed";
        }
        return { ...row, ...patch };
      });
      clearOperation([email]);
      quotaProgress.value = {
        ...quotaProgress.value,
        done: quotaProgress.value.done + 1,
        active: Math.max(0, quotaProgress.value.active - 1),
        succeeded: quotaProgress.value.succeeded + (result.ok ? 1 : 0),
        failed: quotaProgress.value.failed + (result.ok ? 0 : 1),
        relogged: quotaProgress.value.relogged + (result.relogin_started ? 1 : 0),
      };
    });

    const { succeeded, failed, relogged } = quotaProgress.value;
    const skippedText = skippedRows.length ? `，跳过 ${skippedRows.length} 个（${quotaSkipSummary(skippedRows)}）` : "";
    const reloginErrors = Object.values(results).filter((x) => x?.relogin_error).map((x) => x.relogin_error);
    if (reloginErrors.length) {
      ElMessage.warning(`额度查询完成：成功 ${succeeded}，失败 ${failed}${skippedText}；${reloginErrors.slice(0, 3).join("；")}`);
    } else {
      ElMessage[succeeded ? "success" : "warning"](`额度查询完成：成功 ${succeeded}/${emails.length}${relogged ? `，401重登录成功 ${relogged}` : ""}${skippedText}`);
    }
    await load();
  } catch (e) {
    ElMessage.error("额度查询任务失败: " + (e.message || e));
  } finally {
    quotaTaskRunning.value = false;
    clearOperation(emails);
  }
}

async function changeSeat(targetSeat = seatType.value) {
  const target = String(targetSeat || "").trim().toLowerCase().replace(/-/g, "_");
  if (!["default", "usage_based", "prolite"].includes(target)) {
    return ElMessage.warning("请选择目标席位");
  }
  if (seatSwitchRunning.value) return;
  const candidates = selected.value.filter((x) => x.workspace_join_status === "joined");
  if (!candidates.length) return ElMessage.warning("请选择已加入当前空间的候选人");
  const emails = candidates.map((x) => x.email);
  const targetLabel = target === "default" ? "标准席位" : target === "prolite" ? "高级席位（ProLite）" : "Codex席位";
  let succeeded = 0;
  let skipped = 0;
  let failed = 0;
  seatSwitchRunning.value = true;

  const canonicalSeat = (v) => {
    const s = String(v || "").trim().toLowerCase().replace(/-/g, "_");
    if (["usage_based", "usagebased", "codex席位"].includes(s)) return "usage_based";
    if (["default", "standard", "standard_seat", "gpt席位", "标准席位"].includes(s)) return "default";
    if (["prolite", "pro_lite", "advanced", "advanced_seat", "premium", "premium_seat", "pro", "高级", "高级席位"].includes(s)) return "prolite";
    return s;
  };

  setOperation(emails, "排队中…");
  try {
    let requestCount = 0;
    for (const email of emails) {
      const localRow = options.value.find((r) => String(r.email || "").toLowerCase() === email.toLowerCase());
      const localSeat = canonicalSeat(localRow?.seat_label || localRow?.seat_type);
      if (["default", "usage_based", "prolite"].includes(localSeat) && localSeat === target) {
        skipped += 1;
        clearOperation([email]);
        continue;
      }
      if (requestCount > 0) {
        await new Promise((r) => setTimeout(r, 3000));
      }
      setOneOperation(email, `切换为${targetLabel}中…`);
      try {
        const r = await updateCandidateSeat(workspaceId.value, [email], target);
        const item = (r.results || [])[0] || {};
        if (item.skipped) {
          skipped += 1;
        } else if (item.ok) {
          succeeded += 1;
          options.value = options.value.map((row) => {
            if (String(row.email || "").toLowerCase() !== email.toLowerCase()) return row;
            return { ...row, seat_label: target, seat_type: target };
          });
        } else {
          failed += 1;
        }
      } catch (e) {
        failed += 1;
      }
      requestCount += 1;
      clearOperation([email]);
    }
    ElMessage[failed ? "warning" : "success"](
      `席位切换完成（${targetLabel}）：已切换 ${succeeded}，跳过 ${skipped}，失败 ${failed}`
    );
    await load();
  } catch (e) {
    ElMessage.error("席位切换失败: " + e.message);
  } finally {
    seatSwitchRunning.value = false;
    clearOperation(emails);
  }
}

async function runCandidateAction(command) {
  if (command === "check") return check();
  if (command === "quota") return quota();
  if (command === "credentials") return credentials();
  if (command === "login_only") return loginOnly();
  if (command === "select_full_quota") return selectFullQuotaCandidates();
  if (command === "select_quota_401") return selectQuota401Candidates();
  if (command === "trash") return moveToTrash();
  if (command === "restore_trash") return restoreFromTrash();
}

async function selectCandidateRows(predicate, emptyMessage, successMessage) {
  const rows = options.value.filter(
    (row) => predicate(row) && row.account_status !== "permanently_invalid" && row.trash_status !== "trashed"
  );
  const table = candidateTableRef.value;
  if (!table) return ElMessage.warning("候选列表尚未加载完成");
  table.clearSelection();
  await nextTick();
  rows.forEach((row) => table.toggleRowSelection(row, true));
  selected.value = rows;
  if (!rows.length) {
    ElMessage.warning(emptyMessage);
  } else {
    ElMessage.success(`${successMessage}：${rows.length} 个`);
  }
}

async function selectFullQuotaCandidates() {
  return selectCandidateRows(isFullQuotaRow, "当前页没有额度为 100% 的候选人", "已选取当前页额度为 100% 的候选人");
}

async function selectQuota401Candidates() {
  return selectCandidateRows(isQuota401Row, "当前页没有额度查询 401 的候选人", "已选取当前页额度查询 401 的候选人");
}

async function runInviteStatusAction(command) {
  if (command === "manual_pending_invite") return setInviteStatus("pending_invite", "标记待接受邀请");
  if (command === "manual_joined") return setInviteStatus("joined", "标记已加入");
}

async function runMembershipAction(command) {
  if (command === "join") return join();
  if (command === "invite") return invite();
}

async function runExportAction(command) {
  if (command === "push") return push();
  if (command === "export_outbound") return exportAndOutbound();
  return doExport(command);
}

async function runAssignAction(command) {
  if (command === "remove") return remove();
  if (command === "trash") return moveToTrash();
  if (command === "restore_trash") return restoreFromTrash();
  if (command === "outbound") return setOutboundStatus("outbound", "标记出库");
  if (command === "restore_outbound") return setOutboundStatus("active", "恢复出库账号");
}

async function saveSpaceSettings(targetId = workspaceId.value) {
  if (!targetId || !settingsReady.value || settingsWorkspaceId.value !== targetId || workspaceId.value !== targetId) return;
  try {
    await saveCandidateSettings({
      workspace_id: targetId,
      interval_minutes: quotaInterval.value,
      relogin_on_401: reloginOn401.value,
      proxy_pool: proxyList.value.join("\n"),
      auto_push: autoPush.value,
      concurrency: taskConcurrency.value,
      otp_timeout: taskOtpTimeout.value,
      account_retry_count: taskRetry.value,
      cool_down_seconds: taskCooldown.value,
      quota_network_retries: quotaNetworkRetries.value,
      quota_proxy_pool: quotaProxyPool.value,
      trash_enabled: trashEnabled.value,
      trash_invalid_enabled: trashInvalidEnabled.value,
      trash_zero_delay_minutes: trashZeroDelayMinutes.value,
      seat_protect_enabled: seatProtectEnabled.value,
      seat_protect_threshold: seatProtectThreshold.value,
      seat_protect_refresh_time: seatProtectRefreshTime.value,
      prolite_seat_protect_enabled: proliteSeatProtectEnabled.value,
      prolite_seat_protect_threshold: proliteSeatProtectThreshold.value,
      prolite_seat_protect_refresh_time: proliteSeatProtectRefreshTime.value,
      auto_standard_seat_enabled: autoStandardSeatEnabled.value,
      auto_prolite_seat_enabled: autoProliteSeatEnabled.value,
      auto_seat_interval_minutes: autoSeatIntervalMinutes.value,
      auto_prolite_candidate_seat_type: autoProliteCandidateSeatType.value,
    });
    // 同步更新统计数据并刷新统计
    await loadStats();
  } catch (e) {
    ElMessage.error("空间设置保存失败: " + e.message);
  }
}

function queueSpaceSettingsSave() {
  const targetId = workspaceId.value;
  if (!targetId || !settingsReady.value || settingsWorkspaceId.value !== targetId) return;
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = setTimeout(() => {
    settingsSaveTimer = null;
    saveSpaceSettings(targetId);
  }, 250);
}

async function loadSpaceSettings(targetId = workspaceId.value) {
  if (!targetId) return;
  const generation = ++settingsLoadGeneration;
  settingsReady.value = false;
  settingsWorkspaceId.value = null;
  let loaded = false;
  try {
    const st = await quotaScheduleStatus(targetId);
    if (generation !== settingsLoadGeneration || workspaceId.value !== targetId) return;
    const c = st.settings || {};
    quotaRunning.value = Boolean(st.running);
    nextQuotaAt.value = st.next_at || 0;
    quotaInterval.value = Number(c.interval_minutes || st.interval_minutes || 30);
    reloginOn401.value = Boolean(c.relogin_on_401);
    autoPush.value = Boolean(c.auto_push);
    taskConcurrency.value = Number(c.concurrency || 1);
    taskOtpTimeout.value = Number(c.otp_timeout || 180);
    taskRetry.value = Number(c.account_retry_count || 1);
    taskCooldown.value = Number(c.cool_down_seconds || 0);
    quotaNetworkRetries.value = Number(c.quota_network_retries ?? 2);
    quotaProxyPool.value = String(c.quota_proxy_pool || "");
    trashEnabled.value = c.trash_enabled !== false;
    trashInvalidEnabled.value = c.trash_invalid_enabled !== false;
    trashZeroDelayMinutes.value = Number(c.trash_zero_delay_minutes || 60);
    seatProtectEnabled.value = Boolean(c.seat_protect_enabled);
    seatProtectThreshold.value = Number(c.seat_protect_threshold || 8);
    seatProtectRefreshTime.value = String(c.seat_protect_refresh_time || "00:00");
    seatProtectUsedCount.value = Number(c.seat_protect_used_count || 0);
    proliteSeatProtectEnabled.value = Boolean(c.prolite_seat_protect_enabled);
    proliteSeatProtectThreshold.value = Number(c.prolite_seat_protect_threshold || 8);
    proliteSeatProtectRefreshTime.value = String(c.prolite_seat_protect_refresh_time || "00:00");
    proliteSeatProtectUsedCount.value = Number(c.prolite_seat_protect_used_count || 0);
    autoStandardSeatEnabled.value = Boolean(c.auto_standard_seat_enabled);
    autoProliteSeatEnabled.value = Boolean(c.auto_prolite_seat_enabled);
    autoSeatIntervalMinutes.value = Math.min(1440, Math.max(1, Number(c.auto_seat_interval_minutes) || 5));
    autoProliteCandidateSeatType.value = ["default", "usage_based", "all"].includes(String(c.auto_prolite_candidate_seat_type || "default"))
      ? String(c.auto_prolite_candidate_seat_type || "default")
      : "default";
    try {
      const seatStatus = await autoStandardSeatScheduleStatus(targetId);
      autoStandardSeatNextAt.value = seatStatus.next_at || 0;
      autoStandardSeatEnabled.value = Boolean((seatStatus.settings || {}).auto_standard_seat_enabled);
    } catch (_) {}
    try {
      const seatStatus = await autoProliteSeatScheduleStatus(targetId);
      autoProliteSeatNextAt.value = seatStatus.next_at || 0;
      autoProliteSeatEnabled.value = Boolean((seatStatus.settings || {}).auto_prolite_seat_enabled);
    } catch (_) {}
    settingsWorkspaceId.value = targetId;
    loaded = true;
  } catch (_) {
    if (generation === settingsLoadGeneration && workspaceId.value === targetId) {
      quotaRunning.value = false;
      nextQuotaAt.value = 0;
      quotaInterval.value = 30;
      reloginOn401.value = false;
      autoPush.value = false;
      taskConcurrency.value = 1;
      taskOtpTimeout.value = 180;
      taskRetry.value = 1;
      taskCooldown.value = 0;
      quotaNetworkRetries.value = 2;
      quotaProxyPool.value = "";
      trashEnabled.value = true;
      trashInvalidEnabled.value = true;
      trashZeroDelayMinutes.value = 60;
      seatProtectEnabled.value = false;
      seatProtectThreshold.value = 8;
      seatProtectRefreshTime.value = "00:00";
      seatProtectUsedCount.value = 0;
      proliteSeatProtectEnabled.value = false;
      proliteSeatProtectThreshold.value = 8;
      proliteSeatProtectRefreshTime.value = "00:00";
      proliteSeatProtectUsedCount.value = 0;
      autoStandardSeatEnabled.value = false;
      autoStandardSeatNextAt.value = 0;
      autoProliteSeatEnabled.value = false;
      autoProliteSeatNextAt.value = 0;
      autoSeatIntervalMinutes.value = 5;
      autoProliteCandidateSeatType.value = "default";
    }
  } finally {
    if (generation === settingsLoadGeneration && workspaceId.value === targetId) settingsReady.value = loaded;
  }
}

async function toggleQuotaSchedule() {
  try {
    if (!quotaRunning.value) {
      await stopQuotaSchedule(workspaceId.value);
      nextQuotaAt.value = 0;
      ElMessage.success("已停止定时额度查询");
    } else {
      const r = await startQuotaSchedule(
        workspaceId.value,
        quotaInterval.value,
        reloginOn401.value,
        proxyList.value.join("\n"),
        autoPush.value,
        {
          concurrency: taskConcurrency.value,
          otp_timeout: taskOtpTimeout.value,
          account_retry_count: taskRetry.value,
          cool_down_seconds: taskCooldown.value,
          quota_network_retries: quotaNetworkRetries.value,
          quota_proxy_pool: quotaProxyPool.value,
          trash_enabled: trashEnabled.value,
          trash_invalid_enabled: trashInvalidEnabled.value,
          trash_zero_delay_minutes: trashZeroDelayMinutes.value,
          seat_protect_enabled: seatProtectEnabled.value,
          seat_protect_threshold: seatProtectThreshold.value,
          seat_protect_refresh_time: seatProtectRefreshTime.value,
          prolite_seat_protect_enabled: proliteSeatProtectEnabled.value,
          prolite_seat_protect_threshold: proliteSeatProtectThreshold.value,
          prolite_seat_protect_refresh_time: proliteSeatProtectRefreshTime.value,
          auto_standard_seat_enabled: autoStandardSeatEnabled.value,
          auto_prolite_seat_enabled: autoProliteSeatEnabled.value,
          auto_seat_interval_minutes: autoSeatIntervalMinutes.value,
          auto_prolite_candidate_seat_type: autoProliteCandidateSeatType.value,
        }
      );
      nextQuotaAt.value = r.next_at || Date.now() / 1000 + quotaInterval.value * 60;
      ElMessage.success(`已启动定时额度查询（每 ${quotaInterval.value} 分钟）`);
    }
  } catch (e) {
    quotaRunning.value = !quotaRunning.value;
    ElMessage.error(e.message);
  }
}

async function toggleAutoStandardSeat() {
  try {
    if (!autoStandardSeatEnabled.value) {
      await stopAutoStandardSeatSchedule(workspaceId.value);
      autoStandardSeatNextAt.value = 0;
      ElMessage.success("已停止自动补齐标准席位");
    } else {
      const r = await startAutoStandardSeatSchedule(workspaceId.value);
      autoStandardSeatNextAt.value = r.next_at || Date.now() / 1000 + autoSeatIntervalMinutes.value * 60;
      ElMessage.success(`已启动自动补齐标准席位（每 ${autoSeatIntervalMinutes.value} 分钟轮询）`);
    }
  } catch (e) {
    autoStandardSeatEnabled.value = !autoStandardSeatEnabled.value;
    ElMessage.error(e.message);
  }
}

async function toggleAutoProliteSeat() {
  try {
    if (!autoProliteSeatEnabled.value) {
      await stopAutoProliteSeatSchedule(workspaceId.value);
      autoProliteSeatNextAt.value = 0;
      ElMessage.success("已停止自动补齐高级席位");
    } else {
      const r = await startAutoProliteSeatSchedule(workspaceId.value);
      autoProliteSeatNextAt.value = r.next_at || Date.now() / 1000 + autoSeatIntervalMinutes.value * 60;
      ElMessage.success(`已启动自动补齐高级席位（每 ${autoSeatIntervalMinutes.value} 分钟轮询）`);
    }
  } catch (e) {
    autoProliteSeatEnabled.value = !autoProliteSeatEnabled.value;
    ElMessage.error(e.message);
  }
}

async function credentials() {
  const emails = activeSelectedEmails();
  if (!emails.length) return ElMessage.warning("请选择候选人");
  if (!proxyList.value.length) return ElMessage.warning("全局代理池为空");
  setOperation(emails, "凭证获取中…");
  try {
    const result = await fetchWorkspaceCredentials(
      workspaceId.value,
      emails,
      proxyList.value.join("\n"),
      seatType.value,
      autoPush.value,
      {
        concurrency: taskConcurrency.value,
        otp_timeout: taskOtpTimeout.value,
        account_retry_count: taskRetry.value,
        cool_down_seconds: taskCooldown.value,
        quota_network_retries: quotaNetworkRetries.value,
        quota_proxy_pool: quotaProxyPool.value,
      }
    );
    const skipped = result.skipped || 0;
    const eligible = result.eligible || 0;
    if (skipped) ElMessage.warning(`空间凭证任务：跳过 ${skipped} 个不满足条件的候选人，已提交 ${eligible} 个（待接受邀请的成员会在登录时自动接受邀请）`);
    else ElMessage.success(`空间凭证获取任务已启动：已提交 ${eligible} 个，请在运行记录查看`);
  } catch (e) {
    ElMessage.error(e.message);
  } finally {
    clearOperation(emails);
  }
}

async function loginOnly() {
  const emails = activeSelectedEmails();
  if (!emails.length) return ElMessage.warning("请选择候选人");
  if (!proxyList.value.length) return ElMessage.warning("全局代理池为空");
  setOperation(emails, "仅登录中…");
  try {
    const result = await loginOnlyWorkspace(
      workspaceId.value,
      emails,
      proxyList.value.join("\n"),
      seatType.value,
      {
        concurrency: taskConcurrency.value,
        otp_timeout: taskOtpTimeout.value,
        account_retry_count: taskRetry.value,
        cool_down_seconds: taskCooldown.value,
        quota_network_retries: quotaNetworkRetries.value,
        quota_proxy_pool: quotaProxyPool.value,
      }
    );
    const skipped = result.skipped || 0;
    const eligible = result.eligible || 0;
    if (skipped) ElMessage.warning(`仅登录任务：跳过 ${skipped} 个不满足条件的候选人，已提交 ${eligible} 个`);
    else ElMessage.success(`仅登录任务已启动：已提交 ${eligible} 个（跳过 OAuth），请在运行记录查看`);
  } catch (e) {
    ElMessage.error(e.message);
  } finally {
    clearOperation(emails);
  }
}

async function loadExportFormats() {
  if (exportFormats.value.length) return;
  try {
    const r = await listExportFormats();
    exportFormats.value = r.formats || [];
  } catch (e) {
    ElMessage.error("加载导出格式失败: " + e.message);
  }
}

function b64ToBytes(b64) {
  const bin = atob(b64 || "");
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function saveBlob(data, filename, mime) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime || "application/octet-stream" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

async function doExport(fmt, exportOptions = {}) {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  exporting.value = true;
  try {
    const credentialFormat = fmt.id === "cpa" || fmt.id === "sub2api";
    const r = await exportRegistered({
      format: fmt.id,
      emails,
      workspace_id: workspaceId.value,
      proxy_pool: proxyList.value.join("\n"),
      ...(credentialFormat ? { encrypt_credentials: encryptCredentials.value } : {}),
      ...(exportOptions.encryptCredentials !== undefined ? { encrypt_credentials: Boolean(exportOptions.encryptCredentials) } : {}),
      ...(exportOptions.markOutbound ? { mark_outbound: true } : {}),
    });
    if (r.mode === "download") {
      saveBlob(b64ToBytes(r.b64), r.filename, r.mime);
      ElMessage.success(`已下载 ${r.filename}（${r.count || emails.length} 个号）`);
      return r;
    }
    exportText.value = r.text || "";
    exportFilename.value = r.filename || "export.txt";
    exportLabel.value = r.label || fmt.label;
    exportCount.value = r.count || emails.length;
    exportVisible.value = true;
    return r;
  } catch (e) {
    ElMessage.error("导出失败: " + e.message);
  } finally {
    exporting.value = false;
  }
}

async function exportAndOutbound() {
  const rows = selected.value.filter(
    (row) =>
      String(row?.tag_status || "active") !== "outbound" &&
      row?.trash_status !== "trashed" &&
      row?.account_status !== "permanently_invalid"
  );
  if (!rows.length) return ElMessage.warning("请选择正常候选人");
  if (!workspaceId.value) return ElMessage.warning("请选择母号空间");
  try {
    await ElMessageBox.confirm(
      `将 ${rows.length} 个候选人加密导出为 Sub2 文件，并标记为已出库。导出后可在 401 页面使用 Workspace ID 解密密码和 2FA，确定继续吗？`,
      "出库并导出",
      { type: "warning", confirmButtonText: "加密导出并出库", cancelButtonText: "取消" }
    );
  } catch (_) {
    return;
  }
  const previousSelection = selected.value;
  selected.value = rows;
  let succeeded = false;
  try {
    const result = await doExport({ id: "sub2api", label: "加密 Sub2API" }, { encryptCredentials: true, markOutbound: true });
    if (result?.outbound_marked !== undefined) {
      succeeded = true;
      selected.value = [];
      await load();
      ElMessage.success(`加密导出完成，已标记出库 ${result.outbound_marked} 个`);
    }
  } finally {
    if (!succeeded) selected.value = previousSelection;
  }
}

async function push() {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  const missing = selected.value.filter((x) => !x.has_access_token).length;
  if (missing) ElMessage.warning(`${missing} 个候选人缺少空间凭证，推送接口可能跳过`);
  pushing.value = true;
  setOperation(emails, "推送中…");
  try {
    const r = await pushRegisteredToCpa(emails, proxyList.value.join("\n"), workspaceId.value);
    ElMessage.success(r.message || `推送完成（${emails.length} 个）`);
  } catch (e) {
    ElMessage.error("推送失败: " + e.message);
  } finally {
    clearOperation(emails);
    pushing.value = false;
  }
}

watch(workspaceId, async (id) => {
  settingsLoadGeneration += 1;
  settingsReady.value = false;
  settingsWorkspaceId.value = null;
  stopTaskLogPolling();
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = null;
  taskLogs.value = [];
  if (!id) return;
  await load();
  await loadCandidateGroups();
  await loadSpaceSettings(id);
  await loadTaskLogs(true);
  await startTaskLogPolling();
});

watch(
  [
    quotaInterval,
    reloginOn401,
    autoPush,
    taskConcurrency,
    taskOtpTimeout,
    taskRetry,
    taskCooldown,
    quotaNetworkRetries,
    quotaProxyPool,
    trashEnabled,
    trashInvalidEnabled,
    trashZeroDelayMinutes,
    seatProtectEnabled,
    seatProtectThreshold,
    seatProtectRefreshTime,
    proliteSeatProtectEnabled,
    proliteSeatProtectThreshold,
    proliteSeatProtectRefreshTime,
    autoSeatIntervalMinutes,
  ],
  queueSpaceSettingsSave
);

watch(autoSeatIntervalMinutes, (value) => {
  const minutes = Math.min(1440, Math.max(1, Number(value) || 5));
  if (value !== minutes) autoSeatIntervalMinutes.value = minutes;
  if (autoStandardSeatEnabled.value) autoStandardSeatNextAt.value = Date.now() / 1000 + minutes * 60;
  if (autoProliteSeatEnabled.value) autoProliteSeatNextAt.value = Date.now() / 1000 + minutes * 60;
});

watch([autoStandardSeatEnabled, autoProliteSeatEnabled, autoProliteCandidateSeatType], queueSpaceSettingsSave);

watch(
  [accountStatusFilter, joinStatusFilter, credentialStatusFilter, seatTypeFilter, trashStatusFilter, tagStatusFilter, groupNameFilter, searchKeyword],
  () => {
    page.value = 1;
    selected.value = [];
    if (workspaceId.value) load();
  }
);

watch([page, pageSize], () => {
  selected.value = [];
  if (workspaceId.value) load();
});

watch(plainCredentialMode, (value) => {
  try {
    localStorage.setItem(PLAIN_CREDENTIAL_MODE_STORAGE_KEY, value ? "1" : "0");
  } catch (_) {}
});

watch(taskLogAutoRefresh, async () => {
  if (!workspaceId.value) return;
  await loadTaskLogs(true);
  await startTaskLogPolling();
});

onActivated(async () => {
  if (!credentialModeLoaded) {
    try {
      plainCredentialMode.value = localStorage.getItem(PLAIN_CREDENTIAL_MODE_STORAGE_KEY) === "1";
    } catch (_) {}
    credentialModeLoaded = true;
  }
  await loadSpaces();
  if (workspaceId.value) {
    await load();
    await loadCandidateGroups();
    await loadSpaceSettings(workspaceId.value);
    await loadTaskLogs(true);
    await startTaskLogPolling();
  }
});

onDeactivated(() => {
  stopTaskLogPolling();
});

onBeforeUnmount(() => {
  stopTaskLogPolling();
  clearTimeout(settingsSaveTimer);
});
</script>

<template>
  <div class="candidate-page">
    <!-- 顶部 Hero 卡片：母号空间概览与席位 KPI -->
    <div class="hero-card">
      <div class="hero-top-row">
        <div class="hero-selector-wrap">
          <div class="space-select-label">
            <Icon icon="lucide:building-2" class="space-icon" />
            <span>母号空间</span>
          </div>
          <el-select
            v-model="workspaceId"
            class="space-select"
            placeholder="选择母号空间"
            filterable
          >
            <el-option
              v-for="s in spaces"
              :key="s.id"
              :value="s.id"
              :label="`${s.account} · ${s.workspace_id || '无空间ID'}`"
            >
              <div class="space-option-item">
                <span class="space-option-account">{{ s.account }}</span>
                <el-tag size="small" type="info" effect="plain">{{ s.workspace_id || '未提取' }}</el-tag>
              </div>
            </el-option>
          </el-select>
        </div>

        <div class="hero-actions">
          <el-button
            type="primary"
            plain
            size="small"
            :loading="syncingWorkspace"
            :disabled="syncingWorkspaceMembers"
            @click="syncCurrentWorkspace"
          >
            <Icon icon="lucide:refresh-cw" class="btn-icon" />
            同步席位统计
          </el-button>

          <el-button
            type="primary"
            plain
            size="small"
            :loading="syncingWorkspaceMembers"
            :disabled="syncingWorkspace"
            @click="syncCurrentWorkspaceMembers"
          >
            <Icon icon="lucide:users" class="btn-icon" />
            同步成员席位
          </el-button>

          <el-button
            type="info"
            plain
            size="small"
            class="settings-trigger-btn"
            @click="settingsVisible = true"
          >
            <Icon icon="lucide:settings-2" class="btn-icon" />
            空间任务设置
            <span v-if="quotaRunning || autoStandardSeatEnabled || autoProliteSeatEnabled" class="active-badge" />
          </el-button>
        </div>
      </div>

      <!-- 席位 KPI 数据面板 -->
      <div v-if="currentWorkspace" class="hero-kpi-grid">
        <!-- 标准席位 KPI -->
        <div class="kpi-card standard-kpi">
          <div class="kpi-header">
            <div class="kpi-title-wrap">
              <span class="kpi-dot dot-primary" />
              <span class="kpi-title">标准席位</span>
            </div>
            <el-tag v-if="autoStandardSeatEnabled" size="small" type="success" effect="plain" class="kpi-tag">
              自动补齐中
            </el-tag>
          </div>
          <div class="kpi-body">
            <div class="kpi-value-row">
              <span class="kpi-main-val">{{ currentWorkspace.seats_default ?? 0 }}</span>
              <span class="kpi-sub-val">/ {{ currentWorkspace.seats_default_entitled ?? 0 }} 席</span>
            </div>
            <div class="kpi-bar-track">
              <div
                class="kpi-bar-fill fill-primary"
                :style="{
                  width: `${Math.min(100, Math.round(((currentWorkspace.seats_default || 0) / Math.max(1, currentWorkspace.seats_default_entitled || 1)) * 100))}%`
                }"
              />
            </div>
          </div>
          <div class="kpi-footer">
            <span>占用率 {{ Math.round(((currentWorkspace.seats_default || 0) / Math.max(1, currentWorkspace.seats_default_entitled || 1)) * 100) }}% · 累计补齐 {{ candidateStats.seat_fulfillment?.standard?.fulfilled_total || 0 }} 席</span>
            <span v-if="seatProtectEnabled" class="kpi-protect-badge" title="今日席位保护消耗 / 阈值">
              保护消耗: {{ seatProtectUsedCount }}/{{ seatProtectThreshold }}
            </span>
          </div>
        </div>

        <!-- 高级席位 (ProLite) KPI -->
        <div class="kpi-card prolite-kpi">
          <div class="kpi-header">
            <div class="kpi-title-wrap">
              <span class="kpi-dot dot-warning" />
              <span class="kpi-title">高级席位 (ProLite)</span>
            </div>
            <el-tag v-if="autoProliteSeatEnabled" size="small" type="warning" effect="plain" class="kpi-tag">
              自动升级中
            </el-tag>
          </div>
          <div class="kpi-body">
            <div class="kpi-value-row">
              <span class="kpi-main-val">{{ currentWorkspace.seats_prolite ?? 0 }}</span>
              <span class="kpi-sub-val">/ {{ currentWorkspace.seats_prolite_entitled ?? 0 }} 席</span>
            </div>
            <div class="kpi-bar-track">
              <div
                class="kpi-bar-fill fill-warning"
                :style="{
                  width: `${Math.min(100, Math.round(((currentWorkspace.seats_prolite || 0) / Math.max(1, currentWorkspace.seats_prolite_entitled || 1)) * 100))}%`
                }"
              />
            </div>
          </div>
          <div class="kpi-footer">
            <span>占用率 {{ Math.round(((currentWorkspace.seats_prolite || 0) / Math.max(1, currentWorkspace.seats_prolite_entitled || 1)) * 100) }}% · 累计补齐 {{ candidateStats.seat_fulfillment?.prolite?.fulfilled_total || 0 }} 席</span>
            <span v-if="proliteSeatProtectEnabled" class="kpi-protect-badge" title="今日高级席位保护消耗 / 阈值">
              保护消耗: {{ proliteSeatProtectUsedCount }}/{{ proliteSeatProtectThreshold }}
            </span>
          </div>
        </div>

        <!-- 垃圾回收与候选状态 KPI -->
        <div class="kpi-card trash-kpi">
          <div class="kpi-header">
            <div class="kpi-title-wrap">
              <span class="kpi-dot dot-danger" />
              <span class="kpi-title">垃圾回收与异常</span>
            </div>
            <el-tag v-if="candidateStats.trash?.due_scheduled_count" size="small" type="danger" effect="dark" class="kpi-tag">
              {{ candidateStats.trash.due_scheduled_count }} 到期待清理
            </el-tag>
          </div>
          <div class="kpi-body">
            <div class="kpi-value-row">
              <span class="kpi-main-val text-danger">{{ candidateStats.trash?.trashed_count || 0 }}</span>
              <span class="kpi-sub-val">已在垃圾箱</span>
            </div>
            <div class="kpi-stat-subtext">
              延迟回收 {{ candidateStats.trash?.scheduled_count || 0 }} · 失效待入箱 {{ candidateStats.trash?.invalid_pending_trash_count || 0 }}
            </div>
          </div>
          <div class="kpi-footer">
            <span>候选总数 {{ candidateStats.total_candidates || total }} · 出库 {{ candidateStats.seat_fulfillment?.outbound_count || 0 }}</span>
          </div>
        </div>

        <!-- Codex 席位 KPI -->
        <div class="kpi-card codex-kpi">
          <div class="kpi-header">
            <div class="kpi-title-wrap">
              <span class="kpi-dot dot-info" />
              <span class="kpi-title">Codex 席位</span>
            </div>
          </div>
          <div class="kpi-body">
            <div class="kpi-value-row">
              <span class="kpi-main-val">{{ currentWorkspace.seats_usage_based ?? 0 }}</span>
              <span class="kpi-sub-val">在用</span>
            </div>
            <div class="kpi-stat-subtext">
              已购总席位 {{ currentWorkspace.seats_entitled ?? '-' }} · 在用合计 {{ currentWorkspace.seats_in_use ?? '-' }}
            </div>
          </div>
          <div class="kpi-footer">
            <span>按量计费与使用</span>
          </div>
        </div>

        <!-- 空间状态与费用 KPI -->
        <div class="kpi-card meta-kpi">
          <div class="kpi-header">
            <div class="kpi-title-wrap">
              <span class="kpi-dot dot-success" />
              <span class="kpi-title">母号信息</span>
            </div>
            <button
              v-if="currentWorkspace.workspace_id"
              class="copy-chip-btn"
              title="点击复制 Workspace ID"
              @click="copyText(currentWorkspace.workspace_id)"
            >
              <Icon icon="lucide:copy" class="btn-icon-xs" />
              <span>ID</span>
            </button>
          </div>
          <div class="kpi-body meta-body">
            <div class="meta-item">
              <span class="meta-label">空间费用:</span>
              <span class="meta-val highlight-val">{{ currentWorkspace.seat_cost || '未同步' }}</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">续费日期:</span>
              <span class="meta-val">{{ cst(currentWorkspace.renewal_date) }}</span>
            </div>
          </div>
          <div class="kpi-footer">
            <span class="mono-sub">{{ currentWorkspace.account }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 主工作区：过滤器 + 批量操作栏 + 表格 -->
    <el-card shadow="never" class="main-card">
      <!-- 快捷分段视图 Tabs -->
      <div class="view-tabs-row">
        <div class="quick-tabs">
          <button
            class="tab-chip"
            :class="{ active: quickTab === 'all' }"
            @click="handleQuickTabChange('all')"
          >
            全部候选人
          </button>
          <button
            class="tab-chip"
            :class="{ active: quickTab === 'pending' }"
            @click="handleQuickTabChange('pending')"
          >
            待接受邀请
          </button>
          <button
            class="tab-chip"
            :class="{ active: quickTab === 'joined' }"
            @click="handleQuickTabChange('joined')"
          >
            已加入空间
          </button>
          <button
            class="tab-chip"
            :class="{ active: quickTab === 'token' }"
            @click="handleQuickTabChange('token')"
          >
            已获得凭证
          </button>
          <button
            class="tab-chip"
            :class="{ active: quickTab === 'outbound' }"
            @click="handleQuickTabChange('outbound')"
          >
            已出库
          </button>
          <button
            class="tab-chip"
            :class="{ active: quickTab === 'trash' }"
            @click="handleQuickTabChange('trash')"
          >
            垃圾箱
          </button>
        </div>

        <div class="search-input-wrap">
          <el-input
            v-model="searchKeyword"
            placeholder="搜索邮箱或分组..."
            clearable
            class="search-input"
          >
            <template #prefix>
              <Icon icon="lucide:search" class="search-icon" />
            </template>
          </el-input>
        </div>
      </div>

      <!-- 多维精细筛选栏 -->
      <div class="filters-bar">
        <div class="filter-item">
          <span class="filter-label">账号状态</span>
          <el-select v-model="accountStatusFilter" clearable size="small" placeholder="全部" class="filter-select">
            <el-option label="正常" value="active" />
            <el-option label="已永久失效" value="permanently_invalid" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">空间加入</span>
          <el-select v-model="joinStatusFilter" clearable size="small" placeholder="全部" class="filter-select">
            <el-option label="未邀请" value="not_invited" />
            <el-option label="待接受邀请" value="pending_invite" />
            <el-option label="待处理申请" value="pending_request" />
            <el-option label="已申请加入" value="join_requested" />
            <el-option label="已批准，待加入" value="approved" />
            <el-option label="已加入" value="joined" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">凭证状态</span>
          <el-select v-model="credentialStatusFilter" clearable size="small" placeholder="全部" class="filter-select">
            <el-option label="已获得 Team 凭证" value="workspace_credential" />
            <el-option label="仅 Personal 凭证" value="personal_credential" />
            <el-option label="无凭证" value="none" />
            <el-option label="凭证不可用" value="unavailable" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">席位类型</span>
          <el-select v-model="seatTypeFilter" clearable size="small" placeholder="全部" class="filter-select">
            <el-option label="标准席位" value="default" />
            <el-option label="Codex席位" value="usage_based" />
            <el-option label="高级席位（ProLite）" value="prolite" />
            <el-option label="未设置" value="none" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">垃圾箱</span>
          <el-select v-model="trashStatusFilter" clearable size="small" placeholder="全部" class="filter-select">
            <el-option label="正常" value="active" />
            <el-option label="待入箱" value="scheduled" />
            <el-option label="已入箱" value="trashed" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">出库状态</span>
          <el-select v-model="tagStatusFilter" clearable size="small" placeholder="默认隐藏" class="filter-select">
            <el-option label="正常" value="active" />
            <el-option label="已出库" value="outbound" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">分组</span>
          <el-select v-model="groupNameFilter" clearable size="small" placeholder="全部分组" class="filter-select">
            <el-option v-for="g in candidateGroups" :key="g" :label="g" :value="g" />
          </el-select>
        </div>

        <el-button link size="small" type="primary" class="reset-filter-btn" @click="resetFilters">
          重置筛选
        </el-button>
      </div>

      <!-- 批量工作流操作栏 (Workflow Action Bar) -->
      <div class="action-toolbar">
        <div class="action-group-left">
          <!-- 空间加入工作流 -->
          <div class="workflow-btn-group">
            <div class="seat-type-mini-selector">
              <span class="mini-label">目标席位:</span>
              <el-select v-model="seatType" size="small" class="seat-mini-select">
                <el-option label="标准席位" value="default" />
                <el-option label="Codex席位" value="usage_based" />
                <el-option label="高级席位 (ProLite)" value="prolite" />
              </el-select>
            </div>

            <el-button
              type="primary"
              size="small"
              :disabled="candidateMembershipBusy"
              :loading="membershipTaskRunning"
              @click="invite"
            >
              <Icon icon="lucide:user-plus" class="btn-icon" />
              母号批量邀请
            </el-button>

            <el-button
              type="primary"
              plain
              size="small"
              :disabled="candidateMembershipBusy"
              :loading="membershipTaskRunning"
              @click="join"
            >
              <Icon icon="lucide:log-in" class="btn-icon" />
              子号申请加入
            </el-button>

            <el-button
              type="info"
              plain
              size="small"
              :disabled="candidateMembershipBusy"
              :loading="candidateCheckRunning"
              @click="check"
            >
              <Icon icon="lucide:check-circle-2" class="btn-icon" />
              校验候选状态
            </el-button>
          </div>

          <el-divider direction="vertical" class="toolbar-divider" />

          <!-- 额度与凭证工作流 -->
          <div class="workflow-btn-group">
            <el-button
              type="success"
              size="small"
              :loading="quotaTaskRunning"
              @click="quota"
            >
              <Icon icon="lucide:gauge" class="btn-icon" />
              查询额度
            </el-button>

            <el-dropdown :disabled="candidateMembershipBusy" @command="changeSeat">
              <el-button size="small" :loading="seatSwitchRunning">
                <Icon icon="lucide:arrow-left-right" class="btn-icon" />
                切换席位
                <Icon icon="lucide:chevron-down" class="btn-icon-end" />
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="default">切换为标准席位</el-dropdown-item>
                  <el-dropdown-item command="usage_based">切换为 Codex 席位</el-dropdown-item>
                  <el-dropdown-item command="prolite">切换为高级席位（ProLite）</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>

            <el-dropdown @command="runCandidateAction">
              <el-button size="small" plain>
                <Icon icon="lucide:key" class="btn-icon" />
                凭证与登录
                <Icon icon="lucide:chevron-down" class="btn-icon-end" />
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="credentials">获取空间凭证 (OAuth/Password)</el-dropdown-item>
                  <el-dropdown-item command="login_only">仅登录空间</el-dropdown-item>
                  <el-dropdown-item divided command="select_full_quota">选取额度 100%</el-dropdown-item>
                  <el-dropdown-item command="select_quota_401">选取额度 401</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>

            <el-dropdown
              @command="runExportAction"
              @visible-change="(v) => v && loadExportFormats()"
            >
              <el-button size="small" plain type="success" :loading="exporting || pushing">
                <Icon icon="lucide:download" class="btn-icon" />
                导出候选人
                <Icon icon="lucide:chevron-down" class="btn-icon-end" />
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item
                    v-for="fmt in exportFormats"
                    :key="fmt.id"
                    :command="fmt"
                  >
                    {{ fmt.label }}
                  </el-dropdown-item>
                  <el-dropdown-item divided command="push">推送到 CPA 号池</el-dropdown-item>
                  <el-dropdown-item command="export_outbound">出库并导出加密 Sub2 (自动标记出库)</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </div>

        <div class="action-group-right">
          <el-checkbox
            v-model="plainCredentialMode"
            :disabled="exporting || pushing"
            title="勾选后 CPA 和 Sub2 导出明文凭证"
            class="plain-mode-check"
          >
            明文凭证导出
          </el-checkbox>

          <el-dropdown @command="runAssignAction">
            <el-button size="small" type="danger" plain>
              <Icon icon="lucide:more-horizontal" class="btn-icon" />
              划分/生命周期
              <Icon icon="lucide:chevron-down" class="btn-icon-end" />
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="outbound">标记为已出库</el-dropdown-item>
                <el-dropdown-item v-if="tagStatusFilter === 'outbound'" command="restore_outbound">恢复出库账号</el-dropdown-item>
                <el-dropdown-item divided command="trash">移入垃圾箱</el-dropdown-item>
                <el-dropdown-item command="restore_trash">移出垃圾箱</el-dropdown-item>
                <el-dropdown-item divided command="remove" style="color: var(--el-color-danger)">移除当前空间划分</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <!-- 额度查询实时进度条 -->
      <div v-if="quotaTaskRunning" class="quota-progress-banner">
        <div class="progress-banner-head">
          <div class="progress-title">
            <Icon icon="lucide:loader-2" class="spin-icon" />
            <span>额度查询中{{ reloginOn401 ? ' (含 401 自动重登录)' : '' }}</span>
          </div>
          <div class="progress-counts">
            <span>完成 <strong>{{ quotaProgress.done }}</strong> / {{ quotaProgress.total }}</span>
            <span class="count-divider">·</span>
            <span>并发处理中 <strong>{{ quotaProgress.active }}</strong></span>
            <span class="count-divider">·</span>
            <span class="text-success">成功 {{ quotaProgress.succeeded }}</span>
            <span class="count-divider">·</span>
            <span class="text-danger">失败 {{ quotaProgress.failed }}</span>
            <span v-if="quotaProgress.relogged" class="count-divider">·</span>
            <span v-if="quotaProgress.relogged" class="text-warning">重登 {{ quotaProgress.relogged }}</span>
          </div>
        </div>
        <el-progress
          :percentage="quotaProgress.total ? Math.round((quotaProgress.done / quotaProgress.total) * 100) : 0"
          :stroke-width="6"
          :show-text="false"
        />
      </div>

      <!-- 候选人数据表格 -->
      <el-table
        ref="candidateTableRef"
        v-loading="loading"
        :data="options"
        stripe
        class="modern-candidate-table"
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="46" :selectable="isSelectableCandidate" />

        <!-- 候选账号与分组 -->
        <el-table-column label="候选账号" min-width="260">
          <template #default="{ row }">
            <div class="account-cell">
              <div class="account-main-row">
                <span class="account-email">{{ row.email }}</span>
                <button
                  class="mini-copy-btn"
                  title="复制邮箱"
                  @click.stop="copyText(row.email)"
                >
                  <Icon icon="lucide:copy" />
                </button>
              </div>

              <div class="account-meta-row">
                <el-tag v-if="row.group_name" size="small" type="info" effect="plain" class="meta-tag">
                  {{ row.group_name }}
                </el-tag>
                <el-tag v-if="row.tag_status === 'outbound'" size="small" type="warning" effect="dark" class="meta-tag">
                  已出库
                </el-tag>
                <el-tag v-if="row.account_status === 'permanently_invalid'" size="small" type="danger" effect="dark" class="meta-tag">
                  已永久失效
                </el-tag>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 空间加入与席位 -->
        <el-table-column label="空间加入 & 席位" min-width="190">
          <template #default="{ row }">
            <div class="join-seat-cell">
              <div class="join-status-line">
                <el-tag
                  size="small"
                  :type="row.workspace_join_status === 'joined' ? 'success' : (row.workspace_join_status === 'pending_invite' ? 'warning' : (row.workspace_join_status === 'not_invited' ? 'info' : 'primary'))"
                  effect="light"
                >
                  {{ workspaceJoinStatusLabel(row.workspace_join_status) }}
                </el-tag>
              </div>

              <div class="seat-type-line">
                <el-tag
                  v-if="row.seat_label || row.seat_type"
                  size="small"
                  :type="seatTypeTagType(row.seat_label || row.seat_type)"
                  effect="plain"
                >
                  {{ seatLabel(row.seat_label || row.seat_type) }}
                </el-tag>
                <span v-else class="text-muted">—</span>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 凭证状态 -->
        <el-table-column label="空间 Team 凭证" min-width="160">
          <template #default="{ row }">
            <div class="credential-cell">
              <div v-if="row.credential_status === 'unavailable'" class="cred-pill error">
                <span class="cred-dot dot-danger" />
                <span>凭证不可用</span>
              </div>
              <div v-else-if="row.has_workspace_access_token" class="cred-pill success">
                <span class="cred-dot dot-success" />
                <span>已获得 Team 凭证</span>
              </div>
              <div v-else class="cred-pill muted">
                <span class="cred-dot dot-info" />
                <span>未获取凭证</span>
              </div>

              <div class="personal-token-hint">
                <span>Personal: {{ row.has_access_token ? '已具备' : '缺失' }}</span>
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 额度与限制监控 -->
        <el-table-column label="额度与使用率" min-width="230">
          <template #default="{ row }">
            <div class="quota-cell">
              <template v-if="row.quota_json">
                <!-- 错误状态 -->
                <div v-if="parseQuotaInfo(row)?.isError" class="quota-error-row">
                  <el-tag size="small" type="danger" effect="light">
                    HTTP {{ parseQuotaInfo(row)?.errorCode || 'Error' }}
                  </el-tag>
                  <span class="quota-error-msg">额度查询失败</span>
                </div>

                <!-- 正常额度展示 -->
                <div v-else class="quota-valid-wrap">
                  <div class="quota-bars-row">
                    <!-- 5小时额度 -->
                    <div v-if="parseQuotaInfo(row)?.primaryRemain != null" class="quota-pill-stat">
                      <span class="stat-label">5h剩余</span>
                      <span
                        class="stat-val"
                        :class="{
                          'text-success': (parseQuotaInfo(row)?.primaryRemain || 0) >= 80,
                          'text-warning': (parseQuotaInfo(row)?.primaryRemain || 0) < 80 && (parseQuotaInfo(row)?.primaryRemain || 0) >= 30,
                          'text-danger': (parseQuotaInfo(row)?.primaryRemain || 0) < 30
                        }"
                      >
                        {{ parseQuotaInfo(row)?.primaryRemain }}%
                      </span>
                    </div>

                    <!-- 周额度 -->
                    <div v-if="parseQuotaInfo(row)?.secondaryRemain != null" class="quota-pill-stat">
                      <span class="stat-label">周剩余</span>
                      <span class="stat-val text-primary">{{ parseQuotaInfo(row)?.secondaryRemain }}%</span>
                    </div>

                    <!-- 余额 -->
                    <div v-if="parseQuotaInfo(row)?.credits" class="quota-pill-stat">
                      <span class="stat-label">余额</span>
                      <span class="stat-val">{{ parseQuotaInfo(row)?.credits }}</span>
                    </div>
                  </div>

                  <div class="quota-updated-time">
                    {{ parseQuotaInfo(row)?.updatedAt }}
                  </div>
                </div>
              </template>

              <div v-else class="quota-empty-text">
                未查询
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 垃圾箱状态 -->
        <el-table-column label="生命周期" width="130">
          <template #default="{ row }">
            <div class="lifecycle-cell">
              <el-tag
                size="small"
                :type="row.trash_status === 'trashed' ? 'danger' : (row.trash_status === 'scheduled' ? 'warning' : 'success')"
                effect="plain"
              >
                {{ trashStatusLabel(row.trash_status) }}
              </el-tag>
              <div v-if="trashStatusHint(row)" class="trash-hint-text">
                {{ trashStatusHint(row) }}
              </div>
            </div>
          </template>
        </el-table-column>

        <!-- 实时操作状态 -->
        <el-table-column label="实时状态" width="150">
          <template #default="{ row }">
            <div class="status-cell">
              <div v-if="operationStatus[row.email]" class="op-running-tag">
                <Icon icon="lucide:loader-2" class="spin-icon-xs" />
                <span>{{ operationStatus[row.email] }}</span>
              </div>
              <el-tag
                v-else
                size="small"
                :type="row.display_status === 'trashed' ? 'danger' : (row.display_status === 'trash_scheduled' ? 'warning' : 'info')"
                effect="plain"
              >
                {{ displayStatus(row) }}
              </el-tag>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="当前母号空间暂无候选成员，请先在注册结果页面划分账号" :image-size="70" />
        </template>
      </el-table>

      <!-- 分页栏 -->
      <div class="pagination-row">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[100, 200, 500, 1000]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </el-card>

    <!-- 空间任务日志卡片 (Terminal Log Console) -->
    <el-card shadow="never" class="task-log-card">
      <template #header>
        <div class="task-log-head">
          <div class="task-log-title">
            <Icon icon="lucide:terminal" class="term-icon" />
            <span class="section-title" style="margin: 0">空间任务控制台日志</span>
            <span class="hint">仅展示当前母号空间相关任务执行流</span>
          </div>
          <div class="task-log-actions">
            <el-switch v-model="taskLogAutoRefresh" active-text="自动刷新" size="small" />
            <el-button size="small" plain :loading="taskLogLoading" @click="loadTaskLogs(false)">
              <Icon icon="lucide:refresh-cw" class="btn-icon" />
              刷新
            </el-button>
          </div>
        </div>
      </template>
      <div ref="taskLogBoxRef" class="task-log-box">
        <div v-if="!taskLogs.length" class="task-log-empty">暂无空间任务日志记录</div>
        <div
          v-for="item in taskLogs"
          :key="item.id"
          class="task-log-line"
          :class="`lv-${String(item.level || '').toLowerCase()}`"
        >
          <span class="task-log-time">[{{ taskLogTime(item.ts) }}]</span>
          <span class="task-log-level">[{{ item.level }}]</span>
          <span class="task-log-text">{{ item.text }}</span>
        </div>
      </div>
    </el-card>

    <!-- 空间任务设置抽屉 (Settings Drawer with Tabs) -->
    <el-drawer
      v-model="settingsVisible"
      :title="`空间任务设置 · ${currentWorkspace?.account || '当前母号'}`"
      direction="rtl"
      size="440px"
      @close="queueSpaceSettingsSave"
    >
      <div class="settings-drawer-content">
        <el-tabs v-model="settingsActiveTab" class="settings-tabs">
          <!-- Tab 1: 定时额度查询 -->
          <el-tab-pane label="定时额度" name="quota">
            <div class="settings-tab-pane">
              <div class="setting-switch-row">
                <div class="switch-meta">
                  <span class="switch-title">定时额度轮询</span>
                  <span class="switch-desc">只查询当前空间已获得 Team 凭证的候选人，从全局代理池租取代理。</span>
                </div>
                <el-switch v-model="quotaRunning" @change="toggleQuotaSchedule" />
              </div>

              <div v-if="quotaRunning" class="setting-info-box">
                <Icon icon="lucide:clock" class="box-icon" />
                <span>{{ nextQuotaText() || '准备就绪' }}</span>
              </div>

              <el-form label-position="top" class="settings-form">
                <el-form-item label="轮询间隔 (分钟)">
                  <el-input-number v-model="quotaInterval" :min="1" :max="1440" style="width: 100%" />
                  <div class="field-hint">支持自定义轮询周期，最低 1 分钟，最高 1440 分钟（24 小时）。</div>
                </el-form-item>

                <div class="setting-switch-row sub-row">
                  <div class="switch-meta">
                    <span class="switch-title">401 自动重新登录</span>
                    <span class="switch-desc">额度接口返回 401 时自动重新触发登录与凭证刷新</span>
                  </div>
                  <el-switch v-model="reloginOn401" />
                </div>

                <div class="setting-switch-row sub-row">
                  <div class="switch-meta">
                    <span class="switch-title">获取凭证后自动推送</span>
                    <span class="switch-desc">手动或自动获取 Team 凭证后推送到已配置的号池</span>
                  </div>
                  <el-switch v-model="autoPush" />
                </div>
              </el-form>
            </div>
          </el-tab-pane>

          <!-- Tab 2: 席位保护与自动补齐 -->
          <el-tab-pane label="席位与补齐" name="seats">
            <div class="settings-tab-pane">
              <el-form label-position="top" class="settings-form">
                <el-form-item label="席位补齐轮询周期 (分钟)">
                  <el-input-number v-model="autoSeatIntervalMinutes" :min="1" :max="1440" style="width: 100%" />
                  <div class="field-hint">标准席位和高级席位任务共用此周期，每轮内部成员切换间隔 5 秒。</div>
                </el-form-item>
              </el-form>

              <div class="setting-group-box">
                <div class="group-box-title">
                  <Icon icon="lucide:sparkles" class="box-icon" />
                  <span>自动补齐标准席位</span>
                </div>
                <div class="setting-switch-row">
                  <div class="switch-meta">
                    <span class="switch-title">开启自动补齐</span>
                    <span class="switch-desc">按缺口串行补齐标准席位，并自动触发凭证获取。</span>
                  </div>
                  <el-switch v-model="autoStandardSeatEnabled" @change="toggleAutoStandardSeat" />
                </div>
                <div v-if="autoStandardSeatEnabled && autoStandardSeatNextAt" class="countdown-hint">
                  下次轮询：{{ new Date(autoStandardSeatNextAt * 1000).toLocaleString() }}
                </div>
              </div>

              <div class="setting-group-box">
                <div class="group-box-title">
                  <Icon icon="lucide:zap" class="box-icon text-warning" />
                  <span>自动补齐高级席位 (ProLite)</span>
                </div>
                <div class="setting-switch-row">
                  <div class="switch-meta">
                    <span class="switch-title">开启高级席位升级</span>
                    <span class="switch-desc">轮询 ProLite 缺口并将候选人串行升级。</span>
                  </div>
                  <el-switch v-model="autoProliteSeatEnabled" @change="toggleAutoProliteSeat" />
                </div>

                <el-form label-position="top" class="settings-form sub-form">
                  <el-form-item label="目标候选人类型">
                    <el-select v-model="autoProliteCandidateSeatType" style="width: 100%">
                      <el-option label="标准席位" value="default" />
                      <el-option label="Codex席位" value="usage_based" />
                      <el-option label="全部可升级席位" value="all" />
                    </el-select>
                  </el-form-item>
                </el-form>
                <div v-if="autoProliteSeatEnabled && autoProliteSeatNextAt" class="countdown-hint">
                  下次轮询：{{ new Date(autoProliteSeatNextAt * 1000).toLocaleString() }}
                </div>
              </div>

              <div class="setting-group-box">
                <div class="group-box-title">
                  <Icon icon="lucide:shield-check" class="box-icon" />
                  <span>席位保护配额限制</span>
                </div>
                <div class="setting-switch-row">
                  <div class="switch-meta">
                    <span class="switch-title">标准席位保护</span>
                    <span class="switch-desc">当前周期已用 {{ seatProtectUsedCount }} / {{ seatProtectThreshold }}</span>
                  </div>
                  <el-switch v-model="seatProtectEnabled" />
                </div>
                <div v-if="seatProtectEnabled" class="sub-form-grid">
                  <el-form label-position="top">
                    <el-form-item label="保护阈值">
                      <el-input-number v-model="seatProtectThreshold" :min="1" :max="1000" style="width: 100%" />
                    </el-form-item>
                    <el-form-item label="每天刷新时间">
                      <el-time-picker v-model="seatProtectRefreshTime" format="HH:mm" value-format="HH:mm" style="width: 100%" />
                    </el-form-item>
                  </el-form>
                </div>

                <el-divider class="inner-divider" />

                <div class="setting-switch-row">
                  <div class="switch-meta">
                    <span class="switch-title">高级席位保护 (ProLite)</span>
                    <span class="switch-desc">当前周期已用 {{ proliteSeatProtectUsedCount }} / {{ proliteSeatProtectThreshold }}</span>
                  </div>
                  <el-switch v-model="proliteSeatProtectEnabled" />
                </div>
                <div v-if="proliteSeatProtectEnabled" class="sub-form-grid">
                  <el-form label-position="top">
                    <el-form-item label="高级席位阈值">
                      <el-input-number v-model="proliteSeatProtectThreshold" :min="1" :max="1000" style="width: 100%" />
                    </el-form-item>
                    <el-form-item label="每天刷新时间">
                      <el-time-picker v-model="proliteSeatProtectRefreshTime" format="HH:mm" value-format="HH:mm" style="width: 100%" />
                    </el-form-item>
                  </el-form>
                </div>
              </div>
            </div>
          </el-tab-pane>

          <!-- Tab 3: 运行参数与垃圾箱 -->
          <el-tab-pane label="参数与回收" name="tasks">
            <div class="settings-tab-pane">
              <el-form label-position="top" class="settings-form">
                <el-form-item label="并发处理数">
                  <el-input-number v-model="taskConcurrency" :min="1" :max="20" style="width: 100%" />
                </el-form-item>
                <el-form-item label="OTP 等待超时 (秒)">
                  <el-input-number v-model="taskOtpTimeout" :min="10" :max="600" style="width: 100%" />
                </el-form-item>
                <el-form-item label="账号重试次数">
                  <el-input-number v-model="taskRetry" :min="1" :max="5" style="width: 100%" />
                </el-form-item>
                <el-form-item label="任务间冷却秒数">
                  <el-input-number v-model="taskCooldown" :min="0" :max="3600" style="width: 100%" />
                </el-form-item>
                <el-form-item label="额度查询网络失败重试次数">
                  <el-input-number v-model="quotaNetworkRetries" :min="0" :max="5" style="width: 100%" />
                  <div class="hint">TLS 握手失败、连接超时与 5xx 共用此重试预算；429 另按 Retry-After 退避。</div>
                </el-form-item>
                <el-form-item label="候选人专属代理池">
                  <el-input
                    v-model="quotaProxyPool"
                    type="textarea"
                    :rows="5"
                    placeholder="每行一个代理，如 socks5://user:pass@host:1080&#10;留空则使用全局代理池"
                    style="width: 100%"
                  />
                  <div class="proxy-pool-actions">
                    <el-button size="small" @click="importGlobalProxyPool">从全局池导入</el-button>
                    <el-button size="small" @click="quotaProxyPool = ''">清空（回退全局池）</el-button>
                    <span class="hint">{{ quotaProxyPoolHint }}</span>
                  </div>
                  <div class="hint">
                    额度查询、401 重登录与凭证获取都走这里；连续 3 次传输失败的代理会自动冷却 30 分钟。
                  </div>
                </el-form-item>
              </el-form>

              <div class="setting-group-box">
                <div class="group-box-title">
                  <Icon icon="lucide:trash-2" class="box-icon" />
                  <span>垃圾箱生命周期规则</span>
                </div>
                <div class="setting-switch-row">
                  <div class="switch-meta">
                    <span class="switch-title">自动归入垃圾箱</span>
                    <span class="switch-desc">启用候选人生命周期自动入箱调度</span>
                  </div>
                  <el-switch v-model="trashEnabled" />
                </div>
                <div class="setting-switch-row">
                  <div class="switch-meta">
                    <span class="switch-title">失效账号自动入箱</span>
                    <span class="switch-desc">永久失效账号直接归入垃圾箱</span>
                  </div>
                  <el-switch v-model="trashInvalidEnabled" />
                </div>
                <el-form label-position="top" class="settings-form sub-form">
                  <el-form-item label="额度为 0 后延迟入箱 (分钟)">
                    <el-input-number v-model="trashZeroDelayMinutes" :min="1" :max="1440" style="width: 100%" />
                  </el-form-item>
                </el-form>
              </div>

              <div class="setting-group-box">
                <div class="group-box-title">
                  <Icon icon="lucide:pie-chart" class="box-icon text-primary" />
                  <span>当前空间回收与席位概览</span>
                </div>
                <div class="stat-summary-grid">
                  <div class="stat-summary-item">
                    <span class="lbl">已在垃圾箱</span>
                    <span class="val text-danger">{{ candidateStats.trash?.trashed_count || 0 }}</span>
                  </div>
                  <div class="stat-summary-item">
                    <span class="lbl">延迟归箱中</span>
                    <span class="val text-warning">{{ candidateStats.trash?.scheduled_count || 0 }}</span>
                  </div>
                  <div class="stat-summary-item">
                    <span class="lbl">到期待清理</span>
                    <span class="val text-danger">{{ candidateStats.trash?.due_scheduled_count || 0 }}</span>
                  </div>
                  <div class="stat-summary-item">
                    <span class="lbl">失效待入箱</span>
                    <span class="val">{{ candidateStats.trash?.invalid_pending_trash_count || 0 }}</span>
                  </div>
                  <div class="stat-summary-item">
                    <span class="lbl">标准席位补齐</span>
                    <span class="val text-primary">{{ candidateStats.seat_fulfillment?.standard?.count || 0 }} (累计 {{ candidateStats.seat_fulfillment?.standard?.fulfilled_total || 0 }})</span>
                  </div>
                  <div class="stat-summary-item">
                    <span class="lbl">高级席位补齐</span>
                    <span class="val text-warning">{{ candidateStats.seat_fulfillment?.prolite?.count || 0 }} (累计 {{ candidateStats.seat_fulfillment?.prolite?.fulfilled_total || 0 }})</span>
                  </div>
                </div>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </el-drawer>

    <!-- 导出预览弹窗 -->
    <el-dialog
      v-model="exportVisible"
      :title="`${exportLabel}（${exportCount} 个）`"
      width="700px"
      class="export-dialog"
    >
      <el-input v-model="exportText" type="textarea" :rows="16" readonly class="export-area" />
      <template #footer>
        <el-button @click="exportVisible = false">关闭</el-button>
        <el-button
          type="primary"
          @click="saveBlob(exportText, exportFilename, 'text/plain;charset=utf-8')"
        >
          下载文件
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.candidate-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* Hero KPI Section */
.hero-card {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-lg);
  padding: 16px 20px;
  box-shadow: var(--app-shadow-sm);
}

.hero-top-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.hero-selector-wrap {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.space-select-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 14px;
  color: var(--el-text-color-primary);
}

.space-icon {
  font-size: 18px;
  color: var(--el-color-primary);
}

.space-select {
  width: 380px;
}

.space-option-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.space-option-account {
  font-weight: 500;
  margin-right: 8px;
}

.hero-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.settings-trigger-btn {
  position: relative;
}

.active-badge {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-success);
  box-shadow: 0 0 0 2px var(--el-bg-color);
}

/* KPI Cards Grid */
.hero-kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 14px;
}

.kpi-card {
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-md);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  transition: all 0.2s ease;
}

.kpi-card:hover {
  border-color: var(--el-border-color);
  box-shadow: var(--app-shadow-sm);
}

.kpi-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.kpi-title-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
}

.kpi-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.kpi-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.dot-primary { background: var(--el-color-primary); }
.dot-warning { background: var(--el-color-warning); }
.dot-info { background: #8b5cf6; }
.dot-success { background: var(--el-color-success); }
.dot-danger { background: var(--el-color-danger); }

.kpi-body {
  margin: 4px 0 8px;
}

.kpi-value-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-bottom: 6px;
}

.kpi-main-val {
  font-size: 22px;
  font-weight: 700;
  color: var(--el-text-color-primary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.kpi-sub-val {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.kpi-bar-track {
  height: 6px;
  width: 100%;
  background: var(--el-fill-color-dark);
  border-radius: 999px;
  overflow: hidden;
}

.kpi-bar-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.3s ease;
}

.fill-primary { background: var(--el-color-primary); }
.fill-warning { background: var(--el-color-warning); }

.kpi-footer {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  flex-wrap: wrap;
}

.kpi-protect-badge {
  color: var(--el-color-primary);
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 10px;
  background: var(--el-color-primary-light-9);
  padding: 1px 4px;
  border-radius: var(--app-radius-xs);
}

.text-danger {
  color: var(--el-color-danger) !important;
}

.kpi-stat-subtext {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
}

.meta-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.meta-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
}

.meta-label {
  color: var(--el-text-color-secondary);
}

.meta-val {
  font-family: ui-monospace, SFMono-Regular, monospace;
}

.highlight-val {
  font-weight: 600;
  color: var(--el-color-success);
}

.mono-sub {
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 11px;
  word-break: break-all;
}

.copy-chip-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  background: var(--el-fill-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-xs);
  font-size: 11px;
  cursor: pointer;
  color: var(--el-text-color-regular);
  transition: all 0.15s;
}

.copy-chip-btn:hover {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-5);
}

/* Main Table Card */
.main-card {
  border-radius: var(--app-radius-lg);
}

/* View Tabs & Omni-Search */
.view-tabs-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 12px;
}

.quick-tabs {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.tab-chip {
  padding: 6px 12px;
  border-radius: var(--app-radius-md);
  border: 1px solid transparent;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}

.tab-chip:hover {
  color: var(--el-color-primary);
  background: var(--el-fill-color);
}

.tab-chip.active {
  background: var(--el-color-primary);
  color: #fff;
  font-weight: 600;
}

.search-input-wrap {
  min-width: 240px;
}

.search-icon {
  font-size: 15px;
  color: var(--el-text-color-secondary);
}

/* Multi-dimension Filter Bar */
.filters-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.filter-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.filter-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.filter-select {
  width: 120px;
}

.reset-filter-btn {
  font-size: 12px;
}

/* Action Toolbar */
.action-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 14px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-md);
  margin-bottom: 14px;
}

.action-group-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.workflow-btn-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.seat-type-mini-selector {
  display: flex;
  align-items: center;
  gap: 6px;
}

.mini-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.seat-mini-select {
  width: 140px;
}

.toolbar-divider {
  height: 20px;
  margin: 0 4px;
}

.action-group-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.plain-mode-check {
  font-size: 12px;
  margin-right: 0;
}

.btn-icon {
  margin-right: 4px;
  font-size: 14px;
}

.btn-icon-xs {
  font-size: 12px;
}

.btn-icon-end {
  margin-left: 4px;
  font-size: 12px;
}

/* Quota Progress Banner */
.quota-progress-banner {
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: var(--app-radius-md);
  padding: 10px 14px;
  margin-bottom: 14px;
}

.progress-banner-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
  font-size: 13px;
}

.progress-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--el-color-primary);
}

.progress-counts {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.count-divider {
  color: var(--el-border-color);
}

.text-success { color: var(--el-color-success); }
.text-warning { color: var(--el-color-warning); }
.text-danger { color: var(--el-color-danger); }
.text-primary { color: var(--el-color-primary); }
.text-muted { color: var(--el-text-color-placeholder); }

.spin-icon {
  animation: spin 1s linear infinite;
  font-size: 16px;
}

.spin-icon-xs {
  animation: spin 1s linear infinite;
  font-size: 13px;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* Modern Candidate Table Styling */
.modern-candidate-table :deep(.el-table__header) th {
  background: var(--el-fill-color-light);
  font-weight: 600;
  font-size: 12px;
  color: var(--el-text-color-primary);
}

.account-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.account-main-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.account-email {
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-primary);
}

.mini-copy-btn {
  background: transparent;
  border: none;
  padding: 2px;
  cursor: pointer;
  color: var(--el-text-color-placeholder);
  display: inline-flex;
  align-items: center;
  border-radius: var(--app-radius-xs);
  transition: color 0.15s;
}

.mini-copy-btn:hover {
  color: var(--el-color-primary);
  background: var(--el-fill-color);
}

.account-meta-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.meta-tag {
  font-size: 11px;
}

.join-seat-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.credential-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cred-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
}

.cred-pill.success { color: var(--el-color-success); }
.cred-pill.error { color: var(--el-color-danger); }
.cred-pill.muted { color: var(--el-text-color-secondary); }

.cred-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.personal-token-hint {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
}

.quota-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.quota-bars-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.quota-pill-stat {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-xs);
  font-size: 11px;
}

.quota-pill-stat .stat-label {
  color: var(--el-text-color-secondary);
}

.quota-pill-stat .stat-val {
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, monospace;
}

.quota-updated-time {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
}

.quota-error-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.quota-error-msg {
  font-size: 12px;
  color: var(--el-color-danger);
}

.quota-empty-text {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.lifecycle-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.trash-hint-text {
  font-size: 10px;
  color: var(--el-text-color-placeholder);
}

.status-cell {
  display: flex;
  align-items: center;
}

.op-running-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--el-color-warning);
  font-size: 12px;
  font-weight: 500;
}

.pagination-row {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}

/* Terminal Log Console */
.task-log-card {
  border-radius: var(--app-radius-lg);
}

.task-log-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.task-log-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.term-icon {
  font-size: 18px;
  color: var(--el-color-primary);
}

.task-log-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.task-log-box {
  max-height: 280px;
  overflow-y: auto;
  padding: 12px 14px;
  background: var(--app-log-bg, #1e1f22);
  color: var(--app-log-text, #d4d4d4);
  border-radius: var(--app-radius-md);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
}

.task-log-empty {
  color: #737373;
}

.task-log-line {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  word-break: break-word;
  white-space: pre-wrap;
}

.task-log-time {
  color: #737373;
  flex-shrink: 0;
}

.task-log-level {
  min-width: 50px;
  flex-shrink: 0;
  color: #858585;
}

.task-log-text {
  flex: 1;
}

.task-log-line.lv-warning .task-log-level,
.task-log-line.lv-warning .task-log-text { color: #f59e0b; }
.task-log-line.lv-error .task-log-level,
.task-log-line.lv-error .task-log-text { color: #ef4444; }
.task-log-line.lv-info .task-log-level,
.task-log-line.lv-info .task-log-text { color: #60a5fa; }

/* Settings Drawer Styles */
.settings-drawer-content {
  padding: 0 4px;
}

.settings-tab-pane {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-top: 8px;
}

.setting-switch-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.setting-switch-row.sub-row {
  margin-top: 14px;
}

.switch-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.switch-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.switch-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
}

.setting-info-box {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--el-fill-color-light);
  border-radius: var(--app-radius-sm);
  font-size: 12px;
  color: var(--el-color-primary);
}

.setting-group-box {
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-lighter);
  border-radius: var(--app-radius-md);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.group-box-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 13px;
  color: var(--el-text-color-primary);
}

.box-icon {
  font-size: 16px;
  color: var(--el-color-primary);
}

.countdown-hint {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  font-style: italic;
}

.field-hint {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
  line-height: 1.4;
}

.proxy-pool-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 6px;
}

.inner-divider {
  margin: 6px 0;
}

.sub-form {
  margin-top: 8px;
}

.sub-form-grid {
  margin-top: 8px;
}

.stat-summary-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.stat-summary-item {
  background: var(--el-fill-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--app-radius-sm);
  padding: 8px 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.stat-summary-item .lbl {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.stat-summary-item .val {
  font-size: 14px;
  font-weight: 700;
  font-family: ui-monospace, SFMono-Regular, monospace;
}
</style>
