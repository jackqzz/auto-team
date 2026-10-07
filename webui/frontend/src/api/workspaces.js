import http from './request'

export const listWorkspaceMasters = (params) => http.get('/api/workspaces', { params })
export const importWorkspaceSessions = (text, proxy) => http.post('/api/workspaces/import', { text, proxy })
export const getWorkspaceMaster = (id) => http.get(`/api/workspaces/${id}`)
export const deleteWorkspaceMaster = (id) => http.delete(`/api/workspaces/${id}`)
export const bulkDeleteWorkspaceMasters = (ids) => http.post('/api/workspaces/bulk_delete', { ids })
export const updateWorkspaceProxy = (id, proxy) => http.post(`/api/workspaces/${id}/proxy`, { proxy })
export const updateWorkspaceSession = (id, session, proxy) =>
  http.post(`/api/workspaces/${id}/session`, { session, proxy })
export const syncWorkspace = (id) => http.post(`/api/workspaces/${id}/sync`)
export const syncWorkspaceMembers = (id) =>
  http.post(`/api/workspaces/${id}/sync-members`, null, { timeout: 300000 })
// 席位豁免额度/暂留预测：72h 滚动窗口重放席位释放台账
export const getSeatExemptions = (id) => http.get(`/api/workspaces/${id}/seat-exemptions`)
