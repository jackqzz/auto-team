import http from './request'

// 生成/导出兑换码（管理端，候选管理页用）。同一账号重复导出返回同一个码；
// 没有空间凭证的账号会被跳过，响应里带 skipped 列表。
// allow_secret=true 时该码额外允许兑换明文 账号----密码----2FA（默认关闭）。
export const generateRedeemCodes = (workspace_id, emails, allow_secret = false) =>
  http.post('/api/workspace-candidates/redeem-codes', { workspace_id, emails, allow_secret })

// 兑换管理页（管理端）
export const listRedeemCodes = (workspace_id = 0) =>
  http.get('/api/redeem-codes', { params: { workspace_id } })
export const deleteRedeemCodes = (codes) =>
  http.post('/api/redeem-codes/delete', { codes })

// 公开兑换（免登）：凭码下载绑定账号的加密凭证，format: sub2api / cpa。
// codes 传单个字符串或数组（批量），响应里 failed 列出逐码失败原因。
export const redeemCode = (codes, format = 'sub2api') =>
  http.post('/api/redeem', { codes: Array.isArray(codes) ? codes : [codes], format })
