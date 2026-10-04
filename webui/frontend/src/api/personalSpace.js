import http from './request'

// ──────────────── 个人空间（Free 账号池）────────────────

export const listPersonalCandidates = (params) =>
  http.get('/api/personal-space/candidates', { params })

export const assignPersonalCandidates = (emails) =>
  http.post('/api/personal-space/candidates', { emails })

export const removePersonalCandidates = (emails) =>
  http.post('/api/personal-space/candidates/remove', { emails })

export const getPersonalSettings = () => http.get('/api/personal-space/settings')

export const savePersonalSettings = (payload) =>
  http.post('/api/personal-space/settings', payload)

export const personalQuotaStatus = () =>
  http.get('/api/personal-space/quota-schedule/status')

export const startPersonalQuota = () =>
  http.post('/api/personal-space/quota-schedule/start')

export const stopPersonalQuota = () =>
  http.post('/api/personal-space/quota-schedule/stop')

export const refreshPersonalQuota = (emails) =>
  http.post('/api/personal-space/quota', { emails }, { timeout: 600000 })

export const reloginPersonal = (emails) =>
  http.post('/api/personal-space/relogin', { emails })

export const trashPersonal = (emails, reason = 'manual') =>
  http.post('/api/personal-space/trash', { emails, reason })

export const restorePersonal = (emails) =>
  http.post('/api/personal-space/trash/restore', { emails })

export const deletePersonalRows = (emails) =>
  http.post('/api/personal-space/trash/delete', { emails })

export const pushPersonalToCpa = (payload) =>
  http.post('/api/personal-space/push/cpa', payload, { timeout: 600000 })

export const pushPersonalToSub2api = (payload) =>
  http.post('/api/personal-space/push/sub2api', payload, { timeout: 600000 })
