/**
 * pages/history/history.js —— 历史记录（只存在本机）
 */
const store = require('../../utils/store.js')
const format = require('../../utils/format.js')

Page({
  data: {
    list: [],
    limit: store.HISTORY_LIMIT,
    empty: true,
  },

  onShow() {
    this.refresh()
  },

  refresh() {
    const raw = store.getHistoryIndex()
    const list = raw.map(function (it) {
      const lv = format.levelInfo(it.level)
      const summary = (it.summary || '').trim()
      return {
        id: it.id,
        key: it.id,
        timeText: format.formatTime(it.createdAt),
        summary: summary || '（没有正文）',
        score: typeof it.score === 'number' ? it.score : 0,
        levelChip: lv.chip,
        levelText: lv.text,
        modeText: format.modeText(it.mode),
        charCount: it.charCount || 0,
        hasLlm: !!it.hasLlm,
      }
    })
    this.setData({ list: list, empty: list.length === 0 })
  },

  onOpen(e) {
    const id = e.currentTarget.dataset.id
    if (!id) return
    const rec = store.getHistoryItem(id)
    if (!rec) {
      this.toast('这条记录已经不在了')
      this.refresh()
      return
    }
    wx.navigateTo({ url: '/pages/result/result?source=history&id=' + encodeURIComponent(id) })
  },

  onDelete(e) {
    const id = e.currentTarget.dataset.id
    if (!id) return
    wx.showModal({
      title: '删除这一条？',
      content: '删掉就找不回来了。',
      confirmText: '删除',
      confirmColor: '#C6483F',
      cancelText: '取消',
      success: (res) => {
        if (!res.confirm) return
        store.removeHistory(id)
        this.refresh()
        this.toast('已删除')
      },
    })
  },

  onClearAll() {
    if (this.data.empty) {
      this.toast('本来就是空的')
      return
    }
    wx.showModal({
      title: '清空全部历史？',
      content: '一共 ' + this.data.list.length + ' 条，清空后无法恢复。',
      confirmText: '清空',
      confirmColor: '#C6483F',
      cancelText: '再想想',
      success: (res) => {
        if (!res.confirm) return
        store.clearHistory()
        this.refresh()
        this.toast('已清空')
      },
    })
  },

  onBackHome() {
    wx.reLaunch({ url: '/pages/index/index' })
  },

  toast(title) {
    wx.showToast({ title: title, icon: 'none', duration: 1500 })
  },
})
