/**
 * utils/api.js —— 微信云托管调用层（唯一出口，页面不要直接碰 wx.cloud）
 *
 * 三条硬约束（改之前先看官方文档）：
 *  1. 只用 wx.cloud.callContainer，不用 wx.request；
 *  2. header 必须带 X-WX-SERVICE = 服务名；config.env = 云托管「环境ID」；
 *  3. 单次请求 timeout 绝对不能超过 15000ms。
 *
 * 另外处理两件事：
 *  - 初始化竞态：init 还没完成时报 "Cloud API isn't enabled"，等 300ms 重试，最多 3 次；
 *  - 小程序切后台 5 秒后请求会被系统杀掉（fail interrupted），所以轮询里单次失败不判死。
 */
const config = require('../config.js')

const TIMEOUT_MAX = 15000 // 硬上限，绝对不能超过
const RETRY_DELAY_MS = 300
const RETRY_MAX = 3

/* ------------------------------------------------------------------ *
 * 错误文案
 * ------------------------------------------------------------------ */

// 业务错误码 → 中文兜底文案。QUOTA_EXCEEDED 走前端自己的文案（要引导用户看「关于」页）
const ERROR_TEXT = {
  TEXT_EMPTY: '还没输入内容',
  TEXT_TOO_LONG: '文字太长了，最多 ' + config.MAX_INPUT_CHARS + ' 字',
  QUOTA_EXCEEDED: '今天的深度改写次数用完了，明天自动恢复。「关于」页有说明',
  UNAUTHORIZED: '身份校验没通过，退出小程序重进一次试试',
  RATE_LIMITED: '操作有点快，缓几秒再试',
  LLM_ERROR: 'AI 这次没写好，可以点重试，或者先用规则版',
  INTERNAL: '服务开小差了，稍后再试',
}

// 传输层 / 本地错误
const LOCAL_TEXT = {
  INTERRUPTED: '小程序切到后台时请求被系统中断了（这是正常的）',
  TIMEOUT: '请求超时了',
  NETWORK: '网络不太顺畅',
  NOT_SUPPORTED: '当前微信版本太旧，请升级微信（需要基础库 2.23.0 及以上）',
  CLOUD_NOT_READY: '云托管还没初始化好',
  CANCELLED: '已取消',
  POLL_TIMEOUT: '等太久了，AI 还没写完。可以先看规则版，或者点重试',
  ENV_NOT_FOUND: '云托管环境ID填错了（config.js 的 CLOUD_ENV，要填环境ID而不是环境名）',
  SERVICE_NOT_FOUND: '服务名或环境不对（config.js 的 SERVICE_NAME），或者该服务还没有线上版本',
  NO_PERMISSION: '当前小程序还没开通微信云托管（也没有资源复用权限）',
  CONFIG_MISSING: '还没配置云托管环境ID和服务名，请先改 miniprogram/config.js',
}

/**
 * 生成一个带 code 的 Error，页面统一用 describeError() 取文案。
 */
function apiError(code, message, raw) {
  const err = new Error(message || '请求失败')
  err.code = code || 'INTERNAL'
  err.raw = raw
  return err
}

/**
 * 把任意错误转成能给用户看的一句话。
 */
function describeError(err) {
  if (!err) return ERROR_TEXT.INTERNAL
  if (err.message) return err.message
  return LOCAL_TEXT[err.code] || ERROR_TEXT.INTERNAL
}

/**
 * 业务错误码 → 文案。后端已经给了「人话 message」的话优先用后端的；
 * 只有 QUOTA_EXCEEDED 例外：前端要给出「明天恢复 + 看关于页」的明确指引。
 */
function messageForCode(code, backendMessage) {
  if (code === 'QUOTA_EXCEEDED') return ERROR_TEXT.QUOTA_EXCEEDED
  if (backendMessage) return backendMessage
  return ERROR_TEXT[code] || ERROR_TEXT.INTERNAL
}

/* ------------------------------------------------------------------ *
 * 配置自检
 * ------------------------------------------------------------------ */

