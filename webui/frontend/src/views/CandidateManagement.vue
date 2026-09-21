<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Icon } from "@iconify/vue";
import { useRoute, useRouter } from "vue-router";
import { storeToRefs } from "pinia";
import { useProxyStore } from "@/stores/proxy";
import { listWorkspaceMasters, syncWorkspace, syncWorkspaceMembers } from "@/api/workspaces";
import { listExportFormats, exportRegistered, pushRegisteredToCpa } from "@/api/register";
import { generateRedeemCodes } from "@/api/redeemCodes";
import { getCpaExportTemplate, saveCpaExportTemplate } from "@/api/settings";
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
  acceptWorkspaceInvite,
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
  testWorkspacePushTarget,
  deleteCandidatesEverywhere,
  trashCandidates,
  kickCandidates,
  restoreCandidatesFromTrash,
  emptyWorkspaceTrash,
  listCandidateTags,
  setCandidateTags,
  listResetCredits,
  consumeResetCredit,
} from "@/api/workspaceCandidates";
import { PAGE_SIZE_OPTIONS, SELECT_ALL_FETCH_LIMIT } from "@/utils/pagination";

const spaces = ref([]);
const workspaceId = ref(null);
const options = ref([]);
const selected = ref([]);
const candidateTableRef = ref(null);
const loading = ref(false);
const seatType = ref("default");
const { list: proxyList } = storeToRefs(useProxyStore());
const route = useRoute();
const router = useRouter();
// 垃圾箱单独路由复用本组件：route.meta.trash 时只展示已入箱成员并隐藏正常候选人的操作。
const isTrashView = computed(() => Boolean(route.meta?.trash));

function goTrashView() {
  router.push("/workspace-candidates/trash");
}

function goCandidateView() {
  router.push("/workspace-candidates");
}

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

const automationPaused = ref(false);
const quotaRunning = ref(false);
const quotaInterval = ref(30);
const reloginOn401 = ref(false);
const autoPush = ref(false);
const autoPushSub2apiEnabled = ref(true);
const autoPushCpaEnabled = ref(true);
const autoPushSub2apiUrl = ref("");
const autoPushSub2apiApiKey = ref("");
const autoPushSub2apiGroupIds = ref("");
const autoPushCpaUrl = ref("");
const autoPushCpaMgmtKey = ref("");
const cpaStaticProxyEnabled = ref(false);
const cpaStaticProxyPool = ref("");
const autoPushSkipCodexSeat = ref(true);
const pushTestRunning = ref({ sub2api: false, cpa: false });

// 先落库再测：测试读的是已保存的空间覆盖 + 全局兜底，不先存会测到旧配置。
async function testPushTarget(target) {
  if (!workspaceId.value || pushTestRunning.value[target]) return;
  pushTestRunning.value = { ...pushTestRunning.value, [target]: true };
  try {
    await saveSpaceSettings();
    const r = await testWorkspacePushTarget(workspaceId.value, target);
    ElMessage.success(r.result?.message || `${target} 连通正常`);
  } catch (e) {
    ElMessage.error(`${target} 测试失败: ` + (e.message || e));
  } finally {
    pushTestRunning.value = { ...pushTestRunning.value, [target]: false };
  }
}
const nextQuotaAt = ref(0);
const taskConcurrency = ref(1);
const taskOtpTimeout = ref(180);
const taskRetry = ref(1);
const taskCooldown = ref(0);
const quotaNetworkRetries = ref(2);
// 额度耗尽时自动兑换重置券。默认关闭：券是不可逆的消耗品。
const quotaAutoResetEnabled = ref(false);
const quotaProxyPool = ref("");

const quotaProxyPoolCount = computed(
  () => new Set(quotaProxyPool.value.split("\n").map((x) => x.trim()).filter(Boolean)).size,
);

// 专属池为空即回退全局池，所以提示要说清当前实际生效的是哪一份。
const quotaProxyPoolHint = computed(() =>
  quotaProxyPoolCount.value
    ? `已配置 ${quotaProxyPoolCount.value} 条，不再使用全局池`
    : `未配置，回退全局池（${proxyList.value.length} 条）`,
);

// 定时额度 tab 里那句代理来源说明。配了专属池还写"从全局代理池租取"会让人
// 以为专属池没生效，所以跟着实际生效的池子走。
const quotaProxySourceDesc = computed(() =>
  quotaProxyPoolCount.value
    ? `只查询当前空间已获得 Team 凭证的候选人，使用本空间专属代理池（${quotaProxyPoolCount.value} 条）。`
    : `只查询当前空间已获得 Team 凭证的候选人，从全局代理池租取代理（${proxyList.value.length} 条）。`,
);

function importGlobalProxyPool() {
  if (!proxyList.value.length) return ElMessage.warning("全局代理池为空");
  quotaProxyPool.value = proxyList.value.join("\n");
  ElMessage.success(`已导入 ${proxyList.value.length} 条代理，可在此基础上删改`);
}

const trashEnabled = ref(true);
const trashInvalidEnabled = ref(true);
const trashZeroDelayMinutes = ref(60);
const trashZeroQuotaWindow = ref("any");
const trashGapSeconds = ref(30);

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
const autoSeatSwitchGapSeconds = ref(30);
const autoProliteCandidateSeatType = ref("default");
const autoStandardSeatTarget = ref(0);
const autoProliteSeatTarget = ref(0);

// 批量踢出成员的随机等待范围（秒）
const kickDelayMinSeconds = ref(2);
const kickDelayMaxSeconds = ref(5);

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
    trash_zero_quota_window: "any",
    trash_gap_seconds: 30,
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
      target: 0,
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
      target: 0,
    },
    codex: {
      count: 0,
    },
    outbound_count: 0,
    auto_interval_minutes: 5,
    auto_switch_gap_seconds: 30,
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
const trashStatusFilter = ref(isTrashView.value ? "trashed" : "active");
const tagStatusFilter = ref("");
const groupNameFilter = ref("");
const tagFilter = ref("");
const redeemStatusFilter = ref("");
const searchKeyword = ref("");
const quickTab = ref(isTrashView.value ? "trash" : "all");

const candidateGroups = ref([]);
const candidateTags = ref([]);

// ── 候选标签标记 ──
const tagDialogVisible = ref(false);
const tagDialogTags = ref([]);
const tagDialogMode = ref("add");
const tagDialogSaving = ref(false);

function openTagDialog() {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  tagDialogTags.value = [];
  tagDialogMode.value = "add";
  tagDialogVisible.value = true;
}

async function applyCandidateTags() {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) {
    tagDialogVisible.value = false;
    return ElMessage.warning("请选择候选人");
  }
  const tags = tagDialogTags.value.map((t) => String(t || "").trim()).filter(Boolean);
  if (!tags.length && tagDialogMode.value !== "set") {
    return ElMessage.warning("请输入至少一个标签；要清空标签请选择「覆盖」并留空");
  }
  tagDialogSaving.value = true;
  try {
    const r = await setCandidateTags(workspaceId.value, emails, tags, tagDialogMode.value);
    tagDialogVisible.value = false;
    ElMessage.success(`标签已更新（影响 ${r.changed ?? emails.length} 个账号）`);
    await load();
    await loadCandidateTags();
  } catch (e) {
    ElMessage.error("标签更新失败: " + e.message);
  } finally {
    tagDialogSaving.value = false;
  }
}

async function loadCandidateTags() {
  if (!workspaceId.value) {
    candidateTags.value = [];
    return;
  }
  try {
    const r = await listCandidateTags(workspaceId.value);
    candidateTags.value = r.tags || [];
  } catch (_) {
    candidateTags.value = [];
  }
}
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
  if (!row) return "";
  if (row.trash_status === "scheduled" && row.trash_due_at) {
    return `到期 ${new Date(row.trash_due_at * 1000).toLocaleString()}`;
  }
  if (row.trash_status === "trashed" && row.trash_reason) {
    const reason = {
      kicked: "已踢出",
      manual_trash: "手动移入",
      quota_zero: "额度为0",
      trash_retry: "自动回收",
    }[String(row.trash_reason)];
    return reason || String(row.trash_reason);
  }
  return "";
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

// available=0 表示已购席位全部占满，自动补齐/升级此时必然失败。null 是"没同步过"，
// 不能当成 0 报警，所以两种情况要分开判。
const standardSeatsFull = computed(() => currentWorkspace.value?.seats_default_available === 0);
const proliteSeatsFull = computed(() => currentWorkspace.value?.seats_prolite_available === 0);

function seatAvailableText(available) {
  return available === null || available === undefined ? "空位未同步" : `可分配 ${available} 席`;
}

// paid = 在用 + held + available，held 是已占住席位但还没落定的成员（待解决）。
// 只要同步过就显示，哪怕是 0——这是个需要盯的运营指标，藏起来会让人以为没做。
// null/undefined 才是没数据，那时不显示。
// 注意：候选人状态里另有"待处理申请"(pending_request)，是完全不同的东西，
// 所以这里叫"待解决"，不要跟着改成"待处理"。
function seatHeldText(held) {
  return held === null || held === undefined ? "" : ` · 待解决 ${held} 席`;
}

// 进度条分段宽度。已购为 0（或未同步）时不画任何段，避免除零后整条涂满。
function seatBarPct(value, entitled) {
  const total = Number(entitled) || 0;
  if (total <= 0) return "0%";
  return `${Math.min(100, Math.round(((Number(value) || 0) / total) * 100))}%`;
}

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

// 上游的 primary/secondary 并不固定对应 5h/周：多数账号只返回一个周窗口，
// 而它落在 primary 上。窗口种类只能按 window_seconds 认（18000=5h，604800=周），
// 否则「5h剩余」这一栏在大部分行上显示的其实是周额度。
const QUOTA_WINDOW_SECONDS = { fiveHour: 18000, weekly: 604800 };
const QUOTA_WINDOW_TOLERANCE = 600;

function quotaWindowsByKind(q) {
  const found = {};
  for (const key of ["primary", "secondary"]) {
    const window = q?.[key];
    const seconds = Number(window?.window_seconds);
    if (!window || !Number.isFinite(seconds)) continue;
    for (const [kind, expected] of Object.entries(QUOTA_WINDOW_SECONDS)) {
      if (!found[kind] && Math.abs(seconds - expected) <= QUOTA_WINDOW_TOLERANCE) {
        found[kind] = window;
        break;
      }
    }
  }
  return found;
}

function quotaWindowRemain(window) {
  return window?.used_percent != null ? Math.max(0, 100 - Number(window.used_percent)) : null;
}

// 上游 wham/usage 返回的额度重置券。available 是账号名下的券数，applicable
// 是"用在当前耗尽状态上真的有效"的券数——存在 available=1 但 applicable=0 的
// 账号（额度没真正打满，兑券会白烧一张），所以"现在能不能重置"看 applicable。
// 字段缺失（旧记录、上游没返回）与 0 必须区分开，所以这里返回 null 而不是 0。
function parseResetCredits(q) {
  const block = q?.reset_credits;
  if (!block || typeof block !== "object") return null;
  const toCount = (value) => (Number.isFinite(Number(value)) && value !== null ? Number(value) : null);
  const available = toCount(block.available);
  const applicable = toCount(block.applicable);
  if (available == null && applicable == null) return null;
  return { available, applicable };
}

