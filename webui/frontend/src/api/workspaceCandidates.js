import http from './request'
export const listCandidateOptions = (workspace_id, params = {}) => http.get('/api/workspace-candidates/options', { params: { workspace_id, ...params } })
export const getCandidateStats = (workspace_id) => http.get('/api/workspace-candidates/stats', { params: { workspace_id } })
export const listCandidateGroups = (workspace_id) => http.get('/api/workspace-candidates/groups', { params: { workspace_id } })
export const listCandidates = (workspace_id) => http.get('/api/workspace-candidates', { params: { workspace_id } })
export const assignCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/assign', { workspace_id, emails })
export const removeCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/remove', { workspace_id, emails })
export const deleteCandidatesEverywhere = (workspace_id, emails) => http.post('/api/workspace-candidates/delete-everywhere', { workspace_id, emails })
export const updateCandidateTagStatus = (workspace_id, emails, tag_status) => http.post('/api/workspace-candidates/tag-status', { workspace_id, emails, tag_status })
export const kickCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/kick', { workspace_id, emails })
// 成员主动退出：与 kick 同一上游端点，但用成员自己的空间凭证 + 候选人代理，
// 需要候选人已取回空间凭证；成功后本地按已退出落库（删空间凭证/入垃圾箱）。
export const leaveCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/leave', { workspace_id, emails })
// 母号批量邀请会在上游邀请后逐个复查候选状态，处理时间随邀请人数增长。
// 后端一次邀请的时间下限 = 邀请请求 60s + 复查前等待 5s + 邀请列表复查 60s，
// 所以前端保底必须 >125s，否则浏览器会在后端还没写回状态时就中断，
// 用户看到"超时"但邀请其实已经发出去了。取 180s 留一轮重试的余量。
// Axios 的 timeout 单位是毫秒。
export const inviteCandidates = (workspace_id, emails, seat_type = 'default') => {
  const count = Array.isArray(emails) ? emails.length : 0
  const timeout = Math.max(180000, count * 8000)
  return http.post('/api/workspace-candidates/invite', { workspace_id, emails, seat_type }, { timeout })
}
export const setCandidateInviteStatus = (workspace_id, emails, join_status) => http.post('/api/workspace-candidates/invite-status', { workspace_id, emails, join_status })
export const requestJoin = (workspace_id, emails, proxy = '', proxy_pool = '', seat_type = 'default', params = {}) => http.post('/api/workspace-candidates/request-join', { workspace_id, emails, proxy, proxy_pool, seat_type, ...params })
export const checkCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/check', { workspace_id, emails })
export const updateCandidateSeat = (workspace_id, emails, seat_type) => http.post('/api/workspace-candidates/seat', { workspace_id, emails: Array.isArray(emails) ? emails : [emails], seat_type })
// 开了「耗尽自动兑券」后，单个查询请求可能串「查额度 + 兑券 + 重查」三段上游
// 调用，默认 60s 不够；前端超时中断不代表后端停下——券可能已经烧了，报超时
// 只会让人以为没兑成。与批量邀请同款保底 180s。
export const queryCandidateQuota = (workspace_id, emails, relogin_on_401 = false, proxy_pool = '', auto_push = false, params = {}) => http.post('/api/workspace-candidates/quota', { workspace_id, emails, relogin_on_401, proxy_pool, auto_push, ...params }, { timeout: 180000 })
export const startQuotaSchedule = (workspace_id, interval_minutes, relogin_on_401 = false, proxy_pool = '', auto_push = false, params = {}) => http.post('/api/workspace-candidates/quota-schedule/start', { workspace_id, interval_minutes, relogin_on_401, proxy_pool, auto_push, ...params })
export const stopQuotaSchedule = (workspace_id) => http.post('/api/workspace-candidates/quota-schedule/stop', { workspace_id })
export const quotaScheduleStatus = (workspace_id) => http.get('/api/workspace-candidates/quota-schedule', { params: { workspace_id } })
export const startAutoStandardSeatSchedule = (workspace_id) => http.post('/api/workspace-candidates/auto-standard-seat/start', { workspace_id })
export const stopAutoStandardSeatSchedule = (workspace_id) => http.post('/api/workspace-candidates/auto-standard-seat/stop', { workspace_id })
export const autoStandardSeatScheduleStatus = (workspace_id) => http.get('/api/workspace-candidates/auto-standard-seat', { params: { workspace_id } })
export const startAutoProliteSeatSchedule = (workspace_id) => http.post('/api/workspace-candidates/auto-prolite-seat/start', { workspace_id })
export const stopAutoProliteSeatSchedule = (workspace_id) => http.post('/api/workspace-candidates/auto-prolite-seat/stop', { workspace_id })
export const autoProliteSeatScheduleStatus = (workspace_id) => http.get('/api/workspace-candidates/auto-prolite-seat', { params: { workspace_id } })
export const startAutoAdvancedSeatSchedule = startAutoProliteSeatSchedule
export const stopAutoAdvancedSeatSchedule = stopAutoProliteSeatSchedule
export const autoAdvancedSeatScheduleStatus = autoProliteSeatScheduleStatus
export const listWorkspaceTaskLogs = (workspace_id, limit = 120) => http.get('/api/workspace-candidates/task-logs', { params: { workspace_id, limit } })
export const saveCandidateSettings = (payload) => http.post('/api/workspace-candidates/settings', payload)
export const testWorkspacePushTarget = (workspace_id, target) => http.post('/api/workspace-candidates/push-test', { workspace_id, target })
export const fetchWorkspaceCredentials = (workspace_id, emails, proxy_pool, seat_type = 'default', auto_push = false, params = {}) => http.post('/api/workspace-candidates/credentials', { workspace_id, emails, proxy_pool, seat_type, auto_push, ...params })
export const loginOnlyWorkspace = (workspace_id, emails, proxy_pool, seat_type = 'default', params = {}) => http.post('/api/workspace-candidates/login-only', { workspace_id, emails, proxy_pool, seat_type, ...params })
// 接受邀请 = 每个候选人一发 accounts/check + 收尾一次母号复核。成员侧请求
// 走代理可能重试，单个按 ~20s 预算，保底 120s。
export const acceptWorkspaceInvite = (workspace_id, emails, proxy_pool = '') => {
  const count = Array.isArray(emails) ? emails.length : 0
  const timeout = Math.max(120000, count * 20000)
  return http.post('/api/workspace-candidates/accept-invite', { workspace_id, emails, proxy_pool }, { timeout })
}
export const trashCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/trash', { workspace_id, emails })
// 手动移出垃圾箱会自动把账号加入垃圾箱白名单（防止刚恢复又被自动回收）。
export const restoreCandidatesFromTrash = (workspace_id, emails) => http.post('/api/workspace-candidates/trash/restore', { workspace_id, emails })
// 垃圾箱白名单：只拦自动回收（排期/到期/失效），手动入箱和踢出不受约束。
export const setCandidateTrashWhitelist = (workspace_id, emails, whitelist_enabled) => http.post('/api/workspace-candidates/trash-whitelist', { workspace_id, emails, whitelist_enabled })
export const emptyWorkspaceTrash = (workspace_id) => http.post('/api/workspace-candidates/trash/empty', { workspace_id })
export const listCandidateTags = (workspace_id) => http.get('/api/workspace-candidates/tags', { params: { workspace_id } })
export const setCandidateTags = (workspace_id, emails, tags, mode = 'add') => http.post('/api/workspace-candidates/tags', { workspace_id, emails, tags, tag_mode: mode })
// 额度重置券。list 是只读的；consume 会不可逆地烧掉一张券，credit_id 留空时
// 后端自己先 list 再取第一张可用的，所以前端确认后直接调 consume 即可。
// consume 一次串了「租代理 + 列券 + 兑换 + 额度重查」多段上游请求，单个按
// ~60s 预算不够，与批量邀请一样保底 180s——前端超时中断不代表后端停下，
// 但会让用户误以为失败而再点一次、再烧一张券。
export const listResetCredits = (workspace_id, email, proxy_pool = '') => http.get('/api/workspace-candidates/reset-credits', { params: { workspace_id, email, proxy_pool } })
export const consumeResetCredit = (workspace_id, email, credit_id = '', proxy_pool = '') => http.post('/api/workspace-candidates/reset-credits/consume', { workspace_id, email, credit_id, proxy_pool }, { timeout: 180000 })
