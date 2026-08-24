const CODEX_CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
const SUB2API_DEFAULT_EXPIRES_IN = 863999
const OPENAI_AUTH_KEY = 'https://api.openai.com/auth'
const OPENAI_PROFILE_KEY = 'https://api.openai.com/profile'

function text(value) {
  return String(value ?? '').trim()
}

function accountFields(account) {
  const raw = account && typeof account === 'object' ? account : {}
  const credentials = raw.credentials && typeof raw.credentials === 'object' ? raw.credentials : raw
  const extra = raw.extra && typeof raw.extra === 'object' ? raw.extra : {}
  return {
    email: text(credentials.email || raw.email || raw.name || extra.email).toLowerCase(),
    accessToken: text(credentials.access_token || raw.access_token),
    refreshToken: text(credentials.refresh_token || raw.refresh_token),
    idToken: text(credentials.id_token || raw.id_token),
    workspaceId: text(
      credentials.chatgpt_account_id
      || raw.chatgpt_account_id
      || credentials.workspace_id
      || raw.workspace_id
      || credentials.account_id
      || raw.account_id
      || extra.workspace_id,
    ),
    userId: text(credentials.chatgpt_user_id || raw.chatgpt_user_id),
    clientId: text(credentials.client_id || raw.client_id),
  }
}

function decodeBase64Url(value) {
  const normalized = text(value).replace(/-/g, '+').replace(/_/g, '/')
  const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4)
  const binary = atob(padded)
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0))
  return new TextDecoder().decode(bytes)
}

export function decodeJwtPayload(token) {
  try {
    const parts = text(token).split('.')
    if (parts.length < 2) return {}
    const payload = JSON.parse(decodeBase64Url(parts[1]))
    return payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : {}
  } catch (_) {
    return {}
  }
}

function authPayload(payload) {
  const auth = payload?.[OPENAI_AUTH_KEY]
  return auth && typeof auth === 'object' && !Array.isArray(auth) ? auth : {}
}

function profilePayload(payload) {
  const profile = payload?.[OPENAI_PROFILE_KEY]
  return profile && typeof profile === 'object' && !Array.isArray(profile) ? profile : {}
}