function isConfigured() {
  const env = String(config.CLOUD_ENV || '')
  const svc = String(config.SERVICE_NAME || '')
  return !!env && !!svc && env.indexOf('REPLACE_ME') === -1 && svc.indexOf('REPLACE_ME') === -1
}

function configHint() {
  return LOCAL_TEXT.CONFIG_MISSING
}

/* ------------------------------------------------------------------ *
 * 底层：wx.cloud.callContainer 封装
 * ------------------------------------------------------------------ */

function delay(ms) {
  return new Promise(function (resolve) {
    setTimeout(resolve, ms)
  })
}

let cloudInited = false

/**
 * 全局初始化 wx.cloud。app.js onLaunch 会调一次；
 * 这里做幂等保护，重复调用没有副作用。
 */
function initCloud() {
  if (cloudInited) return Promise.resolve()
  cloudInited = true
  return new Promise(function (resolve) {
    try {
      // 注意：不要传 success / fail / complete，否则 callContainer 不返回 Promise。
      // init 的 env 与云托管无关，留空即可（真正决定环境的是 callContainer 的 config.env）。
      wx.cloud.init({ traceUser: true })
    } catch (e) {
      console.warn('[api] wx.cloud.init 失败：', e)
    }
    resolve()
  })
}

function clampTimeout(t) {
  const n = Number(t)
  if (!n || isNaN(n) || n <= 0) return TIMEOUT_MAX
  return Math.min(n, TIMEOUT_MAX)
}

function buildHeader(extra) {
  const header = { 'X-WX-SERVICE': config.SERVICE_NAME }
  if (extra) {
    Object.keys(extra).forEach(function (k) {
      header[k] = extra[k]
    })
  }
  return header
}

function toTransportError(err) {
  const msg = String((err && (err.errMsg || err.message)) || err || '')
  const code = err && (err.errCode !== undefined ? err.errCode : err.errno)

  if (msg.indexOf('interrupted') !== -1) return apiError('INTERRUPTED', LOCAL_TEXT.INTERRUPTED, err)
  if (String(code) === '102002' || msg.toLowerCase().indexOf('timeout') !== -1) {
    return apiError('TIMEOUT', LOCAL_TEXT.TIMEOUT, err)
  }
  if (msg.indexOf("Cloud API isn't enabled") !== -1 || msg.indexOf('Cloud API is not enabled') !== -1) {
    return apiError('CLOUD_NOT_READY', LOCAL_TEXT.CLOUD_NOT_READY, err)
  }
  if (String(code) === '-601027') return apiError('ENV_NOT_FOUND', LOCAL_TEXT.ENV_NOT_FOUND, err)
  if (String(code) === '-601031') return apiError('SERVICE_NOT_FOUND', LOCAL_TEXT.SERVICE_NOT_FOUND, err)
  if (String(code) === '-601034') return apiError('NO_PERMISSION', LOCAL_TEXT.NO_PERMISSION, err)
  if (String(code) === '-606001') return apiError('BODY_TOO_LARGE', '内容太长了，超过单次请求上限', err)
  if (String(code) === '-606002') return apiError('RESP_TOO_LARGE', '返回内容太大，请缩短文本后重试', err)
  if (String(code) === '102005') return apiError('BAD_METHOD', '请求方式不对（基础库版本可能过低）', err)

  return apiError('NETWORK', LOCAL_TEXT.NETWORK + (msg ? '：' + msg : ''), err)
}

/**
 * 发起一次 callContainer（不含重试）。
 */
