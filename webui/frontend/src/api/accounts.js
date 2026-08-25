import http from './request'

// ──────────────── 统计 ────────────────
export const getStats = () => http.get('/api/stats')

// ──────────────── 号池 accounts ────────────────
// kind = 邮箱来源（outlook / ...）。留空后端会按段数猜，
// 但 Outlook 和 Gmail 都是 4 段猜不出来，所以页面上必选。
export const importAccounts = (text, kind = '', groupName = undefined) =>
  http.post('/api/import', { text, kind, group_name: groupName })

export const listAccounts = (params) =>
  http.get('/api/accounts', { params }) // { status, limit, offset, kind, group_name }

export const listAccountGroups = () => http.get('/api/accounts/groups')

export const setAccountsGroup = (emails, groupName) =>
  http.post('/api/accounts/set_group', { emails, group_name: groupName })

// 邮箱列表手动录入 OpenAI 登录密码；已注册账号写入 registered，
// 尚未注册账号写回号池，供后续仅登录/补齐 2FA 使用。
export const updateAccountPassword = (email, password) =>
  http.post('/api/accounts/update_password', { email, password })

export const createAccountGroup = (name) => http.post('/api/accounts/groups', { name })

export const renameAccountGroup = (oldName, newName) =>
  http.post('/api/accounts/groups/rename', { old_name: oldName, new_name: newName })

export const deleteAccountGroup = (name) =>
  http.delete(`/api/accounts/groups/${encodeURIComponent(name)}`)

export const deleteAccount = (email) =>
  http.delete(`/api/accounts/${encodeURIComponent(email)}`)

export const bulkDeleteAccounts = (payload) =>
  http.post('/api/accounts/bulk_delete', payload) // { status } 或 { emails }

export const resetFailed = () => http.post('/api/accounts/reset_failed')

export const resetAccount = (email) =>
  http.post(`/api/accounts/reset/${encodeURIComponent(email)}`)

export const bulkResetAccounts = (emails) =>
  http.post('/api/accounts/bulk_reset', { emails })

export const releaseStale = () => http.post('/api/accounts/release_stale')