function base64UrlEncode(value) {
  const bytes = new TextEncoder().encode(value)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function base64UrlJson(value) {
  return base64UrlEncode(JSON.stringify(value))
}

function fallbackDigest(value, length) {
  let hash = 2166136261
  for (const character of value) {
    hash ^= character.charCodeAt(0)
    hash = Math.imul(hash, 16777619)
  }
  const chunk = (hash >>> 0).toString(16).padStart(8, '0')
  return chunk.repeat(Math.ceil(length / chunk.length)).slice(0, length)
}

async function digestHex(algorithm, value, length) {
  try {
    const digest = await globalThis.crypto.subtle.digest(
      algorithm,
      new TextEncoder().encode(value),
    )
    return [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('')
      .slice(0, length)
  } catch (_) {
    return fallbackDigest(value, length)
  }
}

async function buildCompatIdToken(accessToken, fallbackEmail) {
  const payload = decodeJwtPayload(accessToken)
  if (!Object.keys(payload).length) return ''

  const auth = authPayload(payload)
  const profile = profilePayload(payload)
  const email = text(profile.email || payload.email || fallbackEmail)
  const accountId = text(auth.chatgpt_account_id || auth.account_id)
  const userId = text(auth.chatgpt_user_id || auth.user_id || payload.sub)
  const issuedAt = Number(payload.iat) || 0
  const expiresAt = Number(payload.exp) || 0
  const authTime = Number(payload.pwd_auth_time || payload.auth_time) || issuedAt
  const identity = accountId || email || userId
  const organizationId = text(auth.organization_id)
    || `org-${await digestHex('SHA-1', identity, 24)}`
  const projectId = text(auth.project_id)
    || `proj_${await digestHex('SHA-1', `${organizationId}:${accountId || userId}`, 24)}`
  const sessionId = text(payload.session_id)
    || `compat_session_${text(accountId || userId || 'unknown').replace(/-/g, '').slice(0, 24)}`

  const compatAuth = {
    chatgpt_account_id: accountId,
    chatgpt_plan_type: text(auth.chatgpt_plan_type) || 'free',
    chatgpt_subscription_active_start: auth.chatgpt_subscription_active_start,
    chatgpt_subscription_active_until: auth.chatgpt_subscription_active_until,
    chatgpt_subscription_last_checked: auth.chatgpt_subscription_last_checked,
    chatgpt_user_id: userId,
    completed_platform_onboarding: Boolean(auth.completed_platform_onboarding),
    groups: Array.isArray(auth.groups) ? auth.groups : [],
    is_org_owner: auth.is_org_owner === undefined ? true : Boolean(auth.is_org_owner),
    localhost: auth.localhost === undefined ? true : Boolean(auth.localhost),
    organization_id: organizationId,
    organizations: Array.isArray(auth.organizations) && auth.organizations.length
      ? auth.organizations
      : [{ id: organizationId, is_default: true, role: 'owner', title: 'Personal' }],
    project_id: projectId,
    user_id: text(auth.user_id || userId),
  }
  const compatPayload = {
    amr: ['pwd', 'otp', 'mfa', 'urn:openai:amr:otp_email'],
    at_hash: await digestHex('SHA-256', accessToken, 22),
    aud: [CODEX_CLIENT_ID],
    auth_provider: 'password',
    auth_time: authTime,
    email,
    email_verified: Boolean(profile.email_verified ?? payload.email_verified ?? true),
    exp: expiresAt,
    [OPENAI_AUTH_KEY]: compatAuth,
    iat: issuedAt,
    iss: text(payload.iss) || 'https://auth.openai.com',
    jti: `compat-${await digestHex('SHA-1', accessToken, 32)}`,
    name: email || 'OpenAI User',
    rat: authTime,
    sid: sessionId,
    sub: text(payload.sub || userId),
  }
  const header = { alg: 'RS256', typ: 'JWT', kid: 'compat' }
  const signature = base64UrlEncode('compat_signature_for_cpa_parsing_only')
  return `${base64UrlJson(header)}.${base64UrlJson(compatPayload)}.${signature}`
}

function formatUtc8(timestampMs) {
  const shifted = new Date(timestampMs + (8 * 60 * 60 * 1000))
  if (Number.isNaN(shifted.getTime())) return ''
  return `${shifted.toISOString().slice(0, 19)}+08:00`
}

export async function buildCpaTokenJson(account) {
  const fields = accountFields(account)
  if (!fields.accessToken) throw new Error('账号缺少 access_token')
  const payload = decodeJwtPayload(fields.accessToken)
  const auth = authPayload(payload)
  const expiresAt = Number(payload.exp)
  return {
    type: 'codex',
    email: fields.email,
    expired: Number.isInteger(expiresAt) && expiresAt > 0 ? formatUtc8(expiresAt * 1000) : '',
    id_token: fields.idToken || await buildCompatIdToken(fields.accessToken, fields.email),
    account_id: text(auth.chatgpt_account_id || fields.workspaceId),
    access_token: fields.accessToken,
    last_refresh: formatUtc8(Date.now()),
    refresh_token: fields.refreshToken,
  }
}

export function parseGroupIds(value) {
  const raw = Array.isArray(value) ? value : text(value).split(',')
  const groupIds = raw
    .map((item) => Number(text(item)))
    .filter((item) => Number.isInteger(item) && item > 0)
  return [...new Set(groupIds)].length ? [...new Set(groupIds)] : [2]
}

export function buildSub2ApiPayload(account, groupIds = [2]) {
  const fields = accountFields(account)
  if (!fields.accessToken) throw new Error('账号缺少 access_token')
  const accessPayload = decodeJwtPayload(fields.accessToken)
  const accessAuth = authPayload(accessPayload)
  const idAuth = authPayload(decodeJwtPayload(fields.idToken))
  let organizationId = text(idAuth.organization_id)
  if (!organizationId && Array.isArray(idAuth.organizations)) {
    organizationId = text(idAuth.organizations.find((item) => text(item?.id))?.id)
  }
  organizationId ||= text(accessAuth.organization_id || accessAuth.poid)
  const tokenExpiresAt = Number(accessPayload.exp)
  const expiresAt = Number.isInteger(tokenExpiresAt) && tokenExpiresAt > 0
    ? tokenExpiresAt
    : Math.floor(Date.now() / 1000) + SUB2API_DEFAULT_EXPIRES_IN

  return {
    name: fields.email,
    notes: '',
    platform: 'openai',
    type: 'oauth',
    credentials: {
      access_token: fields.accessToken,
      refresh_token: fields.refreshToken,
      expires_in: SUB2API_DEFAULT_EXPIRES_IN,
      expires_at: expiresAt,
      chatgpt_account_id: text(accessAuth.chatgpt_account_id || fields.workspaceId),
      chatgpt_user_id: text(accessAuth.chatgpt_user_id || fields.userId),
      organization_id: organizationId,
      client_id: text(fields.clientId || accessPayload.client_id) || CODEX_CLIENT_ID,
      id_token: fields.idToken,
    },
    extra: { email: fields.email },
    group_ids: parseGroupIds(groupIds),
    concurrency: 10,
    priority: 1,
    auto_pause_on_expired: true,
  }
}

function targetUrl(baseUrl, path) {
  const value = text(baseUrl)
  if (!value) throw new Error('未填写号池 URL')
  let url
  try {
    url = new URL(value)
  } catch (_) {
    throw new Error('号池 URL 格式不正确')
  }
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error('号池 URL 只支持 http:// 或 https://')
  }
  if (url.username || url.password) {
    throw new Error('请勿把访问密钥写在号池 URL 中')
  }
  if (globalThis.location?.protocol === 'https:' && url.protocol === 'http:') {
    throw new Error('HTTPS 页面不能直连 HTTP 号池，请为号池启用 HTTPS')
  }
  url.search = ''
  url.hash = ''
  return `${url.toString().replace(/\/+$/, '')}${path}`
}

async function responseMessage(response) {
  let body = ''
  try {
    body = await response.text()
    const parsed = JSON.parse(body)
    if (parsed && typeof parsed === 'object') {
      const detail = parsed.message || parsed.msg || parsed.error || parsed.detail
      if (typeof detail === 'string') return detail
      if (detail) return JSON.stringify(detail)
    }
  } catch (_) { /* 保留原始响应文本 */ }
  return body.slice(0, 300) || `HTTP ${response.status}`
}

async function directFetch(url, options, timeoutSeconds) {
  const controller = new AbortController()
  const timeout = Math.max(5, Math.min(300, Number(timeoutSeconds) || 30))
  const timer = setTimeout(() => controller.abort(), timeout * 1000)
  try {
    return await fetch(url, {
      ...options,
      mode: 'cors',
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      signal: controller.signal,
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw new Error(`浏览器直连超时（${timeout} 秒）`)
    if (error instanceof TypeError) {
      throw new Error('浏览器无法直连号池，请检查地址、HTTPS 和目标服务的 CORS 配置')
    }
    throw error
  } finally {
    clearTimeout(timer)
  }
}

function requireKey(value, label) {
  const key = text(value)
  if (!key) throw new Error(`未填写${label}`)
  return key
}

export async function pushToCpa(account, config) {
  const key = requireKey(config?.key, ' CPA 管理密钥')
  const url = targetUrl(config?.url, '/v0/management/auth-files')
  const tokenJson = await buildCpaTokenJson(account)
  const safeEmail = (tokenJson.email || 'unknown').replace(/[\\/:*?"<>|]/g, '_')
  const form = new FormData()
  form.append(
    'file',
    new Blob([JSON.stringify(tokenJson, null, 2)], { type: 'application/json' }),
    `${safeEmail}.json`,
  )
  const response = await directFetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${key}`,
      'X-Management-Key': key,
      Accept: 'application/json, text/plain, */*',
    },
    body: form,
  }, config?.timeout)
  if (!response.ok) throw new Error(`CPA 推送失败 HTTP ${response.status}: ${await responseMessage(response)}`)
  return { target: 'CPA', status: response.status, message: `${tokenJson.email || safeEmail} 推送成功` }
}

export async function pushToSub2Api(account, config) {
  const key = requireKey(config?.key, ' Sub2API API Key')
  const url = targetUrl(config?.url, '/api/v1/admin/accounts')
  const payload = buildSub2ApiPayload(account, config?.groupIds)
  const response = await directFetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/plain, */*',
      'x-api-key': key,
    },
    body: JSON.stringify(payload),
  }, config?.timeout)
  if (!response.ok) {
    throw new Error(`Sub2API 推送失败 HTTP ${response.status}: ${await responseMessage(response)}`)
  }
  return { target: 'Sub2API', status: response.status, message: `${payload.name || '账号'} 推送成功` }
}

export async function testCpaConnection(config) {
  const key = requireKey(config?.key, ' CPA 管理密钥')
  const url = targetUrl(config?.url, '/v0/management/auth-files')
  const response = await directFetch(url, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${key}`,
      'X-Management-Key': key,
      Accept: 'application/json, text/plain, */*',
    },
  }, config?.timeout)
  if (response.ok) return { message: `CPA 浏览器直连正常（HTTP ${response.status}）` }
  if (response.status === 405) return { message: 'CPA 地址可达（HTTP 405），请实际推送验证密钥' }
  throw new Error(`CPA 连接失败 HTTP ${response.status}: ${await responseMessage(response)}`)
}

export async function testSub2ApiConnection(config) {
  const key = requireKey(config?.key, ' Sub2API API Key')
  const url = targetUrl(config?.url, '/api/v1/admin/accounts')
  const response = await directFetch(url, {
    method: 'GET',
    headers: {
      Accept: 'application/json, text/plain, */*',
      'x-api-key': key,
    },
  }, config?.timeout)
  if (response.ok) return { message: `Sub2API 浏览器直连正常（HTTP ${response.status}）` }
  throw new Error(`Sub2API 连接失败 HTTP ${response.status}: ${await responseMessage(response)}`)
}
