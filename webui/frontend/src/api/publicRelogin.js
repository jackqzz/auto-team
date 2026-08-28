import http from './request'

export const checkPublicRelogin = (payload) =>
  http.post('/api/public-relogin/check', payload, { timeout: 900000 })

export const runPublicRelogin = (payload) =>
  http.post('/api/public-relogin/relogin', payload, { timeout: 900000 })

export const getPublicReloginQueueStatus = () =>
  http.get('/api/public-relogin/queue-status')

export const refreshPublicReloginExport = (payload) =>
  http.post('/api/public-relogin/refresh-export', payload, { timeout: 120000 })
