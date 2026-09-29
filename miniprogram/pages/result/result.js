/**
 * pages/result/result.js —— 结果页
 *
 * 三个 Tab：改写后 / 体检报告 / 原文
 *
 * 轮询要点（对应「小程序切后台 5 秒请求会被系统杀掉」）：
 *  - 单次 callContainer 的 timeout 固定 15000，由 utils/api.js 保证；
 *  - onShow 时若还有没完成的任务，自动恢复轮询；
 *  - 单次请求失败不判死，继续轮询；
 *  - onUnload 停止轮询，避免重复轮询；
 *  - 任务信息写进本地存储，冷启动回来也找得回。
 */
const api = require('../../utils/api.js')
const config = require('../../config.js')
const store = require('../../utils/store.js')
const format = require('../../utils/format.js')

function appInstance() {
  return getApp() || {}
}

Page({
  data: {
    missing: false,
    ready: false,

    tab: 'result',

    // 基本信息
    modeText: '',
    createdAtText: '',
    charCount: 0,

    // 体检报告
    score: 0,
    scoreColor: '#2E8B6E',
    levelText: '',
    levelChip: 'chip-low',
    verdict: '',
    detailVerdict: '',
    advice: [],
    hasAdvice: false,
    totalHits: 0,
    categories: [],
    hits: [],
    hasHits: false,
    hasCategories: false,
    segments: [],
    segmentCount: 0,

    // 文本
    rulesText: '',
    llmText: '',
    hasLlm: false,
    variant: 'rules',
    displayText: '',
    displayLabel: '规则版',
    displayCount: 0,

    // 任务状态
    hasTask: false,
    status: '', // '' | 'pending' | 'running' | 'done' | 'failed'
    progressText: '',
    elapsedSec: 0,
    showProgress: false,
    showError: false,
    errorText: '',
    retryLabel: '重试',
    quotaBlocked: false,
    canUpgrade: false,
  },

  /* ----------------------------- 生命周期 ----------------------------- */

  onLoad(options) {
    this._alive = true
    this._polling = false
    this._submitting = false
    this._signal = { stopped: false }
    this._ticker = null
    this._gen = 0
    this._lastTickAt = 0
    this._taskId = ''
    this._deadline = 0
    this._startedAt = 0
    this._retryKind = 'new'
    this._record = null

    const opts = options || {}
    const source = opts.source || ''
    let id = opts.id || ''
    let taskId = ''
    let deadline = 0

    if (source === 'rewrite' || source === 'analyze') {
      const p = appInstance().globalData.resultPayload || {}
      id = p.recordId || id
      taskId = p.taskId || ''
      deadline = p.deadline || 0
    } else if (source === 'pending') {
      const p = store.getPending() || {}
      id = p.id || id
      taskId = p.taskId || ''
      deadline = p.deadline || 0
    }

    // 兜底：冷启动后 globalData 会丢，用未完成任务 / 最近一条记录找回
    if (!id) {
      const p = store.getPending()
      if (p && p.id) {
        id = p.id
        taskId = taskId || p.taskId || ''
        deadline = deadline || p.deadline || 0
      }
    }
    if (!id) id = store.getLastRecordId()

    const rec = id ? store.getHistoryItem(id) : null
    if (!rec) {
      this.setData({ missing: true, ready: true })
      return
    }

    this._record = rec
    // 历史记录里如果有没跑完的任务，也能接着看
    if (!taskId && !rec.llmText) {
      const p = store.getPending()
      if (p && p.id === rec.id && p.taskId) {
        taskId = p.taskId
        deadline = p.deadline || 0
      }
    }
    this._taskId = taskId
    this._deadline = deadline
    if (deadline) this._startedAt = Math.max(0, deadline - (Number(config.POLL_TIMEOUT_MS) || 90000))

    this.applyRecord(rec)
    this.setData({ ready: true })

    if (taskId && !rec.llmText) {
      this.startPolling(taskId, deadline)
    }
  },

  onShow() {
    if (!this._alive) return
    if (!this._taskId || this.data.llmText || this.data.status === 'failed') return

    // 回到前台：把轮询接上。
    // 单次 callContainer 最长 15s，所以「超过 15s + 一个间隔没有任何回调」就认为
    // 上一轮循环已经卡死（切后台被杀且回调没回来），用新一代轮询顶掉它。
    const staleAfter = (Number(config.POLL_INTERVAL_MS) || 1200) + api.TIMEOUT_MAX + 2000
    const hung = this._polling && Date.now() - (this._lastTickAt || 0) > staleAfter
    if (hung) this.stopPolling()
    if (!this._polling) this.startPolling(this._taskId, this._deadline)
  },

  onHide() {
    // 故意不停止轮询：切后台请求会被系统杀掉，回来时靠 onShow + 重试逻辑续上
  },

  onUnload() {
    this._alive = false
    this.stopPolling()
    this.stopTicker()
  },

  /* ----------------------------- 渲染 ----------------------------- */

  applyRecord(rec) {
    const report = format.decorateReport(rec.report)
    const rawHits = rec.report && rec.report.hits ? rec.report.hits : []
    const segments = format.buildSegments(rec.text, rawHits)
    const llmText = rec.llmText || ''
    const rulesText = rec.rulesText || ''
    // 有 AI 版就默认展示 AI 版；只有规则版时展示规则版
    const variant = llmText ? 'llm' : 'rules'

    this.setData({
      modeText: format.modeText(rec.mode),
      createdAtText: format.formatTime(rec.createdAt),
      charCount: report.charCount || format.countChars(rec.text),

      score: report.score,
      scoreColor: report.scoreColor,
      levelText: report.levelText,
      levelChip: report.levelChip,
      verdict: format.scoreVerdict(report.score),
      detailVerdict: report.verdict || '',
      advice: report.advice,
      hasAdvice: report.hasAdvice,
      totalHits: report.totalHits,
      categories: report.categories,
      hits: report.hits,
      hasHits: report.hasHits,
      hasCategories: report.hasCategories,

      segments: segments,
      segmentCount: segments.length,

      rulesText: rulesText,
      llmText: llmText,
      hasLlm: !!llmText,
      variant: variant,
    })
    this.refreshDisplay()
    this.refreshFlags()
  },

  refreshDisplay() {
    const hasLlm = !!this.data.llmText
    const useLlm = hasLlm && this.data.variant === 'llm'
    const text = useLlm ? this.data.llmText : this.data.rulesText
    this.setData({
      hasLlm: hasLlm,
      displayText: text,
      displayLabel: useLlm ? 'AI 改写版' : '规则版',
      displayCount: format.countChars(text),
    })
  },

  refreshFlags() {
    const status = this.data.status
    const busy = status === 'pending' || status === 'running'
    this.setData({
      showProgress: this.data.hasTask && busy,
      showError: status === 'failed',
      canUpgrade: !this.data.hasLlm && this.data.hasTask === false && !!this._record,
    })
  },

  onTab(e) {
    const tab = e.currentTarget.dataset.tab || 'result'
    if (tab === this.data.tab) return
    this.setData({ tab: tab })
  },

  onVariant(e) {
    const variant = e.currentTarget.dataset.variant === 'llm' ? 'llm' : 'rules'
    if (variant === this.data.variant) return
    if (variant === 'llm' && !this.data.llmText) return
    this.setData({ variant: variant })
    this.refreshDisplay()
  },

  onAbout() {
    wx.navigateTo({ url: '/pages/about/about' })
  },

  onBackHome() {
    wx.reLaunch({ url: '/pages/index/index' })
  },

  /* ----------------------------- 轮询 ----------------------------- */

  savePendingFor(taskId, startedAt, deadline) {
    store.savePending({
      id: this._record.id,
      taskId: taskId,
      mode: this._record.mode || 'general',
      text: this._record.text || '',
      rulesText: this.data.rulesText || '',
      startedAt: startedAt,
      deadline: deadline,
    })
  },

  startPolling(taskId, deadline) {
    if (this._polling || !this._alive) return
    if (!taskId) {
      // 服务端没给任务号就别转圈了，直接给可重试的失败态
      this.setData({
        status: 'failed',
        errorText: '服务端没有返回任务号，请重新提交一次',
        retryLabel: '重新改写',
      })
      this.refreshFlags()
      return
    }

    this._polling = true
    this._taskId = taskId
    if (deadline) this._deadline = deadline
    if (!this._deadline) this._deadline = Date.now() + (Number(config.POLL_TIMEOUT_MS) || 90000)
    if (!this._startedAt) this._startedAt = Date.now()

    this._signal = { stopped: false }
    this.setData({
      hasTask: true,
      status: 'pending',
      progressText: '排队中…',
      errorText: '',
      quotaBlocked: false,
    })
    this.refreshFlags()
    this.startTicker()

    // 每一代轮询有唯一 gen：旧的循环即使晚一步回调，也不会覆盖新循环的状态
    const self = this
    const gen = ++this._gen
    this._lastTickAt = Date.now()

    api
      .pollTask(taskId, {
        deadline: this._deadline,
        signal: this._signal,
        onProgress: function (data) {
          self.onProgress(data)
        },
      })
      .then(
        function (data) {
          if (gen !== self._gen) return
          self._polling = false
          self.onDone(data)
        },
        function (err) {
          if (gen !== self._gen) return
          self._polling = false
          self.onFailed(err)
        }
      )
  },

  stopPolling() {
    this._gen = (this._gen || 0) + 1 // 让在途回调失效，避免旧循环把新循环的状态写坏
    if (this._signal) this._signal.stopped = true
    this._polling = false
  },

  startTicker() {
    this.stopTicker()
    this.setData({ elapsedSec: 0 })
    const self = this
    this._ticker = setInterval(function () {
      if (!self._alive) return
      const sec = Math.max(0, Math.floor((Date.now() - self._startedAt) / 1000))
      if (sec !== self.data.elapsedSec) self.setData({ elapsedSec: sec })
    }, 1000)
  },

  stopTicker() {
    if (this._ticker) {
      clearInterval(this._ticker)
      this._ticker = null
    }
  },

  onProgress(data) {
    if (!this._alive) return
    this._lastTickAt = Date.now() // 喂看门狗：只要还有回调，就说明轮询活着
    const status = data && data.status

    if (status === 'retrying') {
      // 切后台被系统中断、网络抖动都会走到这里，界面不要跳成失败
      this.setData({ progressText: '网络抖了一下，正在自动续上…' })
      return
    }
    if (status === 'done' || status === 'failed') return

    this.setData({
      status: status === 'running' ? 'running' : 'pending',
      progressText: status === 'running' ? 'AI 正在逐句重写…' : '排队中…',
    })
    this.refreshFlags()
  },

  onDone(data) {
    this.stopTicker()
    if (!this._alive) return

    const patch = {
      llmText: (data && data.llmText) || '',
      rulesText: (data && data.rulesText) || this.data.rulesText,
      elapsedMs: (data && data.elapsedMs) || 0,
    }
    const updated = store.updateHistory(this._record.id, patch)
    this._record = updated || Object.assign({}, this._record, patch)
    store.clearPending()

    this.setData({
      status: 'done',
      progressText: '',
      errorText: '',
      hasTask: true,
      llmText: this._record.llmText,
      rulesText: this._record.rulesText,
      variant: this._record.llmText ? 'llm' : 'rules',
    })
    this.refreshDisplay()
    this.refreshFlags()

    wx.showToast({ title: '改写完成', icon: 'success', duration: 1200 })
  },

  onFailed(err) {
    this.stopTicker()
    if (!this._alive) return
    const code = err && err.code
    if (code === 'CANCELLED') return

    const mayStillRun = code === 'POLL_TIMEOUT' || code === 'TIMEOUT' || code === 'INTERRUPTED' || code === 'NETWORK'
    this._retryKind = mayStillRun ? 'poll' : 'new'

    // 任务确实死了就别再提示「有未完成任务」；还可能活着就留着，回头能续上
    if (!mayStillRun) store.clearPending()

    this.setData({
      status: 'failed',
      errorText: api.describeError(err),
      retryLabel: mayStillRun ? '再查一次' : '重新改写',
      quotaBlocked: code === 'QUOTA_EXCEEDED',
      variant: 'rules', // 兜底展示规则版
    })
    this.refreshDisplay()
    this.refreshFlags()
  },

  onRetry() {
    if (!this._alive || this._submitting) return

    // 任务可能还在后端跑：先重新开一个轮询窗口，不花次数
    if (this._retryKind === 'poll' && this._taskId) {
      const startedAt = Date.now()
      const deadline = startedAt + (Number(config.POLL_TIMEOUT_MS) || 90000)
      this._startedAt = startedAt
      this._deadline = deadline
      this.savePendingFor(this._taskId, startedAt, deadline)
      this.setData({ status: 'pending', progressText: '重新查询中…', errorText: '' })
      this.refreshFlags()
      this.stopPolling()
      this.startPolling(this._taskId, deadline)
      return
    }

    this.createTask('重新提交…')
  },

  /** 只有规则版的时候，可以就地升级成 AI 深度改写 */
  onUpgrade() {
    if (!this._alive || this._submitting) return
    wx.showModal({
      title: '用 AI 深度改写？',
      content: '会消耗一次深度改写额度（约 10-30 秒）。规则版结果会保留。',
      confirmText: '开始',
      cancelText: '算了',
      success: (res) => {
        if (res.confirm) this.createTask('创建任务…')
      },
    })
  },

  createTask(loadingTitle) {
    if (!this._record) return
    this._submitting = true
    wx.showLoading({ title: loadingTitle || '创建任务…', mask: true })

    const self = this
    api
      .createRewrite(this._record.text, this._record.mode || 'general')
      .then(function (data) {
        wx.hideLoading()
        self._submitting = false
        if (!self._alive) return

        const patch = {
          rulesText: (data && data.rulesText) || self._record.rulesText,
          llmText: '',
        }
        if (data && data.report) {
          patch.report = data.report
          patch.score = data.report.score
          patch.level = data.report.level
          patch.totalHits = data.report.totalHits
          patch.charCount = data.report.charCount
        }
        const updated = store.updateHistory(self._record.id, patch)
        self._record = updated || Object.assign({}, self._record, patch)
        store.setLastRecordId(self._record.id)

        const startedAt = Date.now()
        const deadline = startedAt + (Number(config.POLL_TIMEOUT_MS) || 90000)
        self._startedAt = startedAt
        self._deadline = deadline
        self._taskId = (data && data.taskId) || ''
        self.savePendingFor(self._taskId, startedAt, deadline)

        self.applyRecord(self._record)
        self.setData({ tab: 'result', status: 'pending', progressText: '排队中…', errorText: '' })
        self.refreshFlags()
        self.stopPolling()
        self.startPolling(self._taskId, deadline)
      })
      .catch(function (err) {
        wx.hideLoading()
        self._submitting = false
        if (!self._alive) return
        const code = err && err.code
        self.setData({
          status: 'failed',
          errorText: api.describeError(err),
          retryLabel: '重新改写',
          quotaBlocked: code === 'QUOTA_EXCEEDED',
        })
        self.refreshFlags()
      })
  },

  /* ----------------------------- 复制 ----------------------------- */

  onCopy() {
    const text = this.data.displayText
    if (!text) {
      this.toast('没有可复制的内容')
      return
    }
    wx.setClipboardData({
      data: text,
      success: () => {
        wx.showToast({ title: '已复制', icon: 'none', duration: 1400 })
      },
      fail: () => {
        this.toast('复制失败，长按文本手动选吧')
      },
    })
  },

  toast(title) {
    wx.showToast({ title: title, icon: 'none', duration: 1600 })
  },
})
