/**
 * app.js
 *
 * 唯一职责：全局初始化一次 wx.cloud，并把跨页面传递的数据挂到 globalData。
 *
 * 关键点（官方文档）：
 *  - 使用 wx.cloud.callContainer 之前「一定要 init 一下，全局执行一次即可」；
 *  - init 的 env 与云托管无关，可以留空；
 *  - 真正决定打到哪个云托管环境的是 callContainer 的 config.env（在 utils/api.js 里）。
 */
const api = require('./utils/api.js')

App({
  globalData: {
    // 首页 → 结果页 的跳转载荷，只放 recordId（正文走本地存储，避免 URL 超长）
    resultPayload: null,
  },

  onLaunch() {
    // 全局一次。init 里的网络请求/异步初始化如果没完成，
    // utils/api.js 会自动等待并重试（最多 3 次，每次间隔 300ms）。
    api.initCloud()
  },

  onError(err) {
    // 兜底日志，方便在开发者工具里定位问题；生产环境可以直接删掉。
    console.error('[app onError]', err)
  },

  onUnhandledRejection(res) {
    console.error('[app onUnhandledRejection]', res && res.reason)
  },
})
