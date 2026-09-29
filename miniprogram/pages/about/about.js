/**
 * pages/about/about.js —— 关于：这个工具做什么、边界、次数规则、隐私
 */
const api = require('../../utils/api.js')
const config = require('../../config.js')

Page({
  data: {
    version: config.VERSION,
    maxChars: config.MAX_INPUT_CHARS,
    pollSec: Math.round((Number(config.POLL_TIMEOUT_MS) || 90000) / 1000),
    cloudEnv: config.CLOUD_ENV,
    serviceName: config.SERVICE_NAME,
    configured: true,

    quotaText: '查询中…',
    quotaRemaining: -1,
    quotaLimit: 0,

    healthText: '检测中…',
    healthOk: false,
  },

  onLoad() {
    const configured = api.isConfigured()
    this.setData({ configured: configured })
    if (!configured) {
      this.setData({ quotaText: '还没配置云托管', healthText: '未检测' })
      return
    }
    this.loadQuota()
    this.checkHealth()
  },

  loadQuota() {
    api
      .getQuota()
      .then((data) => {
        const q = data || {}
        const limit = typeof q.limit === 'number' ? q.limit : 0
        const remaining = typeof q.remaining === 'number' ? q.remaining : 0
        this.setData({
          quotaRemaining: remaining,
          quotaLimit: limit,
          quotaText: limit ? '今日剩余 ' + remaining + ' / ' + limit + ' 次' : '今日剩余 ' + remaining + ' 次',
        })
      })
      .catch((err) => {
        this.setData({ quotaText: '查询失败：' + api.describeError(err) })
      })
  },

  checkHealth() {
    api
      .health()
      .then(() => {
        this.setData({ healthOk: true, healthText: '服务正常' })
      })
      .catch((err) => {
        this.setData({ healthOk: false, healthText: api.describeError(err) })
      })
  },

  onCopyConfig() {
    const text = 'CLOUD_ENV=' + this.data.cloudEnv + '\nSERVICE_NAME=' + this.data.serviceName
    wx.setClipboardData({
      data: text,
      success: () => {
        wx.showToast({ title: '已复制', icon: 'none' })
      },
    })
  },
})
