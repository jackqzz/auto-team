<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Icon } from "@iconify/vue";
import { useRoute } from "vue-router";
import { storeToRefs } from "pinia";
import { useProxyStore } from "@/stores/proxy";
import { listWorkspaceMasters, syncWorkspace, syncWorkspaceMembers } from "@/api/workspaces";
import {
  listExportFormats,
  exportRegistered,
  pushRegisteredToCpa,
} from "@/api/register";
import {
  listCandidateOptions,
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
  listWorkspaceTaskLogs,
  saveCandidateSettings,
  trashCandidates,
  restoreCandidatesFromTrash,
} from "@/api/workspaceCandidates";
const spaces = ref([]),
  workspaceId = ref(null),
  options = ref([]),
  selected = ref([]),
  candidateTableRef = ref(null),
  loading = ref(false),
  seatType = ref("default");
const { list: proxyList } = storeToRefs(useProxyStore());
const route = useRoute();
const exportFormats = ref([]),
  exporting = ref(false),
  exportVisible = ref(false),
  exportText = ref(""),
  exportFilename = ref("export.txt"),
  exportLabel = ref("导出结果"),
  exportCount = ref(0),
  pushing = ref(false);
const taskLogs = ref([]), taskLogLoading = ref(false), taskLogAutoRefresh = ref(true), taskLogBoxRef = ref(null);
let taskLogTimer = null;
const quotaRunning = ref(false), quotaInterval = ref(30), reloginOn401 = ref(false), autoPush = ref(false), nextQuotaAt = ref(0), taskConcurrency = ref(1), taskOtpTimeout = ref(180), taskRetry = ref(1), taskCooldown = ref(0);
const trashEnabled = ref(true), trashInvalidEnabled = ref(true), trashZeroDelayMinutes = ref(60);
const seatProtectEnabled = ref(false), seatProtectThreshold = ref(8), seatProtectRefreshTime = ref("00:00"), seatProtectUsedCount = ref(0);
const autoStandardSeatEnabled = ref(false), autoStandardSeatNextAt = ref(0);
const operationStatus = ref({});
const quotaTaskRunning = ref(false);
const quotaProgress = ref({ done: 0, total: 0, active: 0, succeeded: 0, failed: 0, relogged: 0 });
const page = ref(1), pageSize = ref(100), total = ref(0);
const accountStatusFilter = ref(""), joinStatusFilter = ref(""), credentialStatusFilter = ref(""), seatTypeFilter = ref(""), trashStatusFilter = ref(""), tagStatusFilter = ref(""), groupNameFilter = ref("");
const candidateGroups = ref([]);
const settingsReady = ref(false);
const settingsWorkspaceId = ref(null);
const syncingWorkspace = ref(false);
const syncingWorkspaceMembers = ref(false);
const membershipTaskRunning = ref(false);
const candidateCheckRunning = ref(false);
const candidateMembershipBusy = computed(() => membershipTaskRunning.value || candidateCheckRunning.value);
let settingsLoadGeneration = 0;
let settingsSaveTimer = null;
function setOperation(emails, text) { const next = { ...operationStatus.value }; emails.forEach((e) => { next[e] = text }); operationStatus.value = next }
function clearOperation(emails) { const next = { ...operationStatus.value }; emails.forEach((e) => { delete next[e] }); operationStatus.value = next }
function setOneOperation(email, text) { operationStatus.value = { ...operationStatus.value, [email]: text } }
async function runRollingPool(items, concurrency, worker) {
  let cursor = 0;
  const workerCount = Math.min(Math.max(1, Number(concurrency) || 1), items.length);
  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      await worker(items[index], index);
    }
  }));
}
function activeSelectedEmails() {
  return selected.value
    .filter((row) => row.account_status !== "permanently_invalid" && row.trash_status !== "trashed")
    .map((row) => row.email)
    .filter(Boolean);
}
function accountStatusLabel(value) { return value === "permanently_invalid" ? "已永久失效" : "正常" }
function displayStatus(row) { if (operationStatus.value[row.email]) return operationStatus.value[row.email]; const status = row.display_status || "not_invited"; if (status.startsWith("quota_error_")) return `额度查询失败（${status.slice(12)}）`; return ({ not_invited: "未邀请", pending_invite: "待接受邀请", pending_request: "待处理申请", joined: "已加入", workspace_credential: "已获得空间凭证", trash_scheduled: "垃圾箱待处理", trashed: "已入垃圾箱", candidate: "未邀请" }[status] || "未邀请") }
function workspaceJoinStatusLabel(value) { if (String(value || "").startsWith("quota_error_")) return `额度查询失败（${String(value).slice(12)}）`; return ({ not_invited: "未邀请", pending_invite: "待接受邀请", pending_request: "待处理申请", joined: "已加入", join_requested: "已申请加入", approved: "已批准，待加入" }[value] || "未邀请") }
function seatLabel(value) { const v = String(value || "").toLowerCase().replace("-", "_"); return (v === "default" || v === "gpt席位" || v === "标准席位") ? "标准席位" : ((v === "usage_based" || v === "usagebased" || v === "codex席位") ? "Codex席位" : ((v === "prolite" || v === "pro_lite") ? "ProLite席位" : "—")) }
function trashStatusLabel(value) { return ({ active: "正常", scheduled: "待入箱", trashed: "已入箱" }[String(value || "active")] || "正常") }
function trashStatusHint(row) { if (!row || row.trash_status !== "scheduled" || !row.trash_due_at) return ""; return `到期 ${new Date(row.trash_due_at * 1000).toLocaleString()}` }
function tagStatusLabel(value) { return String(value || "active") === "outbound" ? "已出库" : "正常" }
function isSelectableCandidate(row) { return tagStatusFilter.value === "outbound" || String(row?.tag_status || "active") !== "outbound" }
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
const settingsVisible = ref(false);
const currentWorkspace = computed(() => spaces.value.find((x) => x.id === workspaceId.value) || null);
const currentSpaceLabel = () => { const s = currentWorkspace.value; return s ? `${s.account} · ${s.workspace_id || '无空间ID'}` : '未选择母号空间' }
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
function seatSummary(row) { return row ? `标准${row.seats_default ?? "-"}/${row.seats_default_entitled ?? "-"} · ProLite${row.seats_prolite ?? "-"}/${row.seats_prolite_entitled ?? "-"} · Codex${row.seats_usage_based ?? "-"}（已购合计${row.seats_entitled ?? "-"} 在用${row.seats_in_use ?? "-"}）` : "—" }
function quotaUpdated(row) { try { const q = JSON.parse(row.quota_json || ""); return q.error_code ? `失败（HTTP ${q.error_code}）` : (q.updated_at ? new Date(q.updated_at * 1000).toLocaleString() : "未查询") } catch (_) { return "未查询" } }
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
function nextQuotaText() { return nextQuotaAt.value ? `下次额度刷新时间：${new Date(nextQuotaAt.value * 1000).toLocaleString()}` : "" }
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
      `成员席位同步完成：更新 ${result.refreshed || 0}，未匹配 ${result.missing || 0}，剩余未知 ${result.remaining || 0}`,
    );
  } catch (e) {
    ElMessage.error(e.status === 429 ? "上游请求过于频繁，请稍后重试" : e.message);
  } finally {
    syncingWorkspaceMembers.value = false;
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
    });
    options.value = a.items || [];
    total.value = Number(a.total || 0);
  } catch (e) {
    ElMessage.error(e.message);
  } finally {
    loading.value = false;
  }
}
async function loadCandidateGroups() {
  if (!workspaceId.value) { candidateGroups.value = []; return; }
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
    if (!workspaceId.value && spaces.value.length)
      workspaceId.value = spaces.value.some((x) => x.id === requested)
        ? requested
        : spaces.value[0].id;
  } catch (e) {
    ElMessage.error(e.message);
  }
}
async function remove() {
  const emails = selected.value.map((x) => x.email);
  if (!emails.length) return;
  setOperation(emails, "移除中…"); try {
    await removeCandidates(workspaceId.value, emails);
    ElMessage.success("已移除候选划分");
    await load();
  } catch (e) {
    ElMessage.error(e.message);
  } finally { clearOperation(emails); }
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
  const emails = selected.value.filter((x) => x.assigned && x.account_status !== "permanently_invalid" && x.trash_status !== "trashed").map((x) => x.email);
  if (!emails.length)
    return ElMessage.warning("请先将账号划分为当前空间的候选人");
  membershipTaskRunning.value = true;
  setOperation(emails, "邀请中…"); try {
    const r = await inviteCandidates(workspaceId.value, emails, seatType.value);
    const states = Object.values(r.states || {});
    const confirmed = states.filter((x) => x !== "not_invited").length;
    const pending = states.filter((x) => x === "not_invited").length;
    const seatName = seatType.value === "default" ? "标准席位" : (seatType.value === "prolite" ? "ProLite席位" : "Codex席位");
    if (r.recheck_error) {
      ElMessage.warning(`邀请已提交，但状态复查受上游限流影响，请稍后执行候选状态校验`);
    } else if (pending === 0) {
      ElMessage.success(
        `邀请完成并已复查状态（${seatName}）${r.invite_error ? "，上游请求超时但状态已确认" : ""}`,
      );
    } else {
      ElMessage.warning(
        `邀请已复查：确认 ${confirmed}/${emails.length}${r.invite_error ? "（上游请求超时）" : ""}，仍有 ${pending} 个未确认`,
      );
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
  const emails = selected.value.filter((x) => x.assigned && x.account_status !== "permanently_invalid" && x.trash_status !== "trashed").map((x) => x.email);
  if (!emails.length) return ElMessage.warning("请先选择当前空间的候选人");
  if (!proxyList.value.length)
    return ElMessage.warning("全局代理池为空，请先在代理池页面配置代理");
  membershipTaskRunning.value = true;
  setOperation(emails, "申请加入中…"); try {
    const r = await requestJoin(
      workspaceId.value,
      emails,
      "",
      proxyList.value.join("\n"),
      seatType.value,
      { concurrency: taskConcurrency.value },
    );
    ElMessage.success(
      `申请完成（${seatType.value === "default" ? "标准席位" : (seatType.value === "prolite" ? "ProLite席位" : "Codex席位")}）：成功 ${r.succeeded}，失败 ${r.failed}`,
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
  let succeeded = 0, failed = 0;
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
            quota_proxy: quotaProxy,
          },
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
async function changeSeat() {
  const candidates = selected.value.filter((x) =>
    x.workspace_join_status === "joined"
  );
  if (!candidates.length) return ElMessage.warning("请选择已加入当前空间的候选人");
  const emails = candidates.map((x) => x.email);
  let succeeded = 0, skipped = 0, failed = 0;
  // 规范化席位名，用于本地比较
  const canonicalSeat = (v) => {
    const s = String(v || "").trim().toLowerCase().replace(/-/g, "_");
    if (["usage_based", "usagebased", "codex席位"].includes(s)) return "usage_based";
    if (["default", "standard", "standard_seat", "gpt席位", "标准席位"].includes(s)) return "default";
    if (["prolite", "pro_lite"].includes(s)) return "prolite";
    return s;
  };
  setOperation(emails, "排队中…");
  try {
    let requestCount = 0;
    for (const email of emails) {
      // 先检查本地席位信息，已经是目标席位则直接跳过
      const localRow = options.value.find((r) => String(r.email || "").toLowerCase() === email.toLowerCase());
      const localSeat = canonicalSeat(localRow?.seat_label || localRow?.seat_type);
      if (["default", "usage_based", "prolite"].includes(localSeat) && localSeat === seatType.value) {
        skipped += 1;
        clearOperation([email]);
        continue;
      }
      // 需要请求后端（本地席位为空或不匹配）
      if (requestCount > 0) {
        await new Promise((r) => setTimeout(r, 3000));
      }
      setOneOperation(email, "席位切换中…");
      try {
        const r = await updateCandidateSeat(workspaceId.value, [email], seatType.value);
        const item = (r.results || [])[0] || {};
        if (item.skipped) {
          skipped += 1;
        } else if (item.ok) {
          succeeded += 1;
          // 就地更新行的 seat_label
          options.value = options.value.map((row) => {
            if (String(row.email || "").toLowerCase() !== email.toLowerCase()) return row;
            return { ...row, seat_label: seatType.value, seat_type: seatType.value };
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
      `席位切换完成：已切换 ${succeeded}，跳过 ${skipped}，失败 ${failed}`,
    );
    await load();
  } catch (e) {
    ElMessage.error("席位切换失败: " + e.message);
  } finally {
    clearOperation(emails);
  }
}
async function runCandidateAction(command) {
  if (command === "check") return check();
  if (command === "quota") return quota();
  if (command === "credentials") return credentials();
  if (command === "login_only") return loginOnly();
  if (command === "seat") return changeSeat();
  if (command === "select_full_quota") return selectFullQuotaCandidates();
  if (command === "select_quota_401") return selectQuota401Candidates();
  if (command === "trash") return moveToTrash();
  if (command === "restore_trash") return restoreFromTrash();
}
async function selectCandidateRows(predicate, emptyMessage, successMessage) {
  const rows = options.value.filter((row) =>
    predicate(row)
    && row.account_status !== "permanently_invalid"
    && row.trash_status !== "trashed"
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
  // Never let a delayed watcher save controls into a different space.
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
      trash_enabled: trashEnabled.value,
      trash_invalid_enabled: trashInvalidEnabled.value,
      trash_zero_delay_minutes: trashZeroDelayMinutes.value,
      seat_protect_enabled: seatProtectEnabled.value,
      seat_protect_threshold: seatProtectThreshold.value,
      seat_protect_refresh_time: seatProtectRefreshTime.value,
      auto_standard_seat_enabled: autoStandardSeatEnabled.value,
    });
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
    // A response for a previously selected space must not touch current refs.
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
    trashEnabled.value = c.trash_enabled !== false;
    trashInvalidEnabled.value = c.trash_invalid_enabled !== false;
    trashZeroDelayMinutes.value = Number(c.trash_zero_delay_minutes || 60);
    seatProtectEnabled.value = Boolean(c.seat_protect_enabled);
    seatProtectThreshold.value = Number(c.seat_protect_threshold || 8);
    seatProtectRefreshTime.value = String(c.seat_protect_refresh_time || "00:00");
    seatProtectUsedCount.value = Number(c.seat_protect_used_count || 0);
    autoStandardSeatEnabled.value = Boolean(c.auto_standard_seat_enabled);
    try {
      const seatStatus = await autoStandardSeatScheduleStatus(targetId);
      autoStandardSeatNextAt.value = seatStatus.next_at || 0;
      autoStandardSeatEnabled.value = Boolean((seatStatus.settings || {}).auto_standard_seat_enabled);
    } catch (_) {}
    settingsWorkspaceId.value = targetId;
    loaded = true;
  } catch (_) {
    // Do not enable autosave after a failed read: doing so would overwrite a
    // valid persisted configuration with stale controls from another space.
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
      trashEnabled.value = true;
      trashInvalidEnabled.value = true;
      trashZeroDelayMinutes.value = 60;
      seatProtectEnabled.value = false;
      seatProtectThreshold.value = 8;
      seatProtectRefreshTime.value = "00:00";
      seatProtectUsedCount.value = 0;
      autoStandardSeatEnabled.value = false;
      autoStandardSeatNextAt.value = 0;
    }
  } finally {
    if (generation === settingsLoadGeneration && workspaceId.value === targetId) settingsReady.value = loaded;
  }
}
async function toggleQuotaSchedule() { try { if (!quotaRunning.value) { await stopQuotaSchedule(workspaceId.value); nextQuotaAt.value = 0; ElMessage.success('已停止定时额度查询'); } else { const r = await startQuotaSchedule(workspaceId.value, quotaInterval.value, reloginOn401.value, proxyList.value.join('\n'), autoPush.value, { concurrency: taskConcurrency.value, otp_timeout: taskOtpTimeout.value, account_retry_count: taskRetry.value, cool_down_seconds: taskCooldown.value, trash_enabled: trashEnabled.value, trash_invalid_enabled: trashInvalidEnabled.value, trash_zero_delay_minutes: trashZeroDelayMinutes.value, seat_protect_enabled: seatProtectEnabled.value, seat_protect_threshold: seatProtectThreshold.value, seat_protect_refresh_time: seatProtectRefreshTime.value }); nextQuotaAt.value = r.next_at || (Date.now()/1000 + quotaInterval.value*60); ElMessage.success(`已启动定时额度查询（每 ${quotaInterval.value} 分钟）`); } } catch (e) { quotaRunning.value = !quotaRunning.value; ElMessage.error(e.message) } }
async function toggleAutoStandardSeat() {
  try {
    if (!autoStandardSeatEnabled.value) {
      await stopAutoStandardSeatSchedule(workspaceId.value)
      autoStandardSeatNextAt.value = 0
      ElMessage.success('已停止自动补标准席位')
    } else {
      const r = await startAutoStandardSeatSchedule(workspaceId.value)
      autoStandardSeatNextAt.value = r.next_at || (Date.now()/1000 + 300)
      ElMessage.success('已启动自动补标准席位（每 5 分钟轮询）')
    }
  } catch (e) {
    autoStandardSeatEnabled.value = !autoStandardSeatEnabled.value
    ElMessage.error(e.message)
  }
}
async function credentials() {
  const emails = activeSelectedEmails();
  if (!emails.length) return ElMessage.warning("请选择候选人");
  if (!proxyList.value.length) return ElMessage.warning("全局代理池为空");
  setOperation(emails, "凭证获取中…"); try {
    const result = await fetchWorkspaceCredentials(
      workspaceId.value,
      emails,
      proxyList.value.join("\n"),
      seatType.value,
      autoPush.value,
      { concurrency: taskConcurrency.value, otp_timeout: taskOtpTimeout.value, account_retry_count: taskRetry.value, cool_down_seconds: taskCooldown.value },
    );
    const skipped = result.skipped || 0; const eligible = result.eligible || 0;
    if (skipped) ElMessage.warning(`空间凭证任务：跳过 ${skipped} 个不满足条件的候选人，已提交 ${eligible} 个（待接受邀请的成员会在登录时自动接受邀请）`);
    else ElMessage.success(`空间凭证获取任务已启动：已提交 ${eligible} 个，请在运行记录查看`);
  } catch (e) {
    ElMessage.error(e.message);
  } finally { clearOperation(emails); }
}
async function loginOnly() {
  const emails = activeSelectedEmails();
  if (!emails.length) return ElMessage.warning("请选择候选人");
  if (!proxyList.value.length) return ElMessage.warning("全局代理池为空");
  setOperation(emails, "仅登录中…"); try {
    const result = await loginOnlyWorkspace(
      workspaceId.value,
      emails,
      proxyList.value.join("\n"),
      seatType.value,
      { concurrency: taskConcurrency.value, otp_timeout: taskOtpTimeout.value, account_retry_count: taskRetry.value, cool_down_seconds: taskCooldown.value },
    );
    const skipped = result.skipped || 0; const eligible = result.eligible || 0;
    if (skipped) ElMessage.warning(`仅登录任务：跳过 ${skipped} 个不满足条件的候选人，已提交 ${eligible} 个`);
    else ElMessage.success(`仅登录任务已启动：已提交 ${eligible} 个（跳过 OAuth），请在运行记录查看`);
  } catch (e) {
    ElMessage.error(e.message);
  } finally { clearOperation(emails); }
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
  const blob =
    data instanceof Blob
      ? data
      : new Blob([data], { type: mime || "application/octet-stream" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
async function doExport(fmt) {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  exporting.value = true;
  try {
    const r = await exportRegistered({
      format: fmt.id,
      emails,
      workspace_id: workspaceId.value,
      proxy_pool: proxyList.value.join("\n"),
    });
    if (r.mode === "download") {
      saveBlob(b64ToBytes(r.b64), r.filename, r.mime);
      ElMessage.success(
        `已下载 ${r.filename}（${r.count || emails.length} 个号）`,
      );
      return;
    }
    exportText.value = r.text || "";
    exportFilename.value = r.filename || "export.txt";
    exportLabel.value = r.label || fmt.label;
    exportCount.value = r.count || emails.length;
    exportVisible.value = true;
  } catch (e) {
    ElMessage.error("导出失败: " + e.message);
  } finally {
    exporting.value = false;
  }
}
async function push() {
  const emails = selected.value.map((x) => x.email).filter(Boolean);
  if (!emails.length) return ElMessage.warning("请选择候选人");
  const missing = selected.value.filter((x) => !x.has_access_token).length;
  if (missing)
    ElMessage.warning(`${missing} 个候选人缺少空间凭证，推送接口可能跳过`);
  pushing.value = true; setOperation(emails, "推送中…");
  try {
    const r = await pushRegisteredToCpa(
      emails,
      proxyList.value.join("\n"),
      workspaceId.value,
    );
    ElMessage.success(r.message || `推送完成（${emails.length} 个）`);
  } catch (e) {
    ElMessage.error("推送失败: " + e.message);
  } finally {
    clearOperation(emails);
    pushing.value = false;
  }
}
watch(workspaceId, async (id) => {
  // Invalidate any in-flight response from the old space before loading the new one.
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
watch([quotaInterval, reloginOn401, autoPush, taskConcurrency, taskOtpTimeout, taskRetry, taskCooldown, trashEnabled, trashInvalidEnabled, trashZeroDelayMinutes, seatProtectEnabled, seatProtectThreshold, seatProtectRefreshTime], queueSpaceSettingsSave);
watch([autoStandardSeatEnabled], queueSpaceSettingsSave);
watch([accountStatusFilter, joinStatusFilter, credentialStatusFilter, seatTypeFilter, trashStatusFilter, tagStatusFilter, groupNameFilter], () => {
  page.value = 1;
  selected.value = [];
  if (workspaceId.value) load();
});
watch([page, pageSize], () => {
  selected.value = [];
  if (workspaceId.value) load();
});
watch(taskLogAutoRefresh, async () => {
  if (!workspaceId.value) return;
  await loadTaskLogs(true);
  await startTaskLogPolling();
});
onActivated(async () => {
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
  <div class="page">
    <el-card shadow="never"
      ><template #header
        ><div class="header-row">
          <div class="header-left">
            <span class="section-title" style="margin: 0">候选管理</span
            ><span class="hint">系统内划分不等于已加入 Team 空间</span>
          </div>
          <el-button
            circle
            plain
            size="small"
            class="settings-icon-btn"
            title="空间任务设置"
            aria-label="空间任务设置"
            @click="settingsVisible = true"
          >
            <Icon icon="mdi:circle-outline" />
          </el-button>
        </div></template
      >
      <el-form inline
        ><el-form-item label="母号空间"
          ><el-select
            v-model="workspaceId"
            style="width: 360px"
            placeholder="选择母号空间"
            ><el-option
              v-for="s in spaces"
              :key="s.id"
              :value="s.id"
              :label="`${s.account} · ${s.workspace_id || '无空间ID'}`" /></el-select></el-form-item
        ><el-form-item label="席位类型"
          ><el-select v-model="seatType" style="width: 180px"
            ><el-option label="标准席位" value="default" /><el-option
              label="Codex席位"
              value="usage_based" /><el-option
              label="ProLite席位"
              value="prolite" /></el-select></el-form-item
        ><el-form-item
          ><span class="hint"
            >同时用于母号邀请和子号申请；子号申请使用全局代理池（当前
            {{ proxyList.length }} 条）</span
          ></el-form-item
        ></el-form
      >
      <div v-if="currentWorkspace" class="workspace-summary">
        <div class="workspace-summary-main">
          <span class="summary-title">母号概览</span>
          <span class="mono">{{ currentWorkspace.account }}</span>
          <el-tag size="small" type="info">{{ currentWorkspace.workspace_id || '未提取' }}</el-tag>
          <span>席位 {{ seatSummary(currentWorkspace) }}</span>
          <span>费用 {{ currentWorkspace.seat_cost || '未同步' }}</span>
          <span>到期 {{ cst(currentWorkspace.renewal_date) }}</span>
        </div>
        <el-button size="small" :loading="syncingWorkspace" :disabled="syncingWorkspaceMembers" @click="syncCurrentWorkspace">
          <el-icon><Refresh /></el-icon>同步席位统计
        </el-button>
        <el-button size="small" :loading="syncingWorkspaceMembers" :disabled="syncingWorkspace" @click="syncCurrentWorkspaceMembers">
          同步成员席位
        </el-button>
      </div>
      <el-form inline style="margin-bottom: 8px">
        <el-form-item label="账号状态">
          <el-select v-model="accountStatusFilter" clearable style="width: 150px" placeholder="全部">
            <el-option label="正常" value="active" /><el-option label="已永久失效" value="permanently_invalid" />
          </el-select>
        </el-form-item>
        <el-form-item label="空间加入状态">
          <el-select v-model="joinStatusFilter" clearable style="width: 170px" placeholder="全部">
            <el-option label="未邀请" value="not_invited" /><el-option label="待接受邀请" value="pending_invite" />
            <el-option label="待处理申请" value="pending_request" /><el-option label="已申请加入" value="join_requested" />
            <el-option label="已批准，待加入" value="approved" /><el-option label="已加入" value="joined" />
          </el-select>
        </el-form-item>
        <el-form-item label="凭证状态">
          <el-select v-model="credentialStatusFilter" clearable style="width: 170px" placeholder="全部">
            <el-option label="已获得 Team 凭证" value="workspace_credential" /><el-option label="仅 Personal 凭证" value="personal_credential" /><el-option label="无凭证" value="none" /><el-option label="凭证不可用" value="unavailable" />
          </el-select>
        </el-form-item>
        <el-form-item label="席位类型">
          <el-select v-model="seatTypeFilter" clearable style="width: 150px" placeholder="全部">
            <el-option label="标准席位" value="default" /><el-option label="Codex席位" value="usage_based" /><el-option label="ProLite席位" value="prolite" /><el-option label="未设置" value="none" />
          </el-select>
        </el-form-item>
        <el-form-item label="垃圾箱">
          <el-select v-model="trashStatusFilter" clearable style="width: 150px" placeholder="全部">
            <el-option label="正常" value="active" /><el-option label="待入箱" value="scheduled" /><el-option label="已入箱" value="trashed" />
          </el-select>
        </el-form-item>
        <el-form-item label="出库">
          <el-select v-model="tagStatusFilter" clearable style="width: 150px" placeholder="默认隐藏">
            <el-option label="正常" value="active" /><el-option label="已出库" value="outbound" />
          </el-select>
        </el-form-item>
        <el-form-item label="分组">
          <el-select v-model="groupNameFilter" clearable style="width: 170px" placeholder="全部">
            <el-option v-for="g in candidateGroups" :key="g" :label="g" :value="g" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="tool-row">
        <div class="tool-left">
          <el-dropdown :disabled="candidateMembershipBusy" @command="runMembershipAction"
            ><el-button type="warning" :loading="membershipTaskRunning"
              >空间加入操作<i class="el-icon-arrow-down el-icon--right" /></el-button
            ><template #dropdown
              ><el-dropdown-menu
                ><el-dropdown-item command="join">子号申请加入</el-dropdown-item
                ><el-dropdown-item command="invite">母号批量邀请</el-dropdown-item
              ></el-dropdown-menu></template
            ></el-dropdown
          ><el-dropdown @command="runInviteStatusAction"
            ><el-button type="info"
              >邀请状态<i class="el-icon-arrow-down el-icon--right" /></el-button
            ><template #dropdown
              ><el-dropdown-menu
                ><el-dropdown-item command="manual_pending_invite">标记待接受邀请</el-dropdown-item
                ><el-dropdown-item command="manual_joined">标记已加入</el-dropdown-item
              ></el-dropdown-menu></template
            ></el-dropdown
          ><el-dropdown :disabled="candidateMembershipBusy" @command="runCandidateAction"
          ><el-button type="primary" :loading="candidateCheckRunning"
            >候选操作<i class="el-icon-arrow-down el-icon--right" /></el-button
          ><template #dropdown
            ><el-dropdown-menu
              ><el-dropdown-item command="check">校验候选状态</el-dropdown-item
              ><el-dropdown-item command="select_full_quota">选取额度100%</el-dropdown-item
              ><el-dropdown-item command="select_quota_401">选取额度401</el-dropdown-item
              ><el-dropdown-item command="quota">查询额度</el-dropdown-item
              ><el-dropdown-item command="credentials">空间凭证获取</el-dropdown-item
              ><el-dropdown-item command="login_only">仅登录空间</el-dropdown-item
              ><el-dropdown-item command="seat">切换成员席位</el-dropdown-item
              ><el-dropdown-item command="trash">一键手动入箱</el-dropdown-item
              ><el-dropdown-item command="restore_trash">移出垃圾箱</el-dropdown-item
            ></el-dropdown-menu></template
          ></el-dropdown
        ><el-dropdown
          @command="runExportAction"
          @visible-change="(v) => v && loadExportFormats()"
          ><el-button type="success" :loading="exporting || pushing"
            >导出候选人<i
              class="el-icon-arrow-down el-icon--right" /></el-button
          ><template #dropdown
            ><el-dropdown-menu
              ><el-dropdown-item
                v-for="fmt in exportFormats"
                :key="fmt.id"
                :command="fmt"
                >{{ fmt.label }}</el-dropdown-item
              ><el-dropdown-item command="push">推送到 CPA</el-dropdown-item
              ><el-dropdown-item v-if="!exportFormats.length" disabled
                >加载中...</el-dropdown-item
              ></el-dropdown-menu
            ></template
          ></el-dropdown
          >
        </div>
        <div class="tool-right">
          <el-dropdown @command="runAssignAction">
            <el-button>移除/垃圾箱<i class="el-icon-arrow-down el-icon--right" /></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="remove">移除候选划分</el-dropdown-item>
                <el-dropdown-item command="trash">一键手动入箱</el-dropdown-item>
                <el-dropdown-item command="restore_trash">移出垃圾箱</el-dropdown-item>
                <el-dropdown-item command="outbound">标记为出库</el-dropdown-item>
                <el-dropdown-item v-if="tagStatusFilter === 'outbound'" command="restore_outbound">恢复为正常</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
      <div v-if="quotaTaskRunning" class="quota-progress">
        <div class="quota-progress-head">
          <span>额度查询{{ reloginOn401 ? " / 401重登录" : "" }}</span>
          <span>
            已完成 {{ quotaProgress.done }}/{{ quotaProgress.total }} ·
            处理中 {{ quotaProgress.active }} ·
            排队 {{ Math.max(0, quotaProgress.total - quotaProgress.done - quotaProgress.active) }}
          </span>
        </div>
        <el-progress
          :percentage="quotaProgress.total ? Math.round((quotaProgress.done / quotaProgress.total) * 100) : 0"
          :format="() => `${quotaProgress.done}/${quotaProgress.total}`"
        />
      </div>
      <div v-if="quotaRunning" class="quota-next-hint">{{ nextQuotaText() }}</div>
      <el-drawer v-model="settingsVisible" :title="currentSpaceLabel()" direction="rtl" size="360px" @close="queueSpaceSettingsSave">
        <div class="settings-panel">
          <div class="setting-row"><span>定时额度查询</span><el-switch v-model="quotaRunning" @change="toggleQuotaSchedule" /></div>
          <div class="hint">只查询当前空间已获得 Team 凭证的候选人；每个候选都从全局代理池租取代理，不使用母号专属出口。</div>
          <el-form label-position="top" style="margin-top:20px"><el-form-item label="查询间隔"><el-select v-model="quotaInterval" style="width:100%"><el-option :value="15" label="每 15 分钟" /><el-option :value="30" label="每 30 分钟" /><el-option :value="60" label="每 1 小时" /></el-select></el-form-item></el-form>
          <div class="setting-row"><span>401 自动重新登录</span><el-switch v-model="reloginOn401" /></div>
          <div class="hint">额度接口返回 401 时，将该账号重新投入当前 Team 空间登录。</div>
          <div class="setting-row" style="margin-top:20px"><span>获取凭证后自动推送</span><el-switch v-model="autoPush" /></div>
          <div class="hint">手动或自动获取 Team 凭证后，自动推送到已配置的号池。</div>
          <el-divider>垃圾箱设置</el-divider>
          <div class="setting-row"><span>开启垃圾箱自动移除</span><el-switch v-model="trashEnabled" /></div>
          <div class="setting-row"><span>失效账号自动入箱</span><el-switch v-model="trashInvalidEnabled" /></div>
          <el-form label-position="top" style="margin-top:20px"><el-form-item label="额度 0 后延迟入箱（分钟）"><el-input-number v-model="trashZeroDelayMinutes" :min="1" :max="1440" /></el-form-item></el-form>
          <el-divider>空间任务设置</el-divider>
          <el-form label-position="top"><el-form-item label="并发数"><el-input-number v-model="taskConcurrency" :min="1" :max="20" /></el-form-item><el-form-item label="OTP 等待秒数"><el-input-number v-model="taskOtpTimeout" :min="10" :max="600" /></el-form-item><el-form-item label="账号重试次数"><el-input-number v-model="taskRetry" :min="1" :max="5" /></el-form-item><el-form-item label="任务间冷却秒数"><el-input-number v-model="taskCooldown" :min="0" :max="3600" /></el-form-item></el-form>
          <el-divider>席位保护设置</el-divider>
          <div class="setting-row"><span>开启席位保护</span><el-switch v-model="seatProtectEnabled" /></div>
          <div class="hint">仅限制 Usage-based → 标准席位切换；标准席位 → Usage-based 不受影响。</div>
          <div class="hint">当前周期已用 {{ seatProtectUsedCount }} / {{ seatProtectThreshold }}</div>
          <el-form label-position="top" style="margin-top:20px"><el-form-item label="保护阈值"><el-input-number v-model="seatProtectThreshold" :min="1" :max="1000" /></el-form-item><el-form-item label="阈值刷新时间"><el-time-picker v-model="seatProtectRefreshTime" format="HH:mm" value-format="HH:mm" placeholder="每天刷新时间" style="width:100%" /></el-form-item></el-form>
          <el-divider>自动补标准席位</el-divider>
          <div class="setting-row"><span>开启自动补标准席位</span><el-switch v-model="autoStandardSeatEnabled" @change="toggleAutoStandardSeat" /></div>
          <div class="hint">每 5 分钟轮询一次 seats_type_counts，按缺口串行补齐标准席位，并在补齐后自动触发 Team 凭证获取。</div>
          <div v-if="autoStandardSeatEnabled && autoStandardSeatNextAt" class="hint">下次轮询：{{ new Date(autoStandardSeatNextAt * 1000).toLocaleString() }}</div>
        </div>
      </el-drawer>
      <el-table
        ref="candidateTableRef"
        v-loading="loading"
        :data="options"
        stripe
        @selection-change="(v) => (selected = v)"
        ><el-table-column type="selection" width="44" :selectable="isSelectableCandidate" /><el-table-column
          prop="email"
          label="候选账号"
          min-width="260" /><el-table-column
          prop="group_name"
          label="分组"
          width="140" /><el-table-column label="账号状态" width="180"
          ><template #default="{ row }">{{ accountStatusLabel(row.account_status) }}</template></el-table-column
        ><el-table-column label="垃圾箱" width="160"
          ><template #default="{ row }"
            ><el-tag :type="row.trash_status === 'trashed' ? 'danger' : (row.trash_status === 'scheduled' ? 'warning' : 'success')">{{
              trashStatusLabel(row.trash_status)
            }}</el-tag
            ><div v-if="trashStatusHint(row)" class="hint">{{ trashStatusHint(row) }}</div></template
          ></el-table-column
        ><el-table-column label="出库" width="110"
          ><template #default="{ row }"
            ><el-tag :type="row.tag_status === 'outbound' ? 'warning' : 'success'">{{ tagStatusLabel(row.tag_status) }}</el-tag></template
          ></el-table-column
        ><el-table-column label="状态" width="170"
          ><template #default="{ row }"
            ><el-tag :type="operationStatus[row.email] ? 'warning' : (row.display_status === 'trashed' ? 'danger' : row.display_status === 'trash_scheduled' ? 'warning' : 'info')">{{ displayStatus(row) }}</el-tag></template
          ></el-table-column
        ><el-table-column label="空间加入状态" width="150"
          ><template #default="{ row }">{{ workspaceJoinStatusLabel(row.workspace_join_status) }}</template></el-table-column
        ><el-table-column label="Personal 凭证" width="130"
          ><template #default="{ row }">{{
            row.credential_status === "unavailable" ? "不可用" : (row.has_access_token ? "已具备" : "缺失")
          }}</template></el-table-column
        ><el-table-column label="当前空间 Team 凭证" width="170"
          ><template #default="{ row }"
            ><el-tag
              :type="row.has_workspace_access_token ? 'success' : 'info'"
              >{{
                row.credential_status === "unavailable" ? "不可用" : (row.has_workspace_access_token ? "已获得空间凭证" : "未获取")
              }}</el-tag
            ></template
          ></el-table-column
        ><el-table-column label="席位类型" width="130"><template #default="{ row }">{{ seatLabel(row.seat_label || row.seat_type) }}</template></el-table-column
        ><el-table-column label="额度 / 限制" min-width="240"
          ><template #default="{ row }"><span v-if="row.quota_json">{{ (() => { try { const q = JSON.parse(row.quota_json); if (q.error_code) return `查询失败（HTTP ${q.error_code}）`; const p = q.primary?.used_percent != null ? `5h剩余 ${Math.max(0, 100 - q.primary.used_percent)}%` : ''; const s = q.secondary?.used_percent != null ? `周剩余 ${Math.max(0, 100 - q.secondary.used_percent)}%` : ''; return [q.credits_balance ? `余额 ${q.credits_balance}` : '', p, s].filter(Boolean).join(' · ') || '暂无额度数据' } catch (_) { return '暂无额度数据' } })() }}</span><span v-else class="hint">未查询</span></template
          ></el-table-column
        ><el-table-column label="上次额度刷新" width="170"><template #default="{ row }">{{ row.has_workspace_access_token ? quotaUpdated(row) : "—" }}</template></el-table-column
        ><template #empty
          ><el-empty
            description="当前母号空间暂无候选成员，请先在注册结果页面划分账号"
            :image-size="70" /></template></el-table
      ><div style="display:flex;justify-content:center;margin:14px 0">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[100, 200, 500, 1000]"
          :total="total"
          layout="sizes, prev, pager, next, total"
          background
        />
      </div>
      <el-card shadow="never" class="task-log-card">
        <template #header>
          <div class="task-log-head">
            <div class="task-log-title">
              <span class="section-title" style="margin: 0">空间任务日志</span>
              <span class="hint">仅展示当前母号空间相关日志</span>
            </div>
            <div class="task-log-actions">
              <el-switch v-model="taskLogAutoRefresh" active-text="自动刷新" />
              <el-button size="small" :loading="taskLogLoading" @click="loadTaskLogs(false)">
                <el-icon><Refresh /></el-icon>刷新
              </el-button>
            </div>
          </div>
        </template>
        <div ref="taskLogBoxRef" class="task-log-box">
          <div v-if="!taskLogs.length" class="task-log-empty">暂无空间任务日志</div>
          <div
            v-for="item in taskLogs"
            :key="item.id"
            class="task-log-line"
            :class="`lv-${String(item.level || '').toLowerCase()}`"
          >
            <span class="task-log-time">[{{ taskLogTime(item.ts) }}]</span>
            <span class="task-log-level">{{ item.level }}</span>
            <span class="task-log-text">{{ item.text }}</span>
          </div>
        </div>
      </el-card>
      <el-dialog
        v-model="exportVisible"
        :title="`${exportLabel}（${exportCount} 个）`"
        width="700px"
      >
        <el-input v-model="exportText" type="textarea" :rows="16" readonly />
        <template #footer>
          <el-button @click="exportVisible = false">关闭</el-button>
          <el-button
            type="primary"
            @click="saveBlob(exportText, exportFilename, 'text/plain;charset=utf-8')"
          >
            下载
          </el-button>
        </template>
      </el-dialog>
    </el-card>
  </div>
</template>
<style scoped>
.tool-row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.tool-left,.tool-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.setting-row{display:flex;align-items:center;justify-content:space-between;font-weight:600;margin-bottom:8px}.settings-panel .hint{line-height:1.6;color:var(--el-text-color-secondary)}
.quota-next-hint{font-size:12px;color:var(--el-text-color-secondary);margin:-4px 0 10px 2px}
.quota-progress{margin:0 0 12px;padding:10px 12px;border:1px solid var(--el-border-color-lighter);background:var(--el-fill-color-lighter);border-radius:6px}
.quota-progress-head{display:flex;justify-content:space-between;gap:12px;margin-bottom:8px;font-size:13px;color:var(--el-text-color-regular)}
.header-row { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.header-left { display:flex; align-items:center; gap:12px; min-width:0; }
.workspace-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 0 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
}
.workspace-summary-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.summary-title {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.settings-icon-btn :deep(svg) {
  width: 16px;
  height: 16px;
}
.task-log-card {
  margin-top: 14px;
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
  gap: 12px;
  min-width: 0;
}
.task-log-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.task-log-box {
  max-height: 320px;
  overflow: auto;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
  font-family: var(--el-font-family-mono, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace);
  font-size: 12px;
  line-height: 1.6;
}
.task-log-empty {
  color: var(--el-text-color-secondary);
  padding: 4px 0;
}
.task-log-line {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  word-break: break-word;
  white-space: pre-wrap;
}
.task-log-time,
.task-log-level {
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
}
.task-log-level {
  min-width: 56px;
}
.task-log-text {
  flex: 1;
  min-width: 0;
}
.task-log-line.lv-warning .task-log-level,
.task-log-line.lv-warning .task-log-text { color: var(--el-color-warning); }
.task-log-line.lv-error .task-log-level,
.task-log-line.lv-error .task-log-text { color: var(--el-color-danger); }
.task-log-line.lv-info .task-log-level,
.task-log-line.lv-info .task-log-text { color: var(--el-color-primary); }
</style>
