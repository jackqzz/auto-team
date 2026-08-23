import http from './request'

export const checkPublicRelogin = (payload) =>
  http.post('/api/public-relogin/check', payload, { timeout: 180000 })

export const runPublicRelogin = (payload) =>
  http.post('/api/public-relogin/relogin', payload, { timeout: 900000 })
