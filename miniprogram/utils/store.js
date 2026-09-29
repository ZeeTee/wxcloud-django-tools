/**
 * utils/store.js —— 本地历史记录读写（只存在用户手机上，不上传）
 *
 * 存储结构（为什么要拆成「索引 + 明细」两个 key）：
 *   小程序单个 storage key 上限是 1MB。一条记录含原文 + 规则版 + AI 版 + 体检报告，
 *   50 条塞进一个 key 必然超限，所以：
 *     - deai_history_index_v1        ：轻量索引（时间/分数/摘要），倒序，最多 50 条
 *     - deai_history_item_v1_<id>    ：单条完整记录，一条一个 key
 *   淘汰最老的第 51 条时，会把它的明细 key 一起删掉，不会越存越多。
 */

const INDEX_KEY = 'deai_history_index_v1'
const ITEM_PREFIX = 'deai_history_item_v1_'
const PENDING_KEY = 'deai_pending_task_v1'
const LAST_KEY = 'deai_last_record_v1'
const HISTORY_LIMIT = 50
const SUMMARY_LEN = 40

/* ------------------------------ 基础工具 ------------------------------ */

function safeGet(key, fallback) {
  try {
    const v = wx.getStorageSync(key)
    if (v === '' || v === null || v === undefined) return fallback
    return v
  } catch (e) {
    console.warn('[store] getStorageSync 失败：', key, e)
    return fallback
  }
}

function safeSet(key, value) {
  try {
    wx.setStorageSync(key, value)
    return true
  } catch (e) {
    console.warn('[store] setStorageSync 失败：', key, e)
    return false
  }
}

function safeRemove(key) {
  try {
    wx.removeStorageSync(key)
  } catch (e) {
    // ignore
  }
}

function itemKey(id) {
  return ITEM_PREFIX + id
}

function makeId() {
  return 'h' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
}

function summarize(text) {
  return String(text || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, SUMMARY_LEN)
}

function toIndexItem(rec) {
  return {
    id: rec.id,
    createdAt: rec.createdAt,
    mode: rec.mode || 'general',
    score: typeof rec.score === 'number' ? rec.score : 0,
    level: rec.level || 'low',
    totalHits: rec.totalHits || 0,
    charCount: rec.charCount || 0,
    summary: rec.summary || summarize(rec.text),
    hasLlm: !!rec.llmText,
  }
}

/** 报告太大时裁剪，保证一定能写进去 */
function trimReport(report, maxHits) {
  if (!report || typeof report !== 'object') return report
  const hits = Array.isArray(report.hits) ? report.hits.slice(0, maxHits) : []
  return Object.assign({}, report, { hits: hits })
}

/* ------------------------------ 历史记录 ------------------------------ */

function readIndex() {
  const v = safeGet(INDEX_KEY, [])
  return Array.isArray(v) ? v : []
}

function writeIndex(list) {
  safeSet(INDEX_KEY, list)
}

/** 轻量索引（列表页用），已倒序 */
function getHistoryIndex() {
  return readIndex()
}

/** 单条完整记录，取不到返回 null */
function getHistoryItem(id) {
  if (!id) return null
  const v = safeGet(itemKey(id), null)
  return v && typeof v === 'object' ? v : null
}

/**
 * 新增一条记录，返回落库后的完整记录（含 id / createdAt）。
 * record: { text, mode, report, rulesText, llmText, score, level, totalHits, charCount, kind }
 */
function addHistory(record) {
  const rec = Object.assign({}, record || {})
  rec.id = rec.id || makeId()
  rec.createdAt = rec.createdAt || Date.now()
  rec.mode = rec.mode || 'general'
  rec.llmText = rec.llmText || ''
  rec.rulesText = rec.rulesText || ''
  rec.text = rec.text || ''
  rec.summary = summarize(rec.text)

  // 先写明细；写不下就裁掉命中明细再试一次（原文和两版结果一定保住）
  if (!safeSet(itemKey(rec.id), rec)) {
    safeSet(itemKey(rec.id), Object.assign({}, rec, { report: trimReport(rec.report, 60) }))
  }

  // 再更新索引：同 id 去重后插到最前
  const index = readIndex().filter(function (it) {
    return it && it.id !== rec.id
  })
  index.unshift(toIndexItem(rec))

  while (index.length > HISTORY_LIMIT) {
    const dropped = index.pop()
    if (dropped && dropped.id) safeRemove(itemKey(dropped.id))
  }
  writeIndex(index)

  return rec
}

/**
 * 增量更新一条记录（AI 结果回来时用），返回更新后的记录。
 */
function updateHistory(id, patch) {
  const cur = getHistoryItem(id)
  if (!cur) return null
  const next = Object.assign({}, cur, patch || {})
  if (patch && patch.text !== undefined) next.summary = summarize(next.text)
  if (!safeSet(itemKey(id), next)) {
    safeSet(itemKey(id), Object.assign({}, next, { report: trimReport(next.report, 60) }))
  }
  const index = readIndex().map(function (it) {
    return it && it.id === id ? toIndexItem(next) : it
  })
  writeIndex(index)
  return next
}

function removeHistory(id) {
  if (!id) return
  safeRemove(itemKey(id))
  writeIndex(
    readIndex().filter(function (it) {
      return it && it.id !== id
    })
  )
}

function clearHistory() {
  readIndex().forEach(function (it) {
    if (it && it.id) safeRemove(itemKey(it.id))
  })
  safeRemove(INDEX_KEY)
}

/* ------------------------------ 未完成任务 ------------------------------ */
/* 杀后台 / 冷启动后还能找回正在跑的 LLM 任务，避免用户白等一场 */

function savePending(pending) {
  return safeSet(PENDING_KEY, pending)
}

function getPending() {
  const v = safeGet(PENDING_KEY, null)
  return v && typeof v === 'object' && v.taskId ? v : null
}

function clearPending() {
  safeRemove(PENDING_KEY)
}

/* ------------------------------ 最近一条记录 ------------------------------ */
/* 冷启动（小程序被系统回收后重进）时 globalData 会丢，用这个把结果页找回来 */

function setLastRecordId(id) {
  if (id) safeSet(LAST_KEY, id)
}

function getLastRecordId() {
  return safeGet(LAST_KEY, '') || ''
}

module.exports = {
  HISTORY_LIMIT: HISTORY_LIMIT,
  getHistoryIndex: getHistoryIndex,
  getHistoryItem: getHistoryItem,
  addHistory: addHistory,
  updateHistory: updateHistory,
  removeHistory: removeHistory,
  clearHistory: clearHistory,
  savePending: savePending,
  getPending: getPending,
  clearPending: clearPending,
  setLastRecordId: setLastRecordId,
  getLastRecordId: getLastRecordId,
}