function callContainerOnce(options) {
  return new Promise(function (resolve, reject) {
    if (!wx.cloud || typeof wx.cloud.callContainer !== 'function') {
      reject(apiError('NOT_SUPPORTED', LOCAL_TEXT.NOT_SUPPORTED))
      return
    }

    let task
    try {
      task = wx.cloud.callContainer({
        config: { env: config.CLOUD_ENV }, // 云托管环境ID（不是环境名）
        path: options.path,
        method: options.method || 'GET',
        data: options.data,
        header: buildHeader(options.header),
        timeout: clampTimeout(options.timeout),
      })
    } catch (e) {
      reject(toTransportError(e))
      return
    }

    // 没有传 success/fail/complete 时返回 Promise；个别低版本基础库会返回 undefined
    if (!task || typeof task.then !== 'function') {
      reject(apiError('NOT_SUPPORTED', LOCAL_TEXT.NOT_SUPPORTED, task))
      return
    }

    task.then(resolve, function (err) {
      reject(toTransportError(err))
    })
  })
}

/**
 * 初始化竞态兜底：遇到 "Cloud API isn't enabled" 等 300ms 重试，最多 3 次。
 */
function callWithInitRetry(options, attempt) {
  return callContainerOnce(options).catch(function (err) {
    if (err && err.code === 'CLOUD_NOT_READY' && attempt < RETRY_MAX) {
      cloudInited = false
      return delay(RETRY_DELAY_MS)
        .then(initCloud)
        .then(function () {
          return callWithInitRetry(options, attempt + 1)
        })
    }
    throw err
  })
}

/**
 * 拆统一信封：成功 { ok:true, data }；失败 { ok:false, error:{code,message} }。
 */
function parseEnvelope(res) {
  const statusCode = res && res.statusCode
  let body = res && res.data

  if (typeof body === 'string') {
    try {
      body = JSON.parse(body)
    } catch (e) {
      body = null
    }
  }

  if (body && typeof body === 'object' && Object.prototype.hasOwnProperty.call(body, 'ok')) {
    if (body.ok === true) {
      return body.data === undefined || body.data === null ? {} : body.data
    }
    const e = body.error || {}
    throw apiError(e.code || 'INTERNAL', messageForCode(e.code, e.message), e)
  }

  if (typeof statusCode === 'number' && statusCode >= 200 && statusCode < 300) {
    return body === null ? {} : body
  }
  if (statusCode === 401 || statusCode === 403) {
    throw apiError('UNAUTHORIZED', ERROR_TEXT.UNAUTHORIZED, res)
  }
  if (statusCode === 429) {
    throw apiError('RATE_LIMITED', ERROR_TEXT.RATE_LIMITED, res)
  }
  throw apiError('INTERNAL', '服务返回异常（HTTP ' + statusCode + '）', res)
}

/**
 * 统一请求入口。options: { path, method, data, header, timeout }
 * 返回：信封里的 data。
 */
function request(options) {
  if (!isConfigured()) {
    return Promise.reject(apiError('CONFIG_MISSING', LOCAL_TEXT.CONFIG_MISSING))
  }
  return initCloud()
    .then(function () {
      return callWithInitRetry(options, 0)
    })
    .then(parseEnvelope)
}

/* ------------------------------------------------------------------ *
 * 业务接口（严格按后端契约，不要臆造字段）
 * ------------------------------------------------------------------ */

/** POST /api/analyze —— 同步、秒出，只做体检 + 规则改写 */
function analyze(text) {
  return request({
    path: '/api/analyze',
    method: 'POST',
    data: { text: text },
    timeout: TIMEOUT_MAX,
  })
}

/** POST /api/rewrite —— 创建 LLM 深度改写任务，立刻返回 taskId */
function createRewrite(text, mode) {
  return request({
    path: '/api/rewrite',
    method: 'POST',
    data: { text: text, mode: mode || 'general' },
    timeout: TIMEOUT_MAX,
  })
}

/** GET /api/task/<taskId> —— 轮询任务结果 */
function getTask(taskId) {
  return request({
    // query / path 里可能带中文或特殊字符，统一编码
    path: '/api/task/' + encodeURIComponent(String(taskId)),
    method: 'GET',
    timeout: TIMEOUT_MAX,
  })
}

/** GET /api/quota —— 查询剩余次数 */
function getQuota() {
  return request({
    path: '/api/quota',
    method: 'GET',
    timeout: TIMEOUT_MAX,
  })
}