// 额度耗尽归因。workspace_member_credits_depleted 是母号空间的池子被掏空，
// 属于空间级问题，重置券只重置速率窗口，救不了这一类。
const QUOTA_REACHED_TYPE_LABELS = {
  workspace_member_credits_depleted: "空间额度耗尽",
  usage_limit_reached: "用量上限",
};

function quotaReachedTypeLabel(value) {
  const key = String(value || "").trim();
  if (!key) return "";
  return QUOTA_REACHED_TYPE_LABELS[key] || key;
}

function parseQuotaInfo(row) {
  try {
    if (!row?.quota_json) return null;
    const q = JSON.parse(row.quota_json);
    if (!q) return null;
    const isError = Boolean(q.error_code);
    const errorCode = q.error_code ? String(q.error_code) : "";
    const windows = quotaWindowsByKind(q);
    const fiveHourRemain = quotaWindowRemain(windows.fiveHour);
    const weeklyRemain = quotaWindowRemain(windows.weekly);
    // 窗口时长缺失时退回按字段位置展示，标注为「剩余」而非具体窗口。
    const unknownRemain =
      fiveHourRemain == null && weeklyRemain == null
        ? quotaWindowRemain(q.primary) ?? quotaWindowRemain(q.secondary)
        : null;
    const credits = q.credits_balance || "";
    const updatedAt = q.updated_at ? new Date(q.updated_at * 1000).toLocaleString() : "";
    const resetCredits = parseResetCredits(q);
    const reachedType = quotaReachedTypeLabel(q.rate_limit_reached_type);
    return {
      isError, errorCode, fiveHourRemain, weeklyRemain, unknownRemain, credits, updatedAt,
      resetCredits, reachedType,
    };
  } catch (_) {
    return null;
  }
}

// 重置券这一栏的提示文案：有券但当前用不上是最容易误读的状态，必须说清楚。
function resetCreditsHint(info) {
  const reset = info?.resetCredits;
  if (!reset) return "";
  const available = reset.available ?? 0;
  const applicable = reset.applicable ?? 0;
  if (!available) return "该账号名下没有可用的额度重置券。";
  if (!applicable) {
    return `名下有 ${available} 张重置券，但当前额度状态用不上（额度未真正耗尽，或耗尽原因不是速率窗口打满）。点击可查看并手动兑换。`;
  }
  return `名下有 ${available} 张重置券，其中 ${applicable} 张可用于当前的额度耗尽状态。点击可查看并手动兑换。`;
}

// 手动兑换重置券的行内入口。券是不可逆消耗品（上游 2xx 即扣券），所以流程被
// 刻意拆成两步：先只读拉取券列表，把 id / 有效期摆给用户看，确认后才 POST。
const resetCreditBusyEmail = ref("");

function resetCreditDate(value) {
  if (!value) return "未知";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
}

// CPA 家宽代理单元格只显示 host:port，账号密码等敏感部分留在 tooltip 完整串里。
function cpaProxyLabel(proxy) {
  const s = String(proxy || "").trim();
  if (!s) return "";
  const m = s.match(/^[a-zA-Z0-9+.-]+:\/\/(?:[^@/]*@)?([^/]+)/);
  return m ? m[1] : s;
}

