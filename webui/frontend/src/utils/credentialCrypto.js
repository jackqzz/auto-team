// Browser counterpart of webui/credential_crypto.py.
//
// The encrypted values stay in the existing Sub2 ``password`` and
// ``totp_secret`` fields.  The workspace id is used directly as the HMAC key;
// no server-side lookup is needed when a recipient imports the file.
const ENCRYPTED_PREFIX = 'enc:v1:'
// Shared by the public relogin and candidate-management export controls so
// both pages keep the same selected credential mode in this browser.
export const PLAIN_CREDENTIAL_MODE_STORAGE_KEY = 'gpt_auto_register_plain_credential_mode'
const CONTEXT = new TextEncoder().encode('gpt-auto-register:workspace-credential:v1')
const STREAM_LABEL = new TextEncoder().encode('\u0000stream\u0000')
const AUTH_LABEL = new TextEncoder().encode('\u0000auth\u0000')
const NONCE_SIZE = 16
const TAG_SIZE = 16
const BLOCK_SIZE = 32

const text = (value) => String(value ?? '')
const utf8 = (value) => new TextEncoder().encode(String(value ?? ''))

function concatBytes(...parts) {
  const arrays = parts.map((part) => (part instanceof Uint8Array ? part : new Uint8Array(part)))
  const total = arrays.reduce((sum, part) => sum + part.length, 0)
  const output = new Uint8Array(total)
  let offset = 0
  for (const part of arrays) {
    output.set(part, offset)
    offset += part.length
  }
  return output
}