/** GET /api/health —— 探活 */
function health() {
  return request({
    path: '/api/health',
    method: 'GET',
    timeout: TIMEOUT_MAX,
  })
}

/* ------------------------------------------------------------------ *
 * 轮询
 * ------------------------------------------------------------------ */

/**
 * pollTask(taskId, { onProgress, signal, deadline })
 *
 * - 单次 callContainer 的 timeout 固定 15000；
 * - 间隔从 config.POLL_INTERVAL_MS 读；
 * - 总时长超过 deadline（默认 now + config.POLL_TIMEOUT_MS）判失败，
 *   但超时前会「补查最后一次」，因为任务很可能在后端已经跑完了；
 * - 杀后台导致的 fail interrupted / 网络抖动不算失败，下一轮继续 —— 这是必须的，
 *   因为小程序切后台 5 秒后请求会被系统杀掉；
 * - signal.stopped = true 可随时停止（页面 onUnload 用），避免重复轮询。
 *
 * @returns {Promise<Object>} 成功时 resolve 最后一次 GET /api/task 的 data（status=done）
 */
function pollTask(taskId, options) {
  const opts = options || {}
  const onProgress = typeof opts.onProgress === 'function' ? opts.onProgress : function () {}
  const signal = opts.signal || {}
  const interval = Math.max(300, Number(config.POLL_INTERVAL_MS) || 1200)
  const totalMs = Math.max(1000, Number(config.POLL_TIMEOUT_MS) || 90000)
  const deadline = Number(opts.deadline) > 0 ? Number(opts.deadline) : Date.now() + totalMs

  function stopped() {
    return signal.stopped === true
  }

  return new Promise(function (resolve, reject) {
    let exceeded = false // 是否已经把总时长耗完（耗完后只补查一次）

    function cancel() {
      reject(apiError('CANCELLED', LOCAL_TEXT.CANCELLED))
    }

    function notify(payload) {
      try {
        onProgress(payload)
      } catch (e) {
        console.warn('[api] onProgress 抛错：', e)
      }
    }

    function schedule(waitMs) {
      if (stopped()) {
        cancel()
        return
      }
      setTimeout(tick, waitMs)
    }

    function handle(data) {
      const status = data && data.status
      if (status === 'done') {
        resolve(data)
        return true
      }
      if (status === 'failed') {
        reject(apiError('LLM_ERROR', (data && data.error) || ERROR_TEXT.LLM_ERROR, data))
        return true
      }
      return false // pending / running
    }

    function tick() {
      if (stopped()) {
        cancel()
        return
      }
      const past = Date.now() >= deadline
      if (past && exceeded) {
        reject(apiError('POLL_TIMEOUT', LOCAL_TEXT.POLL_TIMEOUT))
        return
      }
      if (past) exceeded = true // 允许最后补查一次

      getTask(taskId).then(
        function (data) {
          if (stopped()) {
            cancel()
            return
          }
          notify(data)
          if (handle(data)) return
          if (exceeded) {
            reject(apiError('POLL_TIMEOUT', LOCAL_TEXT.POLL_TIMEOUT))
            return
          }
          schedule(interval)
        },
        function (err) {
          if (stopped()) {
            cancel()
            return
          }
          // 单次失败不判死：切后台被系统 kill、网络抖动都太常见了
          notify({ status: 'retrying', error: err })
          if (exceeded) {
            reject(apiError('POLL_TIMEOUT', LOCAL_TEXT.POLL_TIMEOUT))
            return
          }
          schedule(Math.max(interval, 1000))
        }
      )
    }

    tick()
  })
}

module.exports = {
  TIMEOUT_MAX: TIMEOUT_MAX,
  ERROR_TEXT: ERROR_TEXT,
  LOCAL_TEXT: LOCAL_TEXT,

  initCloud: initCloud,
  isConfigured: isConfigured,
  configHint: configHint,
  describeError: describeError,

  analyze: analyze,
  createRewrite: createRewrite,
  getTask: getTask,
  getQuota: getQuota,
  health: health,
  pollTask: pollTask,
}
