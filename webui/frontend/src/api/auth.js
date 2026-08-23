import http, { setAdminToken } from './request'

export const authStatus = () => http.get('/api/auth/status')

export const loginAdmin = async (password) => {
  const res = await http.post('/api/auth/login', { password })
  if (res?.token) setAdminToken(res.token)
  return res
}

export const logoutAdmin = async () => {
  try {
    await http.post('/api/auth/logout')
  } finally {
    setAdminToken('')
  }
}