function base64UrlEncode(bytes) {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function base64UrlDecode(value) {
  const encoded = text(value)
  if (!/^[A-Za-z0-9_-]*$/.test(encoded)) throw new Error('加密凭证的 base64 格式无效')
  const normalized = encoded.replace(/-/g, '+').replace(/_/g, '/')
  if (!normalized) throw new Error('加密凭证的 base64 字段为空')
  let binary
  try {
    binary = atob(normalized + '='.repeat((4 - (normalized.length % 4)) % 4))
  } catch (_) {
    throw new Error('加密凭证的 base64 格式无效')
  }
  return Uint8Array.from(binary, (character) => character.charCodeAt(0))
}

function keyText(workspaceId) {
  const key = text(workspaceId).trim()
  if (!key) throw new Error('workspace ID 为空，无法解密凭证')
  return key
}

async function importHmacKey(workspaceId) {
  if (!globalThis.crypto?.subtle) {
    throw new Error('当前浏览器不支持 Web Crypto，无法解密凭证')
  }
  return globalThis.crypto.subtle.importKey(
    'raw',
    utf8(keyText(workspaceId)),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
}

async function hmac(key, value) {
  return new Uint8Array(await globalThis.crypto.subtle.sign('HMAC', key, value))
}

async function keystream(key, nonce, length) {
  const chunks = []
  let remaining = Number(length) || 0
  let counter = 0
  while (remaining > 0) {
    const counterBytes = new Uint8Array(4)
    new DataView(counterBytes.buffer).setUint32(0, counter, false)
    chunks.push(await hmac(key, concatBytes(CONTEXT, STREAM_LABEL, nonce, counterBytes)))
    counter += 1
    remaining -= BLOCK_SIZE
  }
  return concatBytes(...chunks).slice(0, Number(length) || 0)
}

function xorBytes(left, right) {
  const output = new Uint8Array(left.length)
  for (let index = 0; index < left.length; index += 1) output[index] = left[index] ^ right[index]
  return output
}

function equalBytes(left, right) {
  if (left.length !== right.length) return false
  let difference = 0
  for (let index = 0; index < left.length; index += 1) difference |= left[index] ^ right[index]
  return difference === 0
}

export function isEncryptedCredential(value) {
  return text(value).startsWith(ENCRYPTED_PREFIX)
}

export async function encryptCredential(value, workspaceId) {
  const clearText = text(value)
  if (!clearText || isEncryptedCredential(clearText)) return clearText
  const key = await importHmacKey(workspaceId)
  const nonce = globalThis.crypto.getRandomValues(new Uint8Array(NONCE_SIZE))
  const clear = utf8(clearText)
  const cipher = xorBytes(clear, await keystream(key, nonce, clear.length))
  const tag = (await hmac(key, concatBytes(CONTEXT, AUTH_LABEL, nonce, cipher))).slice(0, TAG_SIZE)
  return `${ENCRYPTED_PREFIX}${base64UrlEncode(nonce)}:${base64UrlEncode(cipher)}:${base64UrlEncode(tag)}`
}

export async function decryptCredential(value, workspaceId) {
  const encoded = text(value)
  if (!encoded || !isEncryptedCredential(encoded)) return encoded
  const key = await importHmacKey(workspaceId)
  const parts = encoded.slice(ENCRYPTED_PREFIX.length).split(':')
  if (parts.length !== 3) throw new Error('加密凭证格式无效')
  const nonce = base64UrlDecode(parts[0])
  const cipher = base64UrlDecode(parts[1])
  const tag = base64UrlDecode(parts[2])
  if (nonce.length !== NONCE_SIZE || tag.length !== TAG_SIZE) {
    throw new Error('加密凭证长度无效')
  }
  const expected = (await hmac(key, concatBytes(CONTEXT, AUTH_LABEL, nonce, cipher))).slice(0, TAG_SIZE)
  if (!equalBytes(tag, expected)) throw new Error('workspace ID 不正确或加密凭证已被篡改')
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(
      xorBytes(cipher, await keystream(key, nonce, cipher.length)),
    )
  } catch (error) {
    if (error?.message?.includes('workspace ID')) throw error
    throw new Error('加密凭证解密后不是有效文本')
  }
}

/**
 * Return one credential in the mode selected for export.
 *
 * A false switch guarantees plaintext (and therefore decrypts a previously
 * protected value); a true switch guarantees a protected value. Empty fields
 * remain empty in both modes.
 */
export async function credentialForExport(value, workspaceId, encryptCredentials) {
  const credential = text(value)
  if (!credential) return ''
  const key = text(workspaceId).trim()
  if (encryptCredentials) {
    if (!key) throw new Error('缺少 workspace ID，无法加密密码和 2FA')
    if (isEncryptedCredential(credential)) {
      // Verify/recover an existing protected value first, then apply the
      // selected workspace mode instead of carrying an unknown marker over.
      return encryptCredential(await decryptCredential(credential, key), key)
    }
    return encryptCredential(credential, key)
  }
  if (isEncryptedCredential(credential)) {
    if (!key) throw new Error('缺少 workspace ID，无法取消密码和 2FA 加密')
    return decryptCredential(credential, key)
  }
  return credential
}

function objectValue(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
    } catch (_) { return {} }
  }
  return {}
}

function firstValue(sources, keys) {
  for (const source of sources) {
    for (const key of keys) {
      if (source[key] !== undefined && source[key] !== null && text(source[key]).trim()) {
        return source[key]
      }
    }
  }
  return ''
}

function workspaceIdForAccount(account) {
  const raw = objectValue(account)
  const credentials = objectValue(raw.credentials)
  const data = objectValue(raw.data)
  const extra = objectValue(raw.extra)
  const notes = objectValue(raw.notes)
  const liveIdentity = objectValue(credentials.live_identity || raw.live_identity)
  const sources = [credentials, data, raw, extra, notes, liveIdentity, objectValue(notes.gpt)]
  const accessToken = firstValue(sources, ['access_token', 'accessToken', 'access-token', 'token'])
  const accessPayload = (() => {
    try {
      const part = text(accessToken).split('.')[1]
      if (!part) return {}
      const normalized = part.replace(/-/g, '+').replace(/_/g, '/')
      return JSON.parse(atob(normalized + '='.repeat((4 - normalized.length % 4) % 4))) || {}
    } catch (_) { return {} }
  })()
  const auth = accessPayload?.['https://api.openai.com/auth'] || {}
  // The export key is the workspace id, not a child account id. Prefer an
  // explicit workspace_id field even when the document also carries a
  // chatgpt_account_id/account_id claim for token routing.
  const explicitWorkspaceId = firstValue(sources, ['workspace_id', 'workspaceId', 'workspace-id'])
  const explicitAccountId = firstValue(sources, ['account_id', 'accountId'])
  return text(
    explicitWorkspaceId
      || explicitAccountId
      || firstValue(sources, ['chatgpt_account_id', 'chatgptAccountId'])
      || auth.chatgpt_account_id
      || auth.account_id
      || '',
  ).trim()
}