async function openResetCredit(row) {
  const email = String(row?.email || "").trim();
  if (!email) return;
  const ws = workspaceId.value;
  if (!ws) return ElMessage.warning("请选择母号空间");
  if (resetCreditBusyEmail.value) return ElMessage.warning("重置券操作正在进行中");
  // 兑换不可撤销：查询→确认→兑换期间用户可能切换空间，空间和代理池必须在入口快照。
  const proxyPool = quotaProxyPool.value;

  resetCreditBusyEmail.value = email;
  try {
    let listing;
    try {
      listing = await listResetCredits(ws, email, proxyPool);
    } catch (e) {
      return ElMessage.error("重置券查询失败: " + (e.message || e));
    }
    const usable = (listing?.credits || []).filter((c) => c?.status === "available");
    if (!usable.length) {
      return ElMessage.warning(`${email} 名下没有可兑换的重置券`);
    }
    const target = usable[0];
    // 上游只重置速率窗口，救不了「空间额度耗尽」这一类，兑了也是白烧。
    const info = parseQuotaInfo(row);
    const wastedWarning = info?.reachedType
      ? `\n\n注意：该账号当前的耗尽原因是「${info.reachedType}」，重置券只恢复速率窗口，很可能无法解决，兑换后券直接作废。`
      : (info?.resetCredits?.applicable === 0
        ? "\n\n注意：上游标记这张券当前「不适用」（额度尚未真正耗尽），现在兑换会浪费掉。"
        : "");
    try {
      await ElMessageBox.confirm(
        `即将为 ${email} 兑换 1 张额度重置券。\n\n` +
          `券 ID：${target.id}\n` +
          `类型：${target.reset_type || "未知"}\n` +
          `有效期至：${resetCreditDate(target.expires_at)}\n` +
          `名下可用：${usable.length} 张\n\n` +
          `兑换不可撤销：上游一旦返回成功，这张券就消耗掉了，即使只重置了部分窗口。` +
          wastedWarning,
        "兑换额度重置券",
        {
          type: "warning",
          confirmButtonText: "确认兑换（不可撤销）",
          cancelButtonText: "取消",
          // 消耗类操作的文案里有换行和 ID，必须原样显示。
          customClass: "reset-credit-confirm",
        }
      );
    } catch (_) {
      return;
    }

    setOneOperation(email, "兑换重置券中…");
    try {
      const result = await consumeResetCredit(ws, email, target.id, proxyPool);
      await load();
      if (result?.quota_error) {
        // 券已经扣掉了，这里绝不能报成失败，否则用户会再点一次再烧一张。
        ElMessage.warning(`重置券已兑换，但额度重查失败：${result.quota_error}`);
      } else {
        const windows = result?.consumed?.windows_reset;
        ElMessage.success(
          `重置券已兑换${windows ? `（windows_reset=${windows}）` : ""}，额度已刷新`
        );
      }
    } catch (e) {
      ElMessage.error("重置券兑换失败: " + (e.message || e));
    } finally {
      setOneOperation(email, "");
    }
  } finally {
    resetCreditBusyEmail.value = "";
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
      tag: tagFilter.value,
      redeem_status: redeemStatusFilter.value,
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
  if (isTrashView.value) return;
  quickTab.value = tab;
  redeemStatusFilter.value = "";
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
  } else if (tab === "redeem") {
    trashStatusFilter.value = "active";
    tagStatusFilter.value = "";
    joinStatusFilter.value = "";
    credentialStatusFilter.value = "";
    redeemStatusFilter.value = "has_code";
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
  trashStatusFilter.value = isTrashView.value ? "trashed" : "active";
  tagStatusFilter.value = "";
  groupNameFilter.value = "";
  tagFilter.value = "";
  redeemStatusFilter.value = "";
  searchKeyword.value = "";
  quickTab.value = isTrashView.value ? "trash" : "all";
}

async function remove() {
  const emails = selected.value.map((x) => x.email);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  setOperation(emails, "移除中…");
  try {
    await removeCandidates(workspaceId.value, emails);
    ElMessage.success("已移除候选划分");
    clearSelection();
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
    clearSelection();
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
    clearSelection();
    await load();
  } catch (e) {
    ElMessage.error("移入垃圾箱失败: " + e.message);
  } finally {
    clearOperation(emails);
  }
}

const emptyingTrash = ref(false);

async function emptyTrash() {
  if (!workspaceId.value) return ElMessage.warning("请先选择母号空间");
  try {
    await ElMessageBox.confirm(
      `将清空当前空间的垃圾箱：\n` +
        `垃圾箱内全部账号会从整个系统删除（所有空间的候选划分、已获取凭证、注册结果、邮箱号池）。\n\n` +
        `清空后不可恢复，确定？`,
      "清空垃圾箱",
      { type: "warning", confirmButtonText: "确认清空（不可恢复）", cancelButtonText: "取消", customClass: "reset-credit-confirm" }
    );
  } catch {
    return;
  }
  emptyingTrash.value = true;
  try {
    const r = await emptyWorkspaceTrash(workspaceId.value);
    if (r.deleted) {
      ElMessage.success(`已清空垃圾箱：删除 ${r.deleted} 个账号（候选 ${r.candidates} / 凭证 ${r.credentials} / 注册结果 ${r.registered} / 号池 ${r.pool}）`);
    } else {
      ElMessage.info("垃圾箱已经是空的");
    }
    clearSelection();
    await load();
    await loadStats();
  } catch (e) {
    ElMessage.error("清空垃圾箱失败: " + e.message);
  } finally {
    emptyingTrash.value = false;
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
    clearSelection();
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
      ElMessage.success(r.invite_error
        ? `上游请求超时但状态已确认（${seatName}）`
        : `邀请成功（${seatName}）：${emails.length} 个已标记为待接受邀请`);
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
  // 快照发起时的空间：串行循环跨多次请求，中途切空间后必须继续打在
  // 原空间上，否则剩余成员会错误地校验/操作到新空间。
  const ws = workspaceId.value;
  let succeeded = 0;
  let failed = 0;
  setOperation(emails, "排队中…");
  try {
    for (const email of emails) {
      setOneOperation(email, "校验中…");
      try {
        const result = await checkCandidates(ws, [email]);
        const states = result.states || {};
        const seats = result.seats || {};
        // 已切走空间时 options 里是新空间的行，不再就地补丁（避免误写同名行）。
        if (workspaceId.value === ws) {
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
        }
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

      // 已切走空间时 options 里是新空间的行，不再就地补丁（避免误写同名行）。
      if (workspaceId.value !== workspace) return;
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
  // 快照发起时的空间和行席位：切空间后 options 会换成新空间的行，循环里
  // 不能再读 options/workspaceId，否则会把剩余成员切到新空间或误判跳过。
  const ws = workspaceId.value;
  const seatSnapshot = new Map(
    candidates.map((x) => [String(x.email || "").toLowerCase(), x.seat_label || x.seat_type])
  );

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
      const localSeat = canonicalSeat(seatSnapshot.get(String(email || "").toLowerCase()));
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
        const r = await updateCandidateSeat(ws, [email], target);
        const item = (r.results || [])[0] || {};
        if (item.skipped) {
          skipped += 1;
        } else if (item.ok) {
          succeeded += 1;
          if (workspaceId.value === ws) {
            options.value = options.value.map((row) => {
              if (String(row.email || "").toLowerCase() !== email.toLowerCase()) return row;
              return { ...row, seat_label: target, seat_type: target };
            });
          }
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
  if (command === "accept_invite") return acceptInvite();
  if (command === "select_full_quota") return selectFullQuotaCandidates();
  if (command === "select_quota_401") return selectQuota401Candidates();
  if (command === "trash") return moveToTrash();
  if (command === "restore_trash") return restoreFromTrash();
}

function clearSelection() {
  // 开了 reserve-selection 后，只清 selected 不会取消表格里已勾的行，
  // 必须走表格实例的 clearSelection 才能把跨页保留的勾选一起清掉。
  candidateTableRef.value?.clearSelection();
  selected.value = [];
}

async function selectAllFiltered() {
  if (!workspaceId.value) return;
  try {
    const a = await listCandidateOptions(workspaceId.value, {
      limit: SELECT_ALL_FETCH_LIMIT,
      offset: 0,
      account_status: accountStatusFilter.value,
      join_status: joinStatusFilter.value,
      credential_status: credentialStatusFilter.value,
      seat_type: seatTypeFilter.value,
      trash_status: trashStatusFilter.value,
      tag_status: tagStatusFilter.value,
      group_name: groupNameFilter.value,
      tag: tagFilter.value,
      redeem_status: redeemStatusFilter.value,
      keyword: searchKeyword.value || undefined,
    });
    const items = a.items || [];
    // 后端把 limit 夹在 1000（app.py），候选人真超过这个数时全选会被截断，
    // 必须明说，否则用户以为选全了、批量操作却只落到前 1000 个。
    const truncated = Number(a.total || 0) > items.length;
    // 表格的 :selectable 会挡住外发候选人，全选也照同一套规则跳过，
    // 否则 selected 里混进勾不上的行，批量操作数量会对不上。
    const all = items.filter((row) => isSelectableCandidate(row));
    const table = candidateTableRef.value;
    if (!table) return ElMessage.warning("候选列表尚未加载完成");
    table.clearSelection();
    await nextTick();
    // 全量结果里只有当前页那部分行存在于表格中；靠 row-key="email" 匹配，
    // 其余行由 selected 兜住，翻页时 reserve-selection 会自动补上勾选态。
    all.forEach((row) => table.toggleRowSelection(row, true));
    selected.value = all;
    if (truncated) {
      ElMessage.warning(`已选 ${all.length} 个候选人，但当前筛选共 ${a.total} 个，超出单次上限未全部选中，请收窄筛选条件`);
    } else {
      ElMessage.success(`已全选当前筛选条件下的 ${all.length} 个候选人`);
    }
  } catch (e) {
    ElMessage.error("全选失败: " + e.message);
  }
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
  if (command === "invite_csv") return openInviteCsv();
  if (command === "redeem_codes") return openRedeemCodesDialog();
  return doExport(command);
}

async function kick() {
  const rows = selected.value.filter(
    (x) => x.trash_status !== "trashed" && (x.workspace_join_status === "joined" || x.member_id)
  );
  if (!rows.length) return ElMessage.warning("所选候选人里没有已加入空间的成员");
  const skipped = selected.value.length - rows.length;
  const emails = rows.map((x) => x.email);
  try {
    await ElMessageBox.confirm(
      `将把 ${emails.length} 个成员从 OpenAI 空间移除（上游真正踢出，释放席位）。` +
        (skipped ? `\n\n另有 ${skipped} 个未加入空间的候选人被跳过。` : "") +
        `\n\n踢出成功后该账号会被移入本空间垃圾箱并标记为已踢出（清空成员身份/席位、删除空间凭证；注册结果与号池保留）。之后可在垃圾箱中恢复为普通候选人或彻底删除。确定？`,
      "踢出空间成员",
      { type: "warning", confirmButtonText: "确认踢出", cancelButtonText: "取消", customClass: "reset-credit-confirm" }
    );
  } catch {
    return;
  }
  // 串行踢出队列：一次只提交一个成员，当前成员显示「踢出中」，
  // 其余显示「排队中」；随机等待由前端在两次请求之间执行。
  const delayMin = Math.max(0, Number(kickDelayMinSeconds.value) || 0);
  const delayMax = Math.max(delayMin, Number(kickDelayMaxSeconds.value) || 0);
  // 快照发起时的空间：串行队列跨多次请求，切空间后必须继续踢原空间的
  // 成员，否则剩余成员会打到新空间（甚至误踢新空间里的同名成员）。
  const ws = workspaceId.value;
  setOperation(emails, "排队中");
  let kicked = 0;
  const failedEmails = [];
  for (let i = 0; i < emails.length; i++) {
    const email = emails[i];
    setOneOperation(email, `踢出中 ${i + 1}/${emails.length}`);
    let ok = false;
    try {
      const r = await kickCandidates(ws, [email]);
      const item = (r.results || [])[0] || {};
      ok = Boolean(item.ok ?? r.kicked);
      if (ok) {
        kicked++;
        clearOperation([email]);
      } else {
        failedEmails.push(email);
        setOneOperation(email, item.error || "踢出失败");
      }
    } catch (e) {
      failedEmails.push(email);
      setOneOperation(email, e.message || "踢出失败");
    }
    // 与后端批处理语义一致：只在成功踢出后、且后面还有人时才随机等待。
    if (ok && i < emails.length - 1 && delayMax > 0) {
      const wait = delayMin + Math.random() * (delayMax - delayMin);
      await new Promise((resolve) => setTimeout(resolve, wait * 1000));
    }
  }
  const failed = failedEmails.length;
  const detail = failed && failed <= 5 ? `：${failedEmails.join("、")}` : "";
  ElMessage[failed ? "warning" : "success"](`已踢出 ${kicked} 个${failed ? `，失败 ${failed}${detail}` : ""}`);
  clearOperation(emails);
  clearSelection();
  await load();
  await loadStats();
}

async function deleteEverywhere() {
  const emails = selected.value.map((x) => x.email);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  try {
    await ElMessageBox.confirm(
      `将从整个系统删除 ${emails.length} 个账号：\n` +
        `· 所有空间的候选划分\n` +
        `· 所有空间已获取的凭证\n` +
        `· 注册结果\n` +
        `· 邮箱号池\n\n` +
        `删除后不可恢复，确定？`,
      "删除账号",
      { type: "warning", confirmButtonText: "确认删除（不可恢复）", cancelButtonText: "取消", customClass: "reset-credit-confirm" }
    );
  } catch {
    return;
  }
  setOperation(emails, "删除中…");
  try {
    const r = await deleteCandidatesEverywhere(workspaceId.value, emails);
    ElMessage.success(
      `已删除 ${emails.length} 个账号（候选 ${r.candidates} / 凭证 ${r.credentials} / 注册结果 ${r.registered} / 号池 ${r.pool}）`
    );
    clearSelection();
    await load();
    await loadStats();
  } catch (e) {
    ElMessage.error("删除失败: " + e.message);
  } finally {
    clearOperation(emails);
  }
}

async function runAssignAction(command) {
  if (command === "remove") return remove();
  if (command === "delete_everywhere") return deleteEverywhere();
  if (command === "kick") return kick();
  if (command === "trash") return moveToTrash();
  if (command === "restore_trash") return restoreFromTrash();
  if (command === "outbound") return setOutboundStatus("outbound", "标记出库");
  if (command === "restore_outbound") return setOutboundStatus("active", "恢复出库账号");
  if (command === "tag_marks") return openTagDialog();
}

async function saveSpaceSettings(targetId = workspaceId.value) {
  if (!targetId || !settingsReady.value || settingsWorkspaceId.value !== targetId || workspaceId.value !== targetId) return;
  try {
    await saveCandidateSettings({
      workspace_id: targetId,
      interval_minutes: quotaInterval.value,
      relogin_on_401: reloginOn401.value,
      proxy_pool: proxyList.value.join("\n"),
      automation_paused: automationPaused.value,
      auto_push: autoPush.value,
      auto_push_sub2api_enabled: autoPushSub2apiEnabled.value,
      auto_push_cpa_enabled: autoPushCpaEnabled.value,
      auto_push_sub2api_url: autoPushSub2apiUrl.value,
      auto_push_sub2api_api_key: autoPushSub2apiApiKey.value,
      auto_push_sub2api_group_ids: autoPushSub2apiGroupIds.value,
      auto_push_cpa_url: autoPushCpaUrl.value,
      auto_push_cpa_mgmt_key: autoPushCpaMgmtKey.value,
      cpa_static_proxy_enabled: cpaStaticProxyEnabled.value,
      cpa_static_proxy_pool: cpaStaticProxyPool.value,
      auto_push_skip_codex_seat: autoPushSkipCodexSeat.value,
      concurrency: taskConcurrency.value,
      otp_timeout: taskOtpTimeout.value,
      account_retry_count: taskRetry.value,
      cool_down_seconds: taskCooldown.value,
      quota_network_retries: quotaNetworkRetries.value,
      quota_auto_reset_enabled: quotaAutoResetEnabled.value,
      quota_proxy_pool: quotaProxyPool.value,
      trash_enabled: trashEnabled.value,
      trash_invalid_enabled: trashInvalidEnabled.value,
      trash_zero_delay_minutes: trashZeroDelayMinutes.value,
      trash_zero_quota_window: trashZeroQuotaWindow.value,
      trash_gap_seconds: trashGapSeconds.value,
      seat_protect_enabled: seatProtectEnabled.value,
      seat_protect_threshold: seatProtectThreshold.value,
      seat_protect_refresh_time: seatProtectRefreshTime.value,
      prolite_seat_protect_enabled: proliteSeatProtectEnabled.value,
      prolite_seat_protect_threshold: proliteSeatProtectThreshold.value,
      prolite_seat_protect_refresh_time: proliteSeatProtectRefreshTime.value,
      auto_standard_seat_enabled: autoStandardSeatEnabled.value,
      auto_prolite_seat_enabled: autoProliteSeatEnabled.value,
      auto_standard_seat_target: autoStandardSeatTarget.value,
      auto_prolite_seat_target: autoProliteSeatTarget.value,
      auto_seat_interval_minutes: autoSeatIntervalMinutes.value,
      auto_seat_switch_gap_seconds: autoSeatSwitchGapSeconds.value,
      auto_prolite_candidate_seat_type: autoProliteCandidateSeatType.value,
      kick_delay_min_seconds: kickDelayMinSeconds.value,
      kick_delay_max_seconds: kickDelayMaxSeconds.value,
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
    automationPaused.value = Boolean(c.automation_paused);
    autoPush.value = Boolean(c.auto_push);
    autoPushSub2apiEnabled.value = c.auto_push_sub2api_enabled !== false;
    autoPushCpaEnabled.value = c.auto_push_cpa_enabled !== false;
    autoPushSub2apiUrl.value = String(c.auto_push_sub2api_url || "");
    autoPushSub2apiApiKey.value = String(c.auto_push_sub2api_api_key || "");
    autoPushSub2apiGroupIds.value = String(c.auto_push_sub2api_group_ids || "");
    autoPushCpaUrl.value = String(c.auto_push_cpa_url || "");
    autoPushCpaMgmtKey.value = String(c.auto_push_cpa_mgmt_key || "");
    cpaStaticProxyEnabled.value = Boolean(c.cpa_static_proxy_enabled);
    cpaStaticProxyPool.value = String(c.cpa_static_proxy_pool || "");
    autoPushSkipCodexSeat.value = c.auto_push_skip_codex_seat !== false;
    taskConcurrency.value = Number(c.concurrency || 1);
    taskOtpTimeout.value = Number(c.otp_timeout || 180);
    taskRetry.value = Number(c.account_retry_count || 1);
    taskCooldown.value = Number(c.cool_down_seconds || 0);
    quotaNetworkRetries.value = Number(c.quota_network_retries ?? 2);
    quotaAutoResetEnabled.value = Boolean(c.quota_auto_reset_enabled);
    quotaProxyPool.value = String(c.quota_proxy_pool || "");
    trashEnabled.value = c.trash_enabled !== false;
    trashInvalidEnabled.value = c.trash_invalid_enabled !== false;
    trashZeroDelayMinutes.value = Number(c.trash_zero_delay_minutes || 60);
    trashZeroQuotaWindow.value = String(c.trash_zero_quota_window || "any");
    trashGapSeconds.value = Math.min(600, Math.max(0, Number(c.trash_gap_seconds ?? 30)));
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
    autoStandardSeatTarget.value = Math.max(0, Number(c.auto_standard_seat_target) || 0);
    autoProliteSeatTarget.value = Math.max(0, Number(c.auto_prolite_seat_target) || 0);
    autoSeatIntervalMinutes.value = Math.min(1440, Math.max(1, Number(c.auto_seat_interval_minutes) || 5));
    autoSeatSwitchGapSeconds.value = Math.min(600, Math.max(0, Number(c.auto_seat_switch_gap_seconds ?? 30)));
    autoProliteCandidateSeatType.value = ["default", "usage_based", "all"].includes(String(c.auto_prolite_candidate_seat_type || "default"))
      ? String(c.auto_prolite_candidate_seat_type || "default")
      : "default";
    kickDelayMinSeconds.value = Math.min(600, Math.max(0, Number(c.kick_delay_min_seconds ?? 2)));
    kickDelayMaxSeconds.value = Math.min(600, Math.max(0, Number(c.kick_delay_max_seconds ?? 5)));
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
      automationPaused.value = false;
      autoPush.value = false;
      autoPushSub2apiEnabled.value = true;
      autoPushCpaEnabled.value = true;
      autoPushSub2apiUrl.value = "";
      autoPushSub2apiApiKey.value = "";
      autoPushSub2apiGroupIds.value = "";
      autoPushCpaUrl.value = "";
      autoPushCpaMgmtKey.value = "";
      cpaStaticProxyEnabled.value = false;
      cpaStaticProxyPool.value = "";
      autoPushSkipCodexSeat.value = true;
      taskConcurrency.value = 1;
      taskOtpTimeout.value = 180;
      taskRetry.value = 1;
      taskCooldown.value = 0;
      quotaNetworkRetries.value = 2;
      quotaAutoResetEnabled.value = false;
      quotaProxyPool.value = "";
      trashEnabled.value = true;
      trashInvalidEnabled.value = true;
      trashZeroDelayMinutes.value = 60;
      trashZeroQuotaWindow.value = "any";
      trashGapSeconds.value = 30;
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
      autoStandardSeatTarget.value = 0;
      autoProliteSeatTarget.value = 0;
      autoSeatIntervalMinutes.value = 5;
      autoSeatSwitchGapSeconds.value = 30;
      autoProliteCandidateSeatType.value = "default";
      kickDelayMinSeconds.value = 2;
      kickDelayMaxSeconds.value = 5;
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
          automation_paused: automationPaused.value,
          auto_push_sub2api_enabled: autoPushSub2apiEnabled.value,
          auto_push_cpa_enabled: autoPushCpaEnabled.value,
          auto_push_sub2api_url: autoPushSub2apiUrl.value,
          auto_push_sub2api_api_key: autoPushSub2apiApiKey.value,
          auto_push_sub2api_group_ids: autoPushSub2apiGroupIds.value,
          auto_push_cpa_url: autoPushCpaUrl.value,
          auto_push_cpa_mgmt_key: autoPushCpaMgmtKey.value,
          cpa_static_proxy_enabled: cpaStaticProxyEnabled.value,
          cpa_static_proxy_pool: cpaStaticProxyPool.value,
          auto_push_skip_codex_seat: autoPushSkipCodexSeat.value,
          concurrency: taskConcurrency.value,
          otp_timeout: taskOtpTimeout.value,
          account_retry_count: taskRetry.value,
          cool_down_seconds: taskCooldown.value,
          quota_network_retries: quotaNetworkRetries.value,
          quota_auto_reset_enabled: quotaAutoResetEnabled.value,
          quota_proxy_pool: quotaProxyPool.value,
          trash_enabled: trashEnabled.value,
          trash_invalid_enabled: trashInvalidEnabled.value,
          trash_zero_delay_minutes: trashZeroDelayMinutes.value,
          trash_zero_quota_window: trashZeroQuotaWindow.value,
          trash_gap_seconds: trashGapSeconds.value,
          seat_protect_enabled: seatProtectEnabled.value,
          seat_protect_threshold: seatProtectThreshold.value,
          seat_protect_refresh_time: seatProtectRefreshTime.value,
          prolite_seat_protect_enabled: proliteSeatProtectEnabled.value,
          prolite_seat_protect_threshold: proliteSeatProtectThreshold.value,
          prolite_seat_protect_refresh_time: proliteSeatProtectRefreshTime.value,
          auto_standard_seat_enabled: autoStandardSeatEnabled.value,
          auto_prolite_seat_enabled: autoProliteSeatEnabled.value,
          auto_standard_seat_target: autoStandardSeatTarget.value,
          auto_prolite_seat_target: autoProliteSeatTarget.value,
          auto_seat_interval_minutes: autoSeatIntervalMinutes.value,
          auto_seat_switch_gap_seconds: autoSeatSwitchGapSeconds.value,
          auto_prolite_candidate_seat_type: autoProliteCandidateSeatType.value,
          kick_delay_min_seconds: kickDelayMinSeconds.value,
          kick_delay_max_seconds: kickDelayMaxSeconds.value,
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
    else ElMessage.success(`仅登录任务已启动：已提交 ${eligible} 个（跳过 OAuth），登录完成后会自动把进入空间的成员标记为已加入，请在运行记录查看`);
  } catch (e) {
    ElMessage.error(e.message);
  } finally {
    clearOperation(emails);
  }
}

async function acceptInvite() {
  // 并行接受队列（与额度查询同一模式）：并发数取空间设置的「任务并发」，
  // 在处理中的成员显示「接受中」，其余显示「排队中」；单个失败不中断队列。
  const rows = selected.value.filter((x) => x.workspace_join_status === "pending_invite");
  if (!rows.length) return ElMessage.warning("所选候选人里没有待接受邀请的成员");
  const skipped = selected.value.length - rows.length;
  const emails = rows.map((x) => x.email);
  if (skipped) ElMessage.info(`已跳过 ${skipped} 个非待接受状态的成员`);
  const pool = proxyList.value.join("\n");
  const concurrency = Math.min(Math.max(1, Number(taskConcurrency.value) || 1), 20);
  const workspace = workspaceId.value;
  let joined = 0;
  let stillPending = 0;
  const failedItems = [];
  setOperation(emails, "排队中");
  await runRollingPool(emails, concurrency, async (email) => {
    setOneOperation(email, "接受中…");
    try {
      const r = await acceptWorkspaceInvite(workspace, [email], pool);
      const item = (r.results || [])[0] || {};
      if (item.status === "joined") {
        joined++;
        clearOperation([email]);
      } else if (item.ok) {
        // 请求成功但没转成成员：可能上游还没落地，或邀请已失效。
        stillPending++;
        setOneOperation(email, item.error || "未加入");
      } else {
        failedItems.push(`${email}: ${item.error || "接受失败"}`);
        setOneOperation(email, item.error || "接受失败");
      }
    } catch (e) {
      failedItems.push(`${email}: ${e.message || "接受失败"}`);
      setOneOperation(email, e.message || "接受失败");
    }
  });
  const failed = failedItems.length;
  const detail = failed && failed <= 5 ? `（${failedItems.join("；")}）` : "";
  const parts = [`已加入 ${joined} 个`];
  if (stillPending) parts.push(`仍待接受 ${stillPending} 个`);
  if (failed) parts.push(`失败 ${failed} 个${detail}`);
  ElMessage[failed || stillPending ? "warning" : "success"](`接受邀请完成：${parts.join("，")}`);
  clearOperation(emails);
  await load();
  await loadStats();
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
    const credentialFormat = fmt.id === "cpa" || fmt.id === "sub2api" || fmt.id === "sub2api_lines";
    const r = await exportRegistered({
      format: fmt.id,
      emails,
      workspace_id: workspaceId.value,
      proxy_pool: proxyList.value.join("\n"),
      ...(credentialFormat ? { encrypt_credentials: encryptCredentials.value } : {}),
      ...(exportOptions.encryptCredentials !== undefined ? { encrypt_credentials: Boolean(exportOptions.encryptCredentials) } : {}),
      ...(exportOptions.markOutbound ? { mark_outbound: true } : {}),
      ...(fmt.id === "cpa" && cpaUseTemplate.value ? { cpa_template: true } : {}),
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

// ── 导出邀请 CSV ──
// 模板：电子邮件,角色,席位 / 每行 email,成员,<席位>（见 docs/第二车邀请.csv）
const inviteCsvVisible = ref(false);
const inviteCsvSeat = ref("Premium");
const INVITE_CSV_SEATS = ["Premium", "Standard", "Codex"];

function openInviteCsv() {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  inviteCsvVisible.value = true;
}

function downloadInviteCsv() {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) {
    inviteCsvVisible.value = false;
    return ElMessage.warning("请选择候选人");
  }
  const seat = inviteCsvSeat.value;
  // 不加 BOM：上游批量邀请接口按表头解析，BOM 会污染首列名。
  const csv = ["电子邮件,角色,席位", ...emails.map((e) => `${e},成员,${seat}`)].join("\r\n") + "\r\n";
  saveBlob(csv, `邀请_${seat}_${emails.length}人.csv`, "text/csv;charset=utf-8");
  inviteCsvVisible.value = false;
  ElMessage.success(`已导出邀请 CSV：${emails.length} 个账号，席位 ${seat}`);
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
      clearSelection();
      await load();
      ElMessage.success(`加密导出完成，已标记出库 ${result.outbound_marked} 个`);
    }
  } finally {
    if (!succeeded) selected.value = previousSelection;
  }
}

// ── 生成并导出兑换码 ──
// 只有已持有当前空间凭证的账号能拿到码；每个账号固定一个码，重复导出幂等。
// 「允许兑换账号密码+2FA」默认关闭，勾选后该批码可在兑换页取明文凭证，
// 重复导出会按本次勾选覆盖旧码的能力位。
const redeemDialogVisible = ref(false);
const redeemAllowSecret = ref(false);

function openRedeemCodesDialog() {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  if (!workspaceId.value) return ElMessage.warning("请选择母号空间");
  redeemAllowSecret.value = false;
  redeemDialogVisible.value = true;
}

async function exportRedeemCodes() {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  if (!workspaceId.value) return ElMessage.warning("请选择母号空间");
  redeemDialogVisible.value = false;
  exporting.value = true;
  try {
    const r = await generateRedeemCodes(workspaceId.value, emails, redeemAllowSecret.value);
    exportText.value = r.text || "";
    exportFilename.value = r.filename || "redeem-codes.txt";
    exportLabel.value = r.label || "兑换码";
    exportCount.value = r.count || 0;
    exportVisible.value = true;
    if (r.skipped?.length) {
      ElMessage.warning(`已跳过 ${r.skipped.length} 个无空间凭证的账号：${r.skipped.join("、")}`);
    }
  } catch (e) {
    ElMessage.error("生成兑换码失败: " + e.message);
  } finally {
    exporting.value = false;
  }
}

// ── CPA 导出模版配置 ──
// 勾选导出栏「CPA 按模版」时，后端把模版里的凭证级代理 proxy_url 和
// 「启用凭证文件」（JSON disabled 取反）写进每个 CPA 凭证文件；
// 不勾选则按原样导出。勾选状态存 localStorage，模版存后端 settings。
const CPA_USE_TEMPLATE_KEY = "cpa_use_template";
const cpaUseTemplate = ref(false);
const cpaTplVisible = ref(false);
const cpaTplSaving = ref(false);
const cpaTpl = ref({ proxy_url: "", file_enabled: true });

async function openCpaTplDialog() {
  cpaTplVisible.value = true;
  try {
    const r = await getCpaExportTemplate();
    const t = r.template || {};
    cpaTpl.value = {
      proxy_url: String(t.proxy_url || ""),
      file_enabled: t.file_enabled !== false,
    };
  } catch (e) {
    ElMessage.error("加载 CPA 模版配置失败: " + e.message);
  }
}

async function saveCpaTpl() {
  cpaTplSaving.value = true;
  try {
    const r = await saveCpaExportTemplate({
      proxy_url: cpaTpl.value.proxy_url,
      file_enabled: cpaTpl.value.file_enabled,
    });
    const t = r.template || {};
    cpaTpl.value = {
      proxy_url: String(t.proxy_url || ""),
      file_enabled: t.file_enabled !== false,
    };
    ElMessage.success("CPA 导出模版已保存");
    cpaTplVisible.value = false;
  } catch (e) {
    ElMessage.error("保存模版失败: " + e.message);
  } finally {
    cpaTplSaving.value = false;
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
  // 换空间等于换了一整批候选人，reserve-selection 保留的旧空间勾选必须丢掉，
  // 否则批量操作会带着上一个空间的邮箱打到新空间上。
  clearSelection();
  if (!id) return;
  await load();
  await loadCandidateGroups();
  await loadCandidateTags();
  await loadSpaceSettings(id);
  await loadTaskLogs(true);
  await startTaskLogPolling();
});

watch(
  [
    quotaInterval,
    reloginOn401,
    automationPaused,
    autoPush,
    autoPushSub2apiEnabled,
    autoPushCpaEnabled,
    autoPushSub2apiUrl,
    autoPushSub2apiApiKey,
    autoPushSub2apiGroupIds,
    autoPushCpaUrl,
    autoPushCpaMgmtKey,
    cpaStaticProxyEnabled,
    cpaStaticProxyPool,
    autoPushSkipCodexSeat,
    taskConcurrency,
    taskOtpTimeout,
    taskRetry,
    taskCooldown,
    quotaNetworkRetries,
    quotaAutoResetEnabled,
    quotaProxyPool,
    trashEnabled,
    trashInvalidEnabled,
    trashZeroDelayMinutes,
    trashZeroQuotaWindow,
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

watch(autoSeatSwitchGapSeconds, (value) => {
  // el-input-number 清空时会给 null，Number(null) 是 0，所以只能显式判 NaN 回默认值。
  const raw = Number(value);
  const seconds = Number.isFinite(raw) ? Math.min(600, Math.max(0, Math.round(raw))) : 30;
  if (value !== seconds) autoSeatSwitchGapSeconds.value = seconds;
  queueSpaceSettingsSave();
});

watch(trashGapSeconds, (value) => {
  const raw = Number(value);
  const seconds = Number.isFinite(raw) ? Math.min(600, Math.max(0, Math.round(raw))) : 30;
  if (value !== seconds) trashGapSeconds.value = seconds;
  queueSpaceSettingsSave();
});

for (const delayRef of [kickDelayMinSeconds, kickDelayMaxSeconds]) {
  watch(delayRef, (value) => {
    const raw = Number(value);
    const fallback = delayRef === kickDelayMinSeconds ? 2 : 5;
    const seconds = Number.isFinite(raw) ? Math.min(600, Math.max(0, Math.round(raw))) : fallback;
    if (value !== seconds) delayRef.value = seconds;
    queueSpaceSettingsSave();
  });
}

watch(autoSeatIntervalMinutes, (value) => {
  const minutes = Math.min(1440, Math.max(1, Number(value) || 5));
  if (value !== minutes) autoSeatIntervalMinutes.value = minutes;
  if (autoStandardSeatEnabled.value) autoStandardSeatNextAt.value = Date.now() / 1000 + minutes * 60;
  if (autoProliteSeatEnabled.value) autoProliteSeatNextAt.value = Date.now() / 1000 + minutes * 60;
});

watch([autoStandardSeatEnabled, autoProliteSeatEnabled, autoProliteCandidateSeatType], queueSpaceSettingsSave);

// 补齐目标：0 = 已购席位上限；填正数时不能超过已购上限（上限未同步到时由后端钳制）。
watch(autoStandardSeatTarget, (value) => {
  const cap = Number(currentWorkspace.value?.seats_default_entitled) || 0;
  let target = Math.max(0, Math.round(Number(value) || 0));
  if (cap > 0 && target > cap) target = cap;
  if (value !== target) autoStandardSeatTarget.value = target;
  queueSpaceSettingsSave();
});

watch(autoProliteSeatTarget, (value) => {
  const cap = Number(currentWorkspace.value?.seats_prolite_entitled) || 0;
  let target = Math.max(0, Math.round(Number(value) || 0));
  if (cap > 0 && target > cap) target = cap;
  if (value !== target) autoProliteSeatTarget.value = target;
  queueSpaceSettingsSave();
});

watch(
  [accountStatusFilter, joinStatusFilter, credentialStatusFilter, seatTypeFilter, trashStatusFilter, tagStatusFilter, groupNameFilter, tagFilter, redeemStatusFilter],
  () => {
    page.value = 1;
    clearSelection();
    if (workspaceId.value) load();
  }
);

// 搜索走服务端过滤：每个键击都发请求太浪费，300ms 防抖后再加载。
let searchDebounceTimer = 0;
watch(searchKeyword, () => {
  page.value = 1;
  clearSelection();
  clearTimeout(searchDebounceTimer);
  if (!workspaceId.value) return;
  searchDebounceTimer = setTimeout(() => load(), 300);
});

// 候选管理/垃圾箱两个路由共用本组件，切换时把筛选重置到对应视图再重载。
watch(isTrashView, () => {
  page.value = 1;
  clearSelection();
  resetFilters();
  if (workspaceId.value) load();
});

// 翻页不再清空选择：reserve-selection 会跨页保留勾选，清掉反而让跨页全选失效。
// 每页条数变化会重排行序，此时保留选中容易与用户预期不符，故只在 pageSize 变化时清。
watch(page, () => {
  if (workspaceId.value) load();
});

watch(pageSize, () => {
  page.value = 1;
  clearSelection();
  if (workspaceId.value) load();
});

watch(plainCredentialMode, (value) => {
  try {
    localStorage.setItem(PLAIN_CREDENTIAL_MODE_STORAGE_KEY, value ? "1" : "0");
  } catch (_) {}
});

watch(cpaUseTemplate, (value) => {
  try {
    localStorage.setItem(CPA_USE_TEMPLATE_KEY, value ? "1" : "0");
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
      cpaUseTemplate.value = localStorage.getItem(CPA_USE_TEMPLATE_KEY) === "1";
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
              <!-- 数字始终在页脚显示（含 0），这里的 tag 只在 >0 时出现当告警用。 -->
              <el-tag
                v-if="currentWorkspace.seats_default_held"
                size="small"
                type="warning"
                effect="plain"
                class="kpi-inline-tag"
                title="已占住席位但尚未落定的成员，需要人工跟进"
              >
                待解决 {{ currentWorkspace.seats_default_held }}
              </el-tag>
              <el-tag v-if="standardSeatsFull" size="small" type="danger" effect="plain" class="kpi-inline-tag">
                无空位
              </el-tag>
            </div>
            <!-- 已购席位分三段：在用 + 待解决(held) + 空闲。只画前两段，
                 剩下的槽底色就是空闲，held 单列出来才解释得清"4/4 却无空位"。 -->
            <div class="kpi-bar-track">
              <div class="kpi-bar-fill fill-primary" :style="{ width: seatBarPct(currentWorkspace.seats_default, currentWorkspace.seats_default_entitled) }" />
              <div
                v-if="currentWorkspace.seats_default_held"
                class="kpi-bar-fill fill-held"
                :style="{ width: seatBarPct(currentWorkspace.seats_default_held, currentWorkspace.seats_default_entitled) }"
              />
            </div>
          </div>
          <div class="kpi-footer">
            <span>
              {{ seatAvailableText(currentWorkspace.seats_default_available) }}{{ seatHeldText(currentWorkspace.seats_default_held) }} · 累计补齐 {{ candidateStats.seat_fulfillment?.standard?.fulfilled_total || 0 }} 席
            </span>
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
              <el-tag
                v-if="currentWorkspace.seats_prolite_held"
                size="small"
                type="warning"
                effect="plain"
                class="kpi-inline-tag"
                title="已占住席位但尚未落定的成员，需要人工跟进"
              >
                待解决 {{ currentWorkspace.seats_prolite_held }}
              </el-tag>
              <el-tag v-if="proliteSeatsFull" size="small" type="danger" effect="plain" class="kpi-inline-tag">
                无空位
              </el-tag>
            </div>
            <div class="kpi-bar-track">
              <div class="kpi-bar-fill fill-warning" :style="{ width: seatBarPct(currentWorkspace.seats_prolite, currentWorkspace.seats_prolite_entitled) }" />
              <div
                v-if="currentWorkspace.seats_prolite_held"
                class="kpi-bar-fill fill-held"
                :style="{ width: seatBarPct(currentWorkspace.seats_prolite_held, currentWorkspace.seats_prolite_entitled) }"
              />
            </div>
          </div>
          <div class="kpi-footer">
            <span>
              {{ seatAvailableText(currentWorkspace.seats_prolite_available) }}{{ seatHeldText(currentWorkspace.seats_prolite_held) }} · 累计补齐 {{ candidateStats.seat_fulfillment?.prolite?.fulfilled_total || 0 }} 席
            </span>
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
            <el-tag size="small" type="info" effect="plain" class="kpi-tag">按量计费</el-tag>
          </div>
          <div class="kpi-body">
            <div class="kpi-value-row">
              <span class="kpi-main-val">{{ currentWorkspace.seats_usage_based ?? 0 }}</span>
              <span class="kpi-sub-val">在用</span>
            </div>
            <!-- 旧文案是"已购总席位 8 · 在用合计 79"，两个数并排看着像超额 10 倍。
                 实际 79 = 标准 + ProLite + Codex，而 Codex 按量计费不占已购席位，
                 所以这里把加数拆开写清楚。 -->
            <div class="kpi-stat-subtext">
              不占已购席位（订阅席位 {{ currentWorkspace.seats_entitled ?? '-' }}）
            </div>
          </div>
          <div class="kpi-footer">
            <span>
              空间在用合计 {{ currentWorkspace.seats_in_use ?? '-' }} =
              标准 {{ currentWorkspace.seats_default ?? 0 }} + ProLite {{ currentWorkspace.seats_prolite ?? 0 }} + Codex {{ currentWorkspace.seats_usage_based ?? 0 }}
            </span>
          </div>
        </div>

        <!-- 空间状态与费用 KPI -->
        <div class="kpi-card meta-kpi">
          <div class="kpi-header">
            <div class="kpi-title-wrap">
              <span class="kpi-dot dot-success" />
              <span class="kpi-title">母号信息</span>
            </div>
            <el-tag v-if="currentWorkspace.is_delinquent" size="small" type="danger" effect="dark" class="kpi-tag">
              账单欠费
            </el-tag>
            <button
              v-else-if="currentWorkspace.workspace_id"
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
              <!-- seat_cost 问的是 updated_seats = entitled + 1 的差价，即"再加一个
                   席位要补多少钱"，不是空间月费。标成"空间费用"会被读成月费。 -->
              <span class="meta-label">加购单席位:</span>
              <span class="meta-val highlight-val">{{ currentWorkspace.seat_cost || '未同步' }}</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">{{ currentWorkspace.will_renew === 0 ? '到期日期:' : '续费日期:' }}</span>
              <span class="meta-val" :class="{ 'text-danger': currentWorkspace.will_renew === 0 }">
                {{ cst(currentWorkspace.renewal_date) }}
                <template v-if="currentWorkspace.will_renew === 0">（不再续订）</template>
              </span>
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
        <div v-if="!isTrashView" class="quick-tabs">
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
            :class="{ active: quickTab === 'redeem' }"
            @click="handleQuickTabChange('redeem')"
          >
            已生成兑换码
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

        <el-badge
          v-if="!isTrashView"
          :value="candidateStats.trash?.trashed_count || 0"
          :hidden="!(candidateStats.trash?.trashed_count)"
          class="trash-entry-badge"
        >
          <el-button size="small" type="danger" plain @click="goTrashView">
            <Icon icon="lucide:trash-2" class="btn-icon" />
            垃圾箱
          </el-button>
        </el-badge>
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

        <div v-if="!isTrashView" class="filter-item">
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
          <span class="filter-label">兑换码</span>
          <el-select v-model="redeemStatusFilter" clearable size="small" placeholder="全部" class="filter-select">
            <el-option label="已生成兑换码" value="has_code" />
            <el-option label="未生成兑换码" value="no_code" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">分组</span>
          <el-select v-model="groupNameFilter" clearable size="small" placeholder="全部分组" class="filter-select">
            <el-option v-for="g in candidateGroups" :key="g" :label="g" :value="g" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">标签</span>
          <el-select
            v-model="tagFilter"
            clearable
            filterable
            size="small"
            placeholder="全部标签"
            class="filter-select"
          >
            <el-option v-for="t in candidateTags" :key="t" :label="t" :value="t" />
          </el-select>
        </div>

        <el-button link size="small" type="primary" class="reset-filter-btn" @click="resetFilters">
          重置筛选
        </el-button>
      </div>

      <!-- 批量工作流操作栏 (Workflow Action Bar)：垃圾箱视图只保留恢复/删除/清空 -->
      <div v-if="isTrashView" class="action-toolbar">
        <div class="action-group-left">
          <div class="workflow-btn-group">
            <el-button size="small" @click="goCandidateView">
              <Icon icon="lucide:arrow-left" class="btn-icon" />
              返回候选管理
            </el-button>
            <el-button
              type="primary"
              plain
              size="small"
              :disabled="!selected.length"
              @click="restoreFromTrash"
            >
              <Icon icon="lucide:undo-2" class="btn-icon" />
              恢复选中成员
            </el-button>
            <el-button
              type="danger"
              plain
              size="small"
              :disabled="!selected.length"
              @click="deleteEverywhere"
            >
              <Icon icon="lucide:trash-2" class="btn-icon" />
              删除选中成员（全系统）
            </el-button>
            <el-button
              type="danger"
              size="small"
              :loading="emptyingTrash"
              @click="emptyTrash"
            >
              <Icon icon="lucide:trash" class="btn-icon" />
              清空垃圾箱
            </el-button>
          </div>
        </div>
      </div>
      <div v-else class="action-toolbar">
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

            <el-dropdown @command="runInviteStatusAction">
              <el-button type="info" plain size="small">
                <Icon icon="lucide:user-check" class="btn-icon" />
                邀请状态
                <Icon icon="lucide:chevron-down" class="btn-icon-end" />
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="manual_pending_invite">标记待接受邀请</el-dropdown-item>
                  <el-dropdown-item command="manual_joined">标记已加入</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
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
                  <el-dropdown-item command="accept_invite">接受邀请</el-dropdown-item>
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
                  <el-dropdown-item divided command="invite_csv">导出邀请 CSV</el-dropdown-item>
                  <el-dropdown-item command="push">推送到 CPA 号池</el-dropdown-item>
                  <el-dropdown-item command="export_outbound">出库并导出加密 Sub2 (自动标记出库)</el-dropdown-item>
                  <el-dropdown-item divided command="redeem_codes">生成并导出兑换码</el-dropdown-item>
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

          <el-checkbox
            v-model="cpaUseTemplate"
            :disabled="exporting || pushing"
            title="勾选后 CPA 导出按模版写入凭证级代理 proxy_url 和启停 disabled"
            class="plain-mode-check"
          >
            CPA 按模版
          </el-checkbox>
          <el-button
            size="small"
            text
            class="cpa-tpl-btn"
            title="CPA 导出模版配置"
            @click="openCpaTplDialog"
          >
            <Icon icon="lucide:settings-2" />
          </el-button>

          <el-button size="small" plain :disabled="!selected.length" @click="openTagDialog">
            <Icon icon="lucide:tag" class="btn-icon" />
            标签
          </el-button>

          <el-dropdown @command="runAssignAction">
            <el-button size="small" type="danger" plain>
              <Icon icon="lucide:more-horizontal" class="btn-icon" />
              划分/生命周期
              <Icon icon="lucide:chevron-down" class="btn-icon-end" />
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="tag_marks">标签标记</el-dropdown-item>
                <el-dropdown-item divided command="outbound">标记为已出库</el-dropdown-item>
                <el-dropdown-item v-if="tagStatusFilter === 'outbound'" command="restore_outbound">恢复出库账号</el-dropdown-item>
                <el-dropdown-item divided command="kick" style="color: var(--el-color-warning)">踢出空间成员</el-dropdown-item>
                <el-dropdown-item command="trash">移入垃圾箱</el-dropdown-item>
                <el-dropdown-item command="restore_trash">移出垃圾箱</el-dropdown-item>
                <el-dropdown-item divided command="remove" style="color: var(--el-color-danger)">移除当前空间划分</el-dropdown-item>
                <el-dropdown-item command="delete_everywhere" style="color: var(--el-color-danger)">删除账号（全系统）</el-dropdown-item>
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

      <!-- 跨页选择栏：全选按当前筛选条件拉全量，翻页由 reserve-selection 保持勾选 -->
      <div class="selection-bar">
        <span class="selection-count">
          已选 <strong>{{ selected.length }}</strong> / {{ total }}
        </span>
        <el-button link type="primary" size="small" :disabled="!total" @click="selectAllFiltered">
          全选当前筛选
        </el-button>
        <el-button link type="info" size="small" :disabled="!selected.length" @click="clearSelection">
          清空选择
        </el-button>
      </div>

      <!-- 候选人数据表格 -->
      <el-table
        ref="candidateTableRef"
        v-loading="loading"
        :data="options"
        stripe
        row-key="email"
        class="modern-candidate-table"
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="46" :selectable="isSelectableCandidate" :reserve-selection="true" />

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
                <el-tooltip v-if="row.has_redeem_code" content="点击复制兑换码" placement="top">
                  <el-tag
                    size="small"
                    type="success"
                    effect="plain"
                    class="meta-tag code-tag"
                    @click.stop="copyText(row.redeem_code)"
                  >
                    {{ row.redeem_code }}
                  </el-tag>
                </el-tooltip>
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

              <div v-if="row.cpa_proxy" class="personal-token-hint cpa-proxy-hint">
                <el-tooltip :content="row.cpa_proxy" placement="top" :show-after="200">
                  <span>CPA家宽 {{ cpaProxyLabel(row.cpa_proxy) }}</span>
                </el-tooltip>
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
                    <!-- 5 小时额度（仅在上游确实返回 18000 秒窗口时出现） -->
                    <div v-if="parseQuotaInfo(row)?.fiveHourRemain != null" class="quota-pill-stat">
                      <span class="stat-label">5h剩余</span>
                      <span
                        class="stat-val"
                        :class="{
                          'text-success': (parseQuotaInfo(row)?.fiveHourRemain || 0) >= 80,
                          'text-warning': (parseQuotaInfo(row)?.fiveHourRemain || 0) < 80 && (parseQuotaInfo(row)?.fiveHourRemain || 0) >= 30,
                          'text-danger': (parseQuotaInfo(row)?.fiveHourRemain || 0) < 30
                        }"
                      >
                        {{ parseQuotaInfo(row)?.fiveHourRemain }}%
                      </span>
                    </div>

                    <!-- 周额度 -->
                    <div v-if="parseQuotaInfo(row)?.weeklyRemain != null" class="quota-pill-stat">
                      <span class="stat-label">周剩余</span>
                      <span
                        class="stat-val"
                        :class="{
                          'text-success': (parseQuotaInfo(row)?.weeklyRemain || 0) >= 80,
                          'text-warning': (parseQuotaInfo(row)?.weeklyRemain || 0) < 80 && (parseQuotaInfo(row)?.weeklyRemain || 0) >= 30,
                          'text-danger': (parseQuotaInfo(row)?.weeklyRemain || 0) < 30
                        }"
                      >
                        {{ parseQuotaInfo(row)?.weeklyRemain }}%
                      </span>
                    </div>

                    <!-- 窗口时长缺失：只能报剩余，不标窗口 -->
                    <div v-if="parseQuotaInfo(row)?.unknownRemain != null" class="quota-pill-stat">
                      <span class="stat-label">剩余</span>
                      <span class="stat-val text-primary">{{ parseQuotaInfo(row)?.unknownRemain }}%</span>
                    </div>

                    <!-- 余额 -->
                    <div v-if="parseQuotaInfo(row)?.credits" class="quota-pill-stat">
                      <span class="stat-label">余额</span>
                      <span class="stat-val">{{ parseQuotaInfo(row)?.credits }}</span>
                    </div>

                    <!-- 额度重置券：available 有券但 applicable 为 0 时现在用了会白烧一张 -->
                    <el-tooltip
                      v-if="parseQuotaInfo(row)?.resetCredits"
                      :content="resetCreditsHint(parseQuotaInfo(row))"
                      placement="top"
                    >
                      <div
                        class="quota-pill-stat"
                        :class="{
                          'reset-credit-usable': (parseQuotaInfo(row)?.resetCredits?.applicable || 0) > 0,
                          'reset-credit-clickable': (parseQuotaInfo(row)?.resetCredits?.available || 0) > 0,
                          'is-busy': resetCreditBusyEmail === row.email
                        }"
                        @click="(parseQuotaInfo(row)?.resetCredits?.available || 0) > 0 && openResetCredit(row)"
                      >
                        <span class="stat-label">可重置</span>
                        <span
                          class="stat-val"
                          :class="{
                            'text-success': (parseQuotaInfo(row)?.resetCredits?.applicable || 0) > 0,
                            'text-warning': (parseQuotaInfo(row)?.resetCredits?.applicable || 0) === 0
                              && (parseQuotaInfo(row)?.resetCredits?.available || 0) > 0
                          }"
                        >
                          {{ parseQuotaInfo(row)?.resetCredits?.applicable ?? 0 }}
                          <template v-if="(parseQuotaInfo(row)?.resetCredits?.available || 0) !== (parseQuotaInfo(row)?.resetCredits?.applicable || 0)">
                            / {{ parseQuotaInfo(row)?.resetCredits?.available ?? 0 }}
                          </template>
                        </span>
                        <Icon
                          v-if="(parseQuotaInfo(row)?.resetCredits?.available || 0) > 0"
                          icon="lucide:rotate-ccw"
                          class="reset-credit-icon"
                        />
                      </div>
                    </el-tooltip>

                    <!-- 耗尽归因：空间池子被掏空和成员窗口打满要分得开 -->
                    <div v-if="parseQuotaInfo(row)?.reachedType" class="quota-pill-stat">
                      <span class="stat-label">耗尽</span>
                      <span class="stat-val text-danger">{{ parseQuotaInfo(row)?.reachedType }}</span>
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

        <!-- 自定义标签 -->
        <el-table-column label="标签" min-width="120">
          <template #default="{ row }">
            <div v-if="row.tags && row.tags.length" class="tag-cell">
              <el-tag
                v-for="t in row.tags.slice(0, 3)"
                :key="t"
                size="small"
                effect="plain"
                class="tag-chip"
              >
                {{ t }}
              </el-tag>
              <el-tooltip v-if="row.tags.length > 3" :content="row.tags.join('、')" placement="top">
                <span class="tag-more">+{{ row.tags.length - 3 }}</span>
              </el-tooltip>
            </div>
            <span v-else class="tag-empty">—</span>
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
          :page-sizes="PAGE_SIZE_OPTIONS"
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
        <div class="setting-switch-row automation-pause-row">
          <div class="switch-meta">
            <span class="switch-title">暂停本空间全部自动化任务</span>
            <span class="switch-desc">开启后定时额度、席位补齐、垃圾箱自动回收都暂停；手动操作不受影响</span>
          </div>
          <el-switch v-model="automationPaused" />
        </div>
        <el-tabs v-model="settingsActiveTab" class="settings-tabs">
          <!-- Tab 1: 定时额度查询 -->
          <el-tab-pane label="定时额度" name="quota">
            <div class="settings-tab-pane">
              <div class="setting-switch-row">
                <div class="switch-meta">
                  <span class="switch-title">定时额度轮询</span>
                  <span class="switch-desc">{{ quotaProxySourceDesc }}</span>
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
                <div v-if="autoPush" class="setting-group-box">
                  <div class="group-box-title">
                    <Icon icon="lucide:send" class="box-icon" />
                    <span>空间专属号池推送</span>
                  </div>
                  <el-form-item label="推送目标（可多选）">
                    <el-checkbox v-model="autoPushSub2apiEnabled">Sub2API</el-checkbox>
                    <el-checkbox v-model="autoPushCpaEnabled">CPA</el-checkbox>
                  </el-form-item>
                  <template v-if="autoPushSub2apiEnabled">
                    <el-form-item label="Sub2API 地址">
                      <el-input
                        v-model="autoPushSub2apiUrl"
                        placeholder="留空跟随全局导出配置"
                        style="width: 100%"
                      />
                    </el-form-item>
                    <el-form-item label="Sub2API API Key">
                      <el-input
                        v-model="autoPushSub2apiApiKey"
                        type="password"
                        show-password
                        placeholder="留空跟随全局导出配置"
                        style="width: 100%"
                      />
                    </el-form-item>
                    <el-form-item label="Sub2API 推送号池（分组 ID）">
                      <el-input
                        v-model="autoPushSub2apiGroupIds"
                        placeholder="留空跟随全局导出配置，如 2,5"
                        style="width: 100%"
                      />
                    </el-form-item>
                    <el-form-item>
                      <el-button size="small" :loading="pushTestRunning.sub2api" @click="testPushTarget('sub2api')">
                        测试 Sub2API 连通性
                      </el-button>
                    </el-form-item>
                  </template>
                  <template v-if="autoPushCpaEnabled">
                    <el-form-item label="CPA 推送地址">
                      <el-input
                        v-model="autoPushCpaUrl"
                        placeholder="留空跟随全局导出配置"
                        style="width: 100%"
                      />
                    </el-form-item>
                    <el-form-item label="CPA 管理密钥">
                      <el-input
                        v-model="autoPushCpaMgmtKey"
                        type="password"
                        show-password
                        placeholder="留空跟随全局导出配置"
                        style="width: 100%"
                      />
                    </el-form-item>
                    <el-form-item label="CPA 静态家宽代理池">
                      <div class="setting-switch-row" style="width: 100%">
                        <div class="switch-meta">
                          <span class="switch-desc">启用后推送的 CPA 凭证会绑定家宽代理（proxy_url），默认关闭</span>
                        </div>
                        <el-switch v-model="cpaStaticProxyEnabled" />
                      </div>
                      <el-input
                        v-if="cpaStaticProxyEnabled"
                        v-model="cpaStaticProxyPool"
                        type="textarea"
                        :rows="4"
                        placeholder="每行一个代理，如 socks5://user:pass@host:port；&#10;留空则不写凭证 proxy_url"
                        style="width: 100%; margin-top: 8px"
                      />
                      <div v-if="cpaStaticProxyEnabled" class="field-hint">
                        自动推送 CPA 时给每个凭证绑定池里租用计数最少的一条代理，写进凭证的
                        proxy_url；账号因额度耗尽/凭证失效入垃圾箱时，自动删除 CPA 里的凭证并回收计数。
                      </div>
                    </el-form-item>
                    <el-form-item>
                      <el-button size="small" :loading="pushTestRunning.cpa" @click="testPushTarget('cpa')">
                        测试 CPA 连通性
                      </el-button>
                    </el-form-item>
                  </template>
                  <div class="field-hint">
                    仅本空间生效，每项留空都跟随「自动导出」里的全局配置；
                    地址和密钥都配齐时，即使该目标全局未启用，本空间也会推送。
                  </div>
                </div>

                <div class="setting-switch-row sub-row">
                  <div class="switch-meta">
                    <span class="switch-title">Codex 席位不进入自动推送</span>
                    <span class="switch-desc">开启后 usage-based 成员获取凭证不自动推送；手动推送不受此限制</span>
                  </div>
                  <el-switch v-model="autoPushSkipCodexSeat" />
                </div>

                <div class="setting-switch-row sub-row">
                  <div class="switch-meta">
                    <span class="switch-title">额度耗尽自动兑换重置券</span>
                    <span class="switch-desc">耗尽时先兑一张券自救，额度恢复就不入垃圾箱</span>
                  </div>
                  <el-switch v-model="quotaAutoResetEnabled" />
                </div>
                <div v-if="quotaAutoResetEnabled" class="field-hint auto-reset-hint">
                  券是不可逆的消耗品，兑掉就没了。只有同时满足以下条件才会自动兑换：
                  限流窗口按「额度耗尽判定窗口」的口径确实用尽、上游标记当前可用券数（applicable）大于 0、
                  且耗尽原因不是「空间额度耗尽」（那是母号空间的池子被掏空，重置券救不了）。
                  定时轮询查到耗尽时兑一次；延迟入箱到期复查时再兑一次，兑换后额度恢复的账号不会入箱。
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
                  <div class="field-hint">标准席位和高级席位任务共用此周期，每轮开始时重新读取上游席位数。</div>
                </el-form-item>

                <el-form-item label="成员切换间隔 (秒)">
                  <el-input-number v-model="autoSeatSwitchGapSeconds" :min="0" :max="600" style="width: 100%" />
                  <div class="field-hint">
                    补齐始终是串行的：一轮内每切换一个成员就等待这么久再切下一个，与空缺席位数无关。
                    调大可降低对上游席位接口的压力，0 表示不等待。
                  </div>
                </el-form-item>

                <el-form-item label="踢出成员随机等待 (秒)">
                  <div style="display: flex; gap: 8px; align-items: center; width: 100%">
                    <el-input-number v-model="kickDelayMinSeconds" :min="0" :max="600" style="flex: 1" />
                    <span class="field-hint" style="margin: 0">至</span>
                    <el-input-number v-model="kickDelayMaxSeconds" :min="0" :max="600" style="flex: 1" />
                  </div>
                  <div class="field-hint">
                    批量踢出空间成员时，每踢完一个在该范围内随机等待再踢下一个（最后一个不等）。
                    两个都填 0 表示不等待；下限大于上限时自动交换。
                  </div>
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
                <el-form label-position="top" class="settings-form sub-form">
                  <el-form-item label="补齐目标席位数">
                    <el-input-number
                      v-model="autoStandardSeatTarget"
                      :min="0"
                      :max="Number(currentWorkspace?.seats_default_entitled) || 100000"
                      style="width: 100%"
                    />
                    <div class="field-hint">
                      0 表示补到已购席位上限（当前 {{ currentWorkspace?.seats_default_entitled ?? '未同步' }} 席）；也可填不超过上限的固定目标。
                    </div>
                  </el-form-item>
                </el-form>
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
                  <el-form-item label="补齐目标席位数">
                    <el-input-number
                      v-model="autoProliteSeatTarget"
                      :min="0"
                      :max="Number(currentWorkspace?.seats_prolite_entitled) || 100000"
                      style="width: 100%"
                    />
                    <div class="field-hint">
                      0 表示补到已购高级席位上限（当前 {{ currentWorkspace?.seats_prolite_entitled ?? '未同步' }} 席）；也可填不超过上限的固定目标。
                    </div>
                  </el-form-item>
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

              <div class="setting-group-box">
                <div class="group-box-title">
                  <Icon icon="lucide:pie-chart" class="box-icon text-primary" />
                  <span>当前空间席位补齐概览</span>
                </div>
                <div class="stat-summary-grid">
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

          <!-- Tab 3: 代理池与运行参数 -->
          <el-tab-pane label="代理与参数" name="tasks">
            <div class="settings-tab-pane">
              <div class="setting-group-box">
                <div class="group-box-title">
                  <Icon icon="lucide:globe" class="box-icon" />
                  <span>候选人专属代理池</span>
                </div>
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
                <div class="field-hint">
                  额度查询、401 重登录与凭证获取都走这里；连续 3 次传输失败的代理会自动冷却 30 分钟。
                </div>
              </div>

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
              </el-form>
            </div>
          </el-tab-pane>

          <!-- Tab 4: 垃圾箱回收 -->
          <el-tab-pane label="垃圾箱回收" name="trash">
            <div class="settings-tab-pane">
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
                  <el-form-item label="额度耗尽判定窗口">
                    <el-select v-model="trashZeroQuotaWindow" style="width: 100%">
                      <el-option label="任一窗口耗尽" value="any" />
                      <el-option label="仅看 5 小时限制" value="five_hour" />
                      <el-option label="仅看周限制" value="weekly" />
                    </el-select>
                    <div class="field-hint">
                      决定候选人达到什么条件才排队入箱。上游未返回所选窗口时（多数账号只有周限制），
                      会回退到另一个可用窗口，避免这类账号再也不被回收。
                    </div>
                  </el-form-item>
                  <el-form-item label="额度为 0 后延迟入箱 (分钟)">
                    <el-input-number v-model="trashZeroDelayMinutes" :min="1" :max="1440" style="width: 100%" />
                  </el-form-item>
                  <el-form-item label="连续入箱间隔 (秒)">
                    <el-input-number v-model="trashGapSeconds" :min="0" :max="600" style="width: 100%" />
                    <div class="field-hint">
                      入箱始终是串行的：多个候选人在相近时间到期时，一个入箱完成后等待这么久再处理下一个。
                      调大可降低对上游席位接口的压力，0 表示不等待。仅复查后放行的候选人不占用这个间隔。
                    </div>
                  </el-form-item>
                </el-form>
              </div>

              <div class="setting-group-box">
                <div class="group-box-title">
                  <Icon icon="lucide:pie-chart" class="box-icon text-primary" />
                  <span>当前空间回收概览</span>
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

    <!-- 生成兑换码弹窗 -->
    <el-dialog
      v-model="redeemDialogVisible"
      title="生成并导出兑换码"
      width="440px"
    >
      <div class="redeem-dialog-body">
        <div class="hint">
          为勾选的 {{ selected.length }} 个候选人生成兑换码（每码固定绑定一个账号，重复导出不变）。只有已持有空间凭证的账号会出码，其余跳过。
        </div>
        <el-checkbox v-model="redeemAllowSecret" class="redeem-secret-check">
          允许兑换「账号密码 + 2FA」明文凭证
        </el-checkbox>
        <div class="hint warn">
          默认关闭。开启后持码人在兑换页可直接取到该账号的明文密码和 2FA，请只对需要的码开启。
        </div>
      </div>
      <template #footer>
        <el-button @click="redeemDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="exporting" @click="exportRedeemCodes">
          生成并导出
        </el-button>
      </template>
    </el-dialog>

    <!-- CPA 导出模版配置弹窗 -->
    <el-dialog
      v-model="cpaTplVisible"
      title="CPA 导出模版配置"
      width="480px"
    >
      <el-form label-width="110px">
        <el-form-item label="凭证级代理">
          <el-input
            v-model="cpaTpl.proxy_url"
            placeholder="http://user:pass@host:port 或 socks5://host:port"
            clearable
          />
          <div class="hint">
            写进每个 CPA 凭证 JSON 的 proxy_url 字段；留空表示不配置代理
          </div>
        </el-form-item>
        <el-form-item label="启用凭证文件">
          <el-switch v-model="cpaTpl.file_enabled" />
          <span class="hint" style="margin-left: 10px">
            关闭 → 导出文件中 disabled=true（CPA 端停用该凭证）
          </span>
        </el-form-item>
      </el-form>
      <div class="hint">
        勾选导出栏「CPA 按模版」后生效：以上配置写入每个导出的 CPA 凭证文件；不勾选则按原样导出。
      </div>
      <template #footer>
        <el-button @click="cpaTplVisible = false">关闭</el-button>
        <el-button type="primary" :loading="cpaTplSaving" @click="saveCpaTpl">
          保存模版
        </el-button>
      </template>
    </el-dialog>

    <!-- 导出邀请 CSV 弹窗 -->
    <el-dialog
      v-model="inviteCsvVisible"
      title="导出邀请 CSV"
      width="420px"
    >
      <el-form label-width="80px">
        <el-form-item label="席位">
          <el-radio-group v-model="inviteCsvSeat">
            <el-radio-button
              v-for="seat in INVITE_CSV_SEATS"
              :key="seat"
              :value="seat"
            >
              {{ seat }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="格式">
          <div class="hint">
            电子邮件,角色,席位<br />
            <code>a@example.com,成员,{{ inviteCsvSeat }}</code>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="inviteCsvVisible = false">取消</el-button>
        <el-button type="primary" @click="downloadInviteCsv">下载 CSV</el-button>
      </template>
    </el-dialog>

    <!-- 标签标记弹窗 -->
    <el-dialog
      v-model="tagDialogVisible"
      :title="`标签标记（已选 ${selected.length} 个账号）`"
      width="460px"
    >
      <el-form label-width="80px">
        <el-form-item label="操作方式">
          <el-radio-group v-model="tagDialogMode">
            <el-radio-button value="add">追加</el-radio-button>
            <el-radio-button value="remove">移除</el-radio-button>
            <el-radio-button value="set">覆盖</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="标签">
          <el-select
            v-model="tagDialogTags"
            multiple
            filterable
            allow-create
            default-first-option
            :reserve-keyword="false"
            placeholder="输入新标签回车创建，或选择已有标签"
            style="width: 100%"
          >
            <el-option v-for="t in candidateTags" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <div class="hint" style="margin-left: 80px">
          追加：在现有标签上叠加；移除：去掉所选标签；覆盖：用所选标签整体替换（留空则清空标签）。
        </div>
      </el-form>
      <template #footer>
        <el-button @click="tagDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="tagDialogSaving" @click="applyCandidateTags">应用</el-button>
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

/* 值行是 align-items: baseline，el-tag 的基线会把它顶歪，单独拉回来。 */
.kpi-inline-tag {
  align-self: center;
  margin-left: 2px;
}

/* 在用段和 held 段要横排接续，所以轨道自己做 flex 容器。分段不再各自带圆角，
   接缝处才不会豁开，两端的圆头由轨道的 overflow 裁出来。 */
.kpi-bar-track {
  height: 6px;
  width: 100%;
  background: var(--el-fill-color-dark);
  border-radius: 999px;
  overflow: hidden;
  display: flex;
}

.kpi-bar-fill {
  height: 100%;
  flex: 0 0 auto;
  transition: width 0.3s ease;
}

.fill-primary { background: var(--el-color-primary); }
.fill-warning { background: var(--el-color-warning); }

/* held 用斜条纹而不是纯色：席位被占住但还没落定，跟"在用"不是一回事，
   两张卡片的主色又各不相同，所以统一走中性的 info 色。 */
.fill-held {
  background: repeating-linear-gradient(
    45deg,
    var(--el-color-info-light-3),
    var(--el-color-info-light-3) 3px,
    var(--el-color-info-light-5) 3px,
    var(--el-color-info-light-5) 6px
  );
}

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
  flex: 1;
}

.trash-entry-badge {
  flex-shrink: 0;
  margin-left: 8px;
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

.cpa-tpl-btn {
  margin-left: 2px;
  padding: 2px 4px;
  color: var(--el-text-color-secondary);
}
.cpa-tpl-btn:hover {
  color: var(--el-color-primary);
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

.code-tag {
  cursor: pointer;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: 0.5px;
}

.redeem-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.redeem-secret-check {
  height: auto;
  white-space: normal;
}
.redeem-dialog-body .hint.warn {
  color: var(--el-color-warning);
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

.cpa-proxy-hint {
  color: var(--el-color-primary);
  cursor: help;
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

/* 当前真能用的重置券：这一栏平时全是 0，有货时要一眼看见。 */
.quota-pill-stat.reset-credit-usable {
  background: var(--el-color-success-light-9);
  border-color: var(--el-color-success-light-5);
}

/* 有券才可点。兑换不可逆，所以入口要有明确的可点视觉，不能是个隐藏手势。 */
.quota-pill-stat.reset-credit-clickable {
  cursor: pointer;
}

.quota-pill-stat.reset-credit-clickable:hover {
  border-color: var(--el-color-primary);
}

.quota-pill-stat.is-busy {
  opacity: 0.6;
  pointer-events: none;
}

.reset-credit-icon {
  font-size: 11px;
  color: var(--el-text-color-secondary);
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

.tag-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.tag-chip {
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tag-more {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.tag-empty {
  color: var(--el-text-color-placeholder);
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

.selection-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 0 -4px;
  font-size: 12px;
}

.selection-count {
  color: var(--el-text-color-secondary);
}

.selection-count strong {
  color: var(--el-color-primary);
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

/* 四个标签在 440px 抽屉里按默认 padding 会溢出成左右滚动箭头，收紧一点让它们一屏排下。 */
.settings-tabs :deep(.el-tabs__item) {
  padding: 0 12px;
  font-size: 13px;
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

.automation-pause-row {
  padding: 10px 12px;
  margin-bottom: 8px;
  border-radius: var(--app-radius-md);
  border: 1px solid var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
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

/* 自动兑券的提示直接挂在开关行下面，没有 el-form-item 包裹，要自己留边距。 */
.auto-reset-hint {
  margin: 6px 2px 0;
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

<!-- ElMessageBox 渲染在 body 下，scoped 样式够不着，兑换确认框的换行只能写在
     非 scoped 块里。 -->
<style>
.reset-credit-confirm .el-message-box__message {
  white-space: pre-wrap;
}
</style>
