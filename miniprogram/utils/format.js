/**
 * utils/format.js —— 纯函数：字数、时间、等级/严重度映射、命中高亮分段
 * 不依赖 wx.* ，方便单独复查逻辑。
 */

const LEVEL_MAP = {
  low: { text: '低', chip: 'chip-low', color: '#2E8B6E' },
  medium: { text: '中', chip: 'chip-medium', color: '#C77E22' },
  high: { text: '高', chip: 'chip-high', color: '#C6483F' },
}

const SEVERITY_MAP = {
  low: { text: '轻微', chip: 'chip-low', block: 'hl-low' },
  medium: { text: '中度', chip: 'chip-medium', block: 'hl-medium' },
  high: { text: '严重', chip: 'chip-high', block: 'hl-high' },
}

const MODE_MAP = {
  general: '通用',
  xhs: '小红书',
}

/** 字数：按用户直觉数「字」，emoji 之类的代理对算一个 */
function countChars(text) {
  const s = String(text || '')
  let n = 0
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i)
    if (code >= 0xd800 && code <= 0xdbff && i + 1 < s.length) {
      const next = s.charCodeAt(i + 1)
      if (next >= 0xdc00 && next <= 0xdfff) i++
    }
    n++
  }
  return n
}

function pad2(n) {
  return n < 10 ? '0' + n : String(n)
}

/** 时间戳 → 刚刚 / 12 分钟前 / 今天 20:06 / 09-28 20:06 */
function formatTime(ts) {
  const t = Number(ts) || 0
  if (!t) return ''
  const d = new Date(t)
  const now = new Date()
  const diff = now.getTime() - t

  if (diff >= 0 && diff < 60 * 1000) return '刚刚'
  if (diff >= 0 && diff < 60 * 60 * 1000) return Math.floor(diff / 60000) + ' 分钟前'

  const hm = pad2(d.getHours()) + ':' + pad2(d.getMinutes())
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  if (sameDay) return '今天 ' + hm

  const md = pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
  if (d.getFullYear() === now.getFullYear()) return md + ' ' + hm
  return d.getFullYear() + '-' + md + ' ' + hm
}

function levelInfo(level) {
  return LEVEL_MAP[level] || LEVEL_MAP.low
}

function severityInfo(severity) {
  return SEVERITY_MAP[severity] || SEVERITY_MAP.low
}

function modeText(mode) {
  return MODE_MAP[mode] || MODE_MAP.general
}

/**
 * 把 hits 的 start/end（字符下标）切成「命中 / 未命中」交替的分段，
 * 供「原文」tab 逐段渲染高亮。重叠的命中只保留第一个。
 */
function buildSegments(text, hits) {
  const src = String(text || '')
  const list = (Array.isArray(hits) ? hits : [])
    .filter(function (h) {
      return h && typeof h.start === 'number' && typeof h.end === 'number' && h.end > h.start
    })
    .map(function (h) {
      return {
        start: Math.max(0, Math.floor(h.start)),
        end: Math.min(src.length, Math.floor(h.end)),
        severity: h.severity || 'low',
      }
    })
    .filter(function (h) {
      return h.end > h.start
    })
    .sort(function (a, b) {
      return a.start - b.start
    })

  const segs = []
  let cursor = 0
  for (let i = 0; i < list.length; i++) {
    const h = list[i]
    if (h.start < cursor) continue // 与上一处命中重叠，跳过
    if (h.start > cursor) segs.push({ text: src.slice(cursor, h.start), hit: false, severity: '' })
    segs.push({
      text: src.slice(h.start, h.end),
      hit: true,
      severity: h.severity,
      block: severityInfo(h.severity).block,
    })
    cursor = h.end
  }
  if (cursor < src.length) segs.push({ text: src.slice(cursor), hit: false, severity: '' })
  if (!segs.length) segs.push({ text: src, hit: false, severity: '' })

  segs.forEach(function (s, i) {
    s.key = 'seg' + i
  })
  return segs
}

/**
 * 把后端 report 加工成模板直接可用的形状（补 key、色块 class、文案）。
 * 后端字段：score / level / totalHits / charCount / categories[] / hits[]
 */
function decorateReport(report) {
  const r = report && typeof report === 'object' ? report : {}
  const score = typeof r.score === 'number' ? r.score : 0
  const level = r.level || 'low'
  const lv = levelInfo(level)

  const categories = (Array.isArray(r.categories) ? r.categories : []).map(function (c, i) {
    const sev = severityInfo(c && c.severity)
    const samples = Array.isArray(c && c.samples) ? c.samples : []
    return {
      key: 'c' + i + '_' + ((c && c.key) || ''),
      name: (c && c.name) || '未分类',
      count: (c && c.count) || 0,
      severity: (c && c.severity) || 'low',
      chip: sev.chip,
      severityText: sev.text,
      samplesText: samples.join('、'),
      hasSamples: samples.length > 0,
    }
  })

  const hits = (Array.isArray(r.hits) ? r.hits : []).map(function (h, i) {
    const sev = severityInfo(h && h.severity)
    const kind = (h && h.kind) || 'flag'
    return {
      key: 'h' + i,
      no: i + 1,
      text: (h && h.text) || '',
      kind: kind,
      kindText: kind === 'replace' ? '已替换' : '仅标记',
      severity: (h && h.severity) || 'low',
      chip: sev.chip,
      block: sev.block,
      severityText: sev.text,
      reason: (h && h.reason) || '',
      suggestion: (h && h.suggestion) || '',
      hasSuggestion: !!(h && h.suggestion),
      categoryName: (h && h.categoryName) || '',
    }
  })

  // 后端如果给了 verdict / advice 就用它的（更贴合数据），没有就本地兜底，不依赖这两个字段
  const backendVerdict = typeof r.verdict === 'string' ? r.verdict.trim() : ''
  const advice = (Array.isArray(r.advice) ? r.advice : []).filter(function (t) {
    return typeof t === 'string' && t.trim()
  })

  return {
    score: score,
    level: level,
    levelText: lv.text,
    levelChip: lv.chip,
    scoreColor: lv.color,
    totalHits: typeof r.totalHits === 'number' ? r.totalHits : hits.length,
    charCount: typeof r.charCount === 'number' ? r.charCount : 0,
    categories: categories,
    hits: hits,
    hasHits: hits.length > 0,
    hasCategories: categories.length > 0,
    verdict: backendVerdict || scoreVerdict(score),
    advice: advice.slice(0, 4),
    hasAdvice: advice.length > 0,
  }
}

/** 按分数给一句人话结论 */
function scoreVerdict(score) {
  const s = Number(score) || 0
  if (s >= 70) return 'AI 味偏重，建议整段重写'
  if (s >= 40) return '有些机器腔，局部改一改就行'
  if (s > 0) return '整体像人写的，个别套话顺手删掉'
  return '没检出明显的 AI 痕迹'
}

module.exports = {
  countChars: countChars,
  formatTime: formatTime,
  levelInfo: levelInfo,
  severityInfo: severityInfo,
  modeText: modeText,
  buildSegments: buildSegments,
  decorateReport: decorateReport,
  scoreVerdict: scoreVerdict,
}