function emailForAccount(account) {
  const raw = objectValue(account)
  const credentials = objectValue(raw.credentials)
  return text(credentials.email || raw.email || raw.name || '').trim().toLowerCase()
}

/**
 * Decrypt only the existing login fields in one Sub2/CPA-style account.
 * The returned object is a deep clone; the uploaded file object is untouched.
 */
export async function decryptAccountCredentials(account) {
  const clone = JSON.parse(JSON.stringify(account ?? {}))
  if (clone.credentials && typeof clone.credentials === 'string') clone.credentials = objectValue(clone.credentials)
  if (clone.data && typeof clone.data === 'string') clone.data = objectValue(clone.data)
  if (clone.extra && typeof clone.extra === 'string') clone.extra = objectValue(clone.extra)
  if (clone.notes && typeof clone.notes === 'string') clone.notes = objectValue(clone.notes)
  const credentials = objectValue(clone.credentials)
  const data = objectValue(clone.data)
  const extra = objectValue(clone.extra)
  const notes = objectValue(clone.notes)
  const workspaceId = workspaceIdForAccount(clone)
  const targets = []
  const addTarget = (object, key) => {
    if (object && typeof object === 'object' && !targets.some((item) => item.object === object && item.key === key)) {
      targets.push({ object, key })
    }
  }
  for (const source of [credentials, data, clone, extra, notes]) {
    for (const key of ['password', 'passwd', 'totp_secret', 'totpSecret', 'two_factor_secret', 'twoFactorSecret', '2fa']) {
      addTarget(source, key)
    }
  }
  const gpt = objectValue(notes.gpt)
  const twoFactor = objectValue(notes.two_factor || notes.twoFactor)
  addTarget(gpt, 'password')
  addTarget(twoFactor, 'secret')
  // Ensure parsed nested objects are reflected in the clone before mutation.
  if (clone.notes && typeof clone.notes === 'object') {
    if (clone.notes.gpt && typeof clone.notes.gpt === 'string') clone.notes.gpt = gpt
    if (clone.notes.two_factor && typeof clone.notes.two_factor === 'string') clone.notes.two_factor = twoFactor
    if (clone.notes.twoFactor && typeof clone.notes.twoFactor === 'string') clone.notes.twoFactor = twoFactor
  }
  const promoteNotesCredentials = () => {
    if (!clone.password && gpt.password) clone.password = gpt.password
    if (!clone.totp_secret && twoFactor.secret) clone.totp_secret = twoFactor.secret
  }
  const encrypted = targets.filter(({ object, key }) => isEncryptedCredential(object[key]))
  if (!encrypted.length) {
    promoteNotesCredentials()
    return clone
  }
  if (!workspaceId) {
    throw new Error(`${emailForAccount(clone) || '账号'} 缺少 workspace ID，无法解密密码/2FA`)
  }
  for (const target of encrypted) target.object[target.key] = await decryptCredential(target.object[target.key], workspaceId)
  // A few older Sub2/CPA variants put the login fields only in notes. Keep
  // the nested shape, but expose decrypted values at the top level too so
  // the existing normalizer can consume them.
  promoteNotesCredentials()
  return clone
}
