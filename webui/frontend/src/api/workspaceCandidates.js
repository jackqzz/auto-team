import http from './request'
export const listCandidateOptions = (workspace_id, params = {}) => http.get('/api/workspace-candidates/options', { params: { workspace_id, ...params } })
export const listCandidateGroups = (workspace_id) => http.get('/api/workspace-candidates/groups', { params: { workspace_id } })
export const listCandidates = (workspace_id) => http.get('/api/workspace-candidates', { params: { workspace_id } })
export const assignCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/assign', { workspace_id, emails })
export const removeCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/remove', { workspace_id, emails })
export const updateCandidateTagStatus = (workspace_id, emails, tag_status) => http.post('/api/workspace-candidates/tag-status', { workspace_id, emails, tag_status })
// 母号批量邀请会在上游邀请后逐个复查候选状态，处理时间随邀请人数增长。
// Axios 的 timeout 单位是毫秒；每个候选人预留 5 秒，避免沿用全局 60 秒导致大批量邀请被浏览器提前中断。
export const inviteCandidates = (workspace_id, emails, seat_type = 'default') => {
  const count = Array.isArray(emails) ? emails.length : 0
  const timeout = Math.max(1, count) * 5000
  return http.post('/api/workspace-candidates/invite', { workspace_id, emails, seat_type }, { timeout })
}
export const setCandidateInviteStatus = (workspace_id, emails, join_status) => http.post('/api/workspace-candidates/invite-status', { workspace_id, emails, join_status })
export const requestJoin = (workspace_id, emails, proxy = '', proxy_pool = '', seat_type = 'default', params = {}) => http.post('/api/workspace-candidates/request-join', { workspace_id, emails, proxy, proxy_pool, seat_type, ...params })
export const checkCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/check', { workspace_id, emails })
export const updateCandidateSeat = (workspace_id, emails, seat_type) => http.post('/api/workspace-candidates/seat', { workspace_id, emails: Array.isArray(emails) ? emails : [emails], seat_type })
export const queryCandidateQuota = (workspace_id, emails, relogin_on_401 = false, proxy_pool = '', auto_push = false, params = {}) => http.post('/api/workspace-candidates/quota', { workspace_id, emails, relogin_on_401, proxy_pool, auto_push, ...params })
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
export const fetchWorkspaceCredentials = (workspace_id, emails, proxy_pool, seat_type = 'default', auto_push = false, params = {}) => http.post('/api/workspace-candidates/credentials', { workspace_id, emails, proxy_pool, seat_type, auto_push, ...params })
export const loginOnlyWorkspace = (workspace_id, emails, proxy_pool, seat_type = 'default', params = {}) => http.post('/api/workspace-candidates/login-only', { workspace_id, emails, proxy_pool, seat_type, ...params })
export const trashCandidates = (workspace_id, emails) => http.post('/api/workspace-candidates/trash', { workspace_id, emails })
export const restoreCandidatesFromTrash = (workspace_id, emails) => http.post('/api/workspace-candidates/trash/restore', { workspace_id, emails })
