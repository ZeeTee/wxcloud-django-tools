/**
 * 去 AI 味 · 前端配置文件
 *
 * ★ 用户只需要改这个文件 ★
 * 下面标了 REPLACE_ME 的两项必须改，否则请求一定失败。
 * 改完保存，用微信开发者工具重新编译即可，不需要构建、不需要 npm。
 */
module.exports = {
  // 微信云托管的环境ID（控制台 → 云托管 → 环境设置 → 环境ID），形如 prod-xxxx
  // 注意：这里要填「环境ID」，不是环境名称；填错会报 errCode -601027 Environment not found
  CLOUD_ENV: 'REPLACE_ME_CLOUD_ENV',

  // 云托管的服务名（控制台 → 服务管理 → 服务列表 → 服务名称）
  // 会作为请求头 X-WX-SERVICE 发出；填错会报 errCode -601031
  SERVICE_NAME: 'REPLACE_ME_SERVICE_NAME',

  // 轮询参数
  POLL_INTERVAL_MS: 1200, // 每次轮询的间隔
  POLL_TIMEOUT_MS: 90000, // 从创建任务开始算的总时长上限，超过就判失败

  // 输入上限（与后端一致）
  MAX_INPUT_CHARS: 5000,

  // 版本号，只用于「关于」页展示
  VERSION: '1.0.0',
}
