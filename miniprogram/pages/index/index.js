/**
 * pages/index/index.js —— 首页：输入 → 选模式 → 深度改写 / 只做体检
 */
const api = require('../../utils/api.js')
const config = require('../../config.js')
const store = require('../../utils/store.js')
const format = require('../../utils/format.js')

// 内置示例：一段典型「AI 腔」中文，几乎命中所有常见套路，方便用户一键体验
const SAMPLE_TEXT =
  '在当今数字化浪潮下，如何提升内容创作的效率，已经成为每个团队必须面对的重要课题。' +
  '首先，我们需要明确目标用户的核心痛点，找到真正的抓手；其次，要打通数据与流程之间的闭环，实现降本增效。' +
  '值得注意的是，AI 工具的赋能作用不可否认，它不仅极大地提升了写作效率，而且显著地降低了沟通成本。' +
  '综上所述，只有坚持精益求精的态度，才能在激烈的赛道中脱颖而出，为业务发展注入新的活力。' +
  '未来可期，让我们一起拭目以待。'

function appInstance() {
  return getApp() || {}
}

Page({
  data: {
    text: '',
    charCount: 0,
    maxChars: config.MAX_INPUT_CHARS,
    over: false,
    mode: 'general',

    submitting: false,
    submittingAction: '',
    canSubmit: false,
    btnDisabled: false,

    quota: null,
    quotaText: '',
    quotaEmpty: false,

    configured: true,
    configHint: '',

    pending: null,
    pendingSummary: '',
  },

  /* ----------------------------- 生命周期 ----------------------------- */

  onLoad() {
    this._text = ''
    const configured = api.isConfigured()
    this.setData({
      configured: configured,
      configHint: configured ? '' : api.configHint(),
    })
    if (configured) this.refreshQuota()
  },

  onShow() {
    // 从结果页/历史页返回时刷新次数；顺便检查有没有没看完的 LLM 任务
    if (this.data.configured) this.refreshQuota()
    this.checkPending()
  },

  /* ----------------------------- 输入区 ----------------------------- */

  onInput(e) {
    // 注意：这里不 setData(this._text)，避免原生 textarea 在中文输入法下光标错位。
    // data.text 只在「程序主动改文本」时更新（粘贴/示例/清空）。
    this._text = e.detail.value || ''
    this.syncCount()
  },

  /** 程序主动设置文本：先同步 data.text 让原生组件刷新 */
  setText(value) {
    const v = value === null || value === undefined ? '' : String(value)
    this._text = v
    this.syncCount()
    if (v === this.data.text) {
      // 值没变时 setData 不会触发原生组件更新，用一个临时值顶一下
      const tmp = v === '' ? '\u200B' : ''
      this.setData({ text: tmp }, () => {
        this.setData({ text: v })
      })
    } else {
      this.setData({ text: v })
    }
  },

  syncCount() {
    this.refreshSubmitState(this.data.submitting)
  },

  /** 单一来源：按钮状态永远由「当前文本 + 是否提交中」算出来，避免用到过期计数 */
  refreshSubmitState(submitting) {
    const len = format.countChars(this._text)
    const over = len > this.data.maxChars
    const busy = !!submitting
    const empty = len === 0
    this.setData({
      charCount: len,
      over: over,
      // canSubmit 只控制「看起来能不能点」的样式
      canSubmit: !empty && !over && !busy,
      // 空输入 / 超字数 / 提交中都真正禁用（空输入时下方有常驻轻提示）
      btnDisabled: empty || over || busy,
    })
  },

  onPaste() {
    wx.getClipboardData({
      success: (res) => {
        const data = (res && res.data) || ''
        if (!data.trim()) {
          this.toast('剪贴板是空的')
          return
        }
        this.setText(data)
        this.toast('已粘贴')
      },
      fail: () => {
        this.toast('读不到剪贴板，手动粘贴吧')
      },
    })
  },

  onSample() {
    this.setText(SAMPLE_TEXT)
    this.toast('已填入示例')
  },

  onClear() {
    if (!this._text) {
      this.toast('已经是空的了')
      return
    }
    this.setText('')
  },

  onMode(e) {
    const mode = (e.currentTarget.dataset.mode || 'general') === 'xhs' ? 'xhs' : 'general'
    if (mode === this.data.mode) return
    this.setData({ mode: mode })
  },

  /* ----------------------------- 剩余次数 ----------------------------- */

  refreshQuota() {
    return api
      .getQuota()
      .then((data) => {
        this.applyQuota(data)
      })
      .catch((err) => {
        console.warn('[index] 查询剩余次数失败：', err)
        this.setData({ quota: null, quotaText: '剩余次数暂时查不到，点这里重试', quotaEmpty: false })
      })
  },

  applyQuota(data) {
    const q = data || {}
    const limit = typeof q.limit === 'number' ? q.limit : 0
    const remaining = typeof q.remaining === 'number' ? q.remaining : 0
    this.setData({
      quota: { used: q.used || 0, limit: limit, remaining: remaining },
      quotaText: limit ? '今日剩余 ' + remaining + ' / ' + limit + ' 次' : '今日剩余 ' + remaining + ' 次',
      quotaEmpty: remaining <= 0 && limit > 0,
    })
  },

  onRefreshQuota() {
    if (!this.data.configured) return
    wx.showLoading({ title: '查询中', mask: true })
    this.refreshQuota().then(() => {
      wx.hideLoading()
      this.toast('已刷新')
    })
  },

  onAbout() {
    wx.navigateTo({ url: '/pages/about/about' })
  },

  /* ----------------------------- 未完成任务 ----------------------------- */

  checkPending() {
    const p = store.getPending()
    if (!p) {
      if (this.data.pending) this.setData({ pending: null, pendingSummary: '' })
      return
    }
    const rec = store.getHistoryItem(p.id)
    if (!rec) {
      // 记录被删了，清掉悬空的 pending
      store.clearPending()
      if (this.data.pending) this.setData({ pending: null, pendingSummary: '' })
      return
    }
    this.setData({
      pending: p,
      pendingSummary: (rec.summary || '').slice(0, 24),
    })
  },

  onResumePending() {
    const app = appInstance()
    const p = this.data.pending
    if (!p || !p.id) return
    app.globalData.resultPayload = { source: 'pending', recordId: p.id }
    wx.navigateTo({ url: '/pages/result/result?source=pending' })
  },

  onDismissPending() {
    wx.showModal({
      title: '不再等待这次改写？',
      content: '规则版结果会保留在历史记录里，AI 版结果将不再自动取回。',
      confirmText: '好',
      cancelText: '继续等',
      success: (res) => {
        if (!res.confirm) return
        store.clearPending()
        this.setData({ pending: null, pendingSummary: '' })
      },
    })
  },

  /* ----------------------------- 提交 ----------------------------- */

  guardSubmit() {
    if (this.data.submitting) return false
    if (!this.data.configured) {
      wx.showModal({
        title: '还没配置云托管',
        content: api.configHint() + '。打开 miniprogram/config.js，填好 CLOUD_ENV 和 SERVICE_NAME 再试。',
        showCancel: false,
        confirmText: '知道了',
      })
      return false
    }
    const len = format.countChars(this._text)
    if (len === 0) {
      this.toast('先粘贴或输入一段文字')
      return false
    }
    if (len > this.data.maxChars) {
      this.toast('文字太长了，最多 ' + this.data.maxChars + ' 字')
      return false
    }
    return true
  },

  setSubmitting(action) {
    this.setData({
      submitting: !!action,
      submittingAction: action || '',
    })
    this.refreshSubmitState(action)
  },

  /** 主按钮：深度改写（AI）→ 创建任务 → 跳结果页轮询 */
  onRewrite() {
    if (!this.guardSubmit()) return
    const text = this._text
    const mode = this.data.mode

    this.setSubmitting('rewrite')
    wx.showLoading({ title: '创建任务…', mask: true })

    api
      .createRewrite(text, mode)
      .then((data) => {
        wx.hideLoading()

        // 先落历史（规则版立刻就有，AI 版回来后再 update），结果页只认 recordId
        const rec = store.addHistory({
          kind: 'rewrite',
          text: text,
          mode: mode,
          report: data.report,
          rulesText: data.rulesText,
          llmText: '',
          score: (data.report && data.report.score) || 0,
          level: (data.report && data.report.level) || 'low',
          totalHits: (data.report && data.report.totalHits) || 0,
          charCount: (data.report && data.report.charCount) || 0,
        })

        const startedAt = Date.now()
        const deadline = startedAt + (Number(config.POLL_TIMEOUT_MS) || 90000)
        const pending = {
          id: rec.id,
          taskId: data.taskId,
          mode: mode,
          text: text,
          rulesText: data.rulesText || '',
          startedAt: startedAt,
          deadline: deadline,
        }
        store.savePending(pending)
        store.setLastRecordId(rec.id)

        if (data.quota) this.applyQuota(data.quota)

        appInstance().globalData.resultPayload = {
          source: 'rewrite',
          recordId: rec.id,
          taskId: data.taskId,
          deadline: deadline,
        }
        wx.navigateTo({ url: '/pages/result/result?source=rewrite' })
      })
      .catch((err) => {
        wx.hideLoading()
        this.showError(err, 'rewrite')
      })
      .then(() => {
        this.setSubmitting('')
        this.checkPending()
      })
  },

  /** 次按钮：只做体检 + 规则改写（秒出） */
  onAnalyze() {
    if (!this.guardSubmit()) return
    const text = this._text
    const mode = this.data.mode

    this.setSubmitting('analyze')
    wx.showLoading({ title: '体检中…', mask: true })

    api
      .analyze(text)
      .then((data) => {
        wx.hideLoading()
        const rec = store.addHistory({
          kind: 'analyze',
          text: text,
          mode: mode,
          report: data.report,
          rulesText: data.rulesText,
          rulesChanges: data.rulesChanges || 0,
          llmText: '',
          score: (data.report && data.report.score) || 0,
          level: (data.report && data.report.level) || 'low',
          totalHits: (data.report && data.report.totalHits) || 0,
          charCount: (data.report && data.report.charCount) || 0,
        })
        appInstance().globalData.resultPayload = {
          source: 'analyze',
          recordId: rec.id,
        }
        store.setLastRecordId(rec.id)
        wx.navigateTo({ url: '/pages/result/result?source=analyze' })
      })
      .catch((err) => {
        wx.hideLoading()
        this.showError(err, 'analyze')
      })
      .then(() => {
        this.setSubmitting('')
        this.checkPending()
      })
  },

  showError(err, action) {
    const code = err && err.code
    if (code === 'QUOTA_EXCEEDED') {
      wx.showModal({
        title: '今天的次数用完了',
        content: api.describeError(err),
        confirmText: '看说明',
        cancelText: '知道了',
        success: (res) => {
          if (res.confirm) wx.navigateTo({ url: '/pages/about/about' })
        },
      })
      this.refreshQuota()
      return
    }
    this.toast(api.describeError(err))
  },

  onHistory() {
    wx.navigateTo({ url: '/pages/history/history' })
  },

  toast(title) {
    wx.showToast({ title: title, icon: 'none', duration: 1600 })
  },
})
