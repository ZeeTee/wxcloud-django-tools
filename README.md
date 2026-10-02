# wxcloudrun-django · 微信云托管 Django（去 AI 味版）

微信云托管 Django 框架模板 + **去 AI 味（去 AI 腔）接口**。

在小程序里粘贴一段疑似 AI 写的中文，返回「AI 味体检报告」和「改写后的人话」。

> **本仓库只包含后端。** 小程序前端是独立项目，通过 `wx.cloud.callContainer`
> 调用这里的 `/api/*`，两边只靠下面第六节的接口契约耦合。

```
deai/          去 AI 味应用（引擎 + 5 个 API + 异步任务）
wxcloudrun/    项目配置 + 模板原有的计数器示例（保持可用）
```

---

## 一、这个仓库是什么

上游是微信云托管的官方 Django 模板（`WeixinCloud/wxcloudrun-django`），
本仓库在保留**原有计数器示例与主页**的前提下，做了两件事：

**1. 加入 `deai` 应用** —— 去 AI 味的完整后端：

| 能力 | 说明 | 耗时 | 消耗额度 |
| --- | --- | --- | --- |
| AI 味体检 | 命中 300+ 条词库与 36 条结构正则，给 0-100 分、分类明细、逐条原因与建议 | 毫秒级 | 否 |
| 规则层改写 | 安全替换/删除高置信度的 AI 套话 | 毫秒级 | 否 |
| AI 深度改写 | 调大模型重写全文（拆长句、调节奏、给判断） | 10-60 秒 | 每天限额 |

其中「AI 深度改写」由 **skill** 驱动（当前是 Humanizer v4.1）：

* skill 原文是为**有文件工具的 agent** 写的，正文里到处写着「加载 `references/banned-words.md`」。
  产品后台是单向 LLM 调用，模型没法自己读文件。所以 `deai/skills/registry.py` 会先**编译**它：
  把加载指令改写成附录引用、把 references 正文注入 prompt、追加场景覆盖与输出协议。
  **漏掉这一步，模型就会看到一条它无法执行的指令。**
* 输出用 `<REPORT>` / `<REWRITTEN>` 标签分隔，**不用 markdown 标题**——模型完全可能在改写正文里
  写出同样的标题，导致解析错位。解析器多级容错，最差情况整段当正文，绝不因解析失败就丢弃产出。
* 编译后的 system prompt 约 1.9 万字（含禁用词表与结构清单，不含示例库）。示例库标了 `fewshot`，
  暂不注入以控制成本，需要时改 `skill.json` 即可。
* `skill=legacy` 保留旧硬编码提示词作为**灰度与故障回退通道**，skill 出问题可一键切回。

**2. 修掉模板里不能上生产的地方**：

| 原模板 | 问题 | 现在 |
| --- | --- | --- |
| `from django.conf.urls import url` | 该 API 在 **Django 4.x 已被移除**，直接 ImportError | 改用 `re_path` / `path` |
| `url(r'^^api/count(/)?$', ...)` | 正则开头**两个 `^`**，`/api/count` 永远匹配不上，请求掉进 catch-all 返回主页 HTML | 改为一个 `^` |
| Django 3.2.8 | 已停止安全更新 | 升到 **4.2 LTS** |
| `runserver` | 开发服务器，单线程，不能上生产 | `gunicorn`（2 worker × 4 线程） |
| `DEBUG = True` + 硬编码 `SECRET_KEY` | 生产环境泄露堆栈、密钥进仓库 | 环境变量驱动，默认生产安全值 |
| `os.environ.get("MYSQL_ADDRESS").split(':')` | 变量缺失直接 `AttributeError`，本地起不来 | 缺失则回落 SQLite |
| 日志写 `logs/*.log` | 容器重启即丢，且云托管只采集 stdout，控制台看不到 | 统一打 stdout |
| `TIME_ZONE = 'UTC'` | 容器已是 Asia/Shanghai，日志时间差 8 小时 | 对齐 Asia/Shanghai |
| `Counters` 无迁移，建表 SQL 只在模板部署时跑 | 自建服务 / 本地开发时表不存在，`/api/count` 报错 | 补标准迁移 + `--fake-initial` 兼容已存在的表 |
| `Counters` 模型四处 bug | `models.AutoField` 少括号、`IntegerField(max_length=)`、`datetime.now()` 类定义时求值、`__str__` 引用不存在的 `self.title` | 全部修掉（不需要迁移） |

> `Counters` 模型的改动只涉及 Python 层，**不影响表结构**，因此升级时不需要额外迁移。

---

## 二、目录结构

```
.
├── Dockerfile                  python:3.11-slim + gunicorn
├── container.config.json       模板部署的「服务设置」初值（自建服务请忽略）
├── requirements.txt            Django 4.2 / gunicorn / PyMySQL
├── manage.py
├── .env.example                本地开发的环境变量样例
├── wxcloudrun/                 项目配置 + 模板原有计数器示例
│   ├── settings.py             环境变量驱动，MySQL 缺失则回落 SQLite
│   ├── urls.py                 deai 接口 + count + 主页
│   ├── views.py / models.py    计数器示例（保留）
│   ├── templates/index.html    模板欢迎页（保留）
│   └── migrations/0001_initial.py   为 Counters 补的迁移
├── deai/                       ★ 去 AI 味应用
│   ├── engine/                 引擎（不依赖 Django、不联网，可单独测试）
│   │   ├── rules.py            词库加载、命中检测、可安全执行的替换
│   │   ├── detector.py         评分与体检报告
│   │   ├── rewriter.py         LLM 编排 + 保真校验 + skill 调度
│   │   ├── llm.py              OpenAI 兼容客户端（纯标准库）
│   │   ├── prompts.py          旧版硬编码 prompt（legacy 回退用）
│   │   ├── textutil.py         分句 / 标点清理 / 节奏统计
│   │   └── lexicon/rules.json  177 条替换 + 95 条标记 + 36 条结构正则
│   ├── skills/                 ★ skill 子系统
│   │   ├── registry.py         扫描并编译 skill（把「加载 references」编译成注入）
│   │   ├── output.py           解析 <REPORT>/<REWRITTEN> 协议（多级容错）
│   │   └── humanizer/          Humanizer v4.1
│   │       ├── SKILL.md            主提示词（与 agent 版保持一致）
│   │       ├── skill.json          产品化清单：注入规则 / 场景 / 轮次
│   │       ├── references/         禁用词表 · 结构清单 · 示例库
│   │       └── overrides/          场景覆盖（general / xhs）
│   ├── views.py                6 个接口，统一响应信封
│   ├── models.py               RewriteTask / QuotaUsage
│   ├── auth.py                 从云托管请求头取 openid
│   ├── quota.py                每日额度
│   └── tasks.py                后台线程池 + 超时判失败
├── scripts/
│   ├── dev.sh                  本地一键起服务
│   └── smoke_api.py            接口端到端冒烟（无需联网）
└── tests/
    ├── test_engine.py          引擎单测（不需要 Django）
    └── test_skills.py          skill 编译与输出解析单测

docs/                          设计依据（不是代码，但值得留档）
├── 去AI味-工程素材包.md        中文 AI 腔特征清单、词库来源、两版提示词原型
└── 微信云托管-实现规范笔记.md    callContainer / Django 模板 / 鉴权 / 计费的官方依据
```

> `docs/` 被 `.dockerignore` 排除，不会进镜像；它只是留给维护者的背景材料。
> 词库（`deai/engine/lexicon/rules.json`）的来源与取舍、以及为什么
> `callContainer` 必须异步，都能在这两份文档里找到出处。

---

## 三、本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 填 LLM_API_KEY
./scripts/dev.sh              # → http://127.0.0.1:8080
```

不配 `MYSQL_ADDRESS` 时自动用 SQLite（`data/deai.sqlite3`），**不需要先装 MySQL**。

自测：

```bash
curl -s localhost:8080/api/health
# {"ok": true, "data": {"status": "up", "llmConfigured": true}}

curl -s localhost:8080/api/analyze -H 'Content-Type: application/json' \
  -d '{"text":"首先，AI 助手能够极大地提升工作效率。综上所述，它很有价值。"}'

# 模板原有的计数器仍在（注意：原模板这里因为正则 bug 是访问不到的）
curl -s localhost:8080/api/count
# {"code": 0, "data": 0}
```

跑测试（**都不需要联网**）：

```bash
python3 -m unittest discover -s tests -t .   # 引擎单测 43 项
python3 scripts/smoke_api.py                 # 接口冒烟 33 项
```

`smoke_api.py` 会故意把模型地址指向一个连不上的端口，从而把「建任务 → 后台线程 →
轮询 → 失败兜底 → 配额扣减 → 鉴权」整条链路真实走一遍，同时回归模板原有的
`/api/count` 与主页。

---

## 四、部署到微信云托管

**前置**：已注册小程序，并在[微信云托管控制台](https://cloud.weixin.qq.com/)开通。

1. **建环境**：新建环境，记下**环境 ID**（形如 `prod-xxxx`）。
2. **建服务**：新建服务，服务名例如 `deai-api`。
3. **部署**：
   - 方式 A（推荐）：绑定本仓库，构建目录填 `.`。
   - 方式 B：把仓库打成 zip 上传。
   - **端口填 `80`**（必须与 Dockerfile 的 `EXPOSE 80` 一致，否则 `Readiness probe failed`）。
4. **配环境变量**（服务设置 → 环境变量）—— 至少这三个：

   | 变量 | 值 |
   | --- | --- |
   | `LLM_API_KEY` | 你的模型密钥 |
   | `LLM_BASE_URL` | 如 `https://api.deepseek.com/v1` |
   | `LLM_MODEL` | 如 `deepseek-chat` |

   再补一个随机密钥：`DJANGO_SECRET_KEY`
   （`python3 -c "import secrets;print(secrets.token_urlsafe(50))"`）。

   > 环境变量在**构建阶段读不到**，只在运行时注入，所以不要在 Dockerfile 里读。

   > 如果要用云托管内 MySQL：控制台开通后会自动注入 `MYSQL_ADDRESS` 等变量，
   > 本项目无需额外配置。**多副本必须开 MySQL**，否则各副本任务状态不一致。

5. **发布**：保存后发布版本，访问 `https://<域名>/api/health` 确认
   `llmConfigured: true`。

6. **小程序前端**：前端是**独立项目**，不在本仓库。在那边配置：

   ```js
   module.exports = {
     CLOUD_ENV: 'prod-xxxx',   // 第 1 步的环境 ID（不是环境名）
     SERVICE_NAME: 'deai-api', // 第 2 步的服务名
   }
   ```

   并把小程序后台的**基础库最低版本设为 ≥ 2.23.0**（否则 `callContainer` 不可用）。
   前端不需要配「服务器域名」——`callContainer` 免域名校验、免备案。

7. **联调**：在开发者工具里跑一次「关于」页的配置自检，确认能打通。

> ⚠️ `container.config.json` 只在「控制台一键模板部署」那一次生效；自己新建服务 +
> 传代码包时它会被忽略，端口/规格/环境变量都要在控制台手填。

---

## 五、环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 空 | **必填**，模型密钥。只在服务端，绝不下发到小程序 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容端点 |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `LLM_TIMEOUT` | `50` | 秒，会被夹到 ≤55 |
| `LLM_PRICE_CACHE_IN` | `0.04` | 缓存命中输入单价（元/百万 token） |
| `LLM_PRICE_IN` | `2.0` | 缓存未命中输入单价（元/百万 token） |
| `LLM_PRICE_OUT` | `8.0` | 输出单价（元/百万 token） |
| `DJANGO_SECRET_KEY` | 不安全默认值 | **生产必须换** |
| `DJANGO_DEBUG` | `false` | |
| `DJANGO_ALLOWED_HOSTS` | `*` | 云托管 Host 不固定，默认放开 |
| `DEAI_MAX_INPUT_CHARS` | `5000` | 单次输入上限 |
| `DEAI_DAILY_LIMIT` | `20` | 每人每天的 AI 改写次数（规则层不限） |
| `DEAI_TASK_TIMEOUT_SECONDS` | `120` | 超时仍无结果的任务判为失败 |
| `DEAI_WORKERS` | `4` | 后台改写线程数 |
| `DEAI_SYNC_WAIT_SECONDS` | `12` | 混合模式：同步等待多久，超时才转轮询；`0` 为纯异步 |
| `DEAI_ALLOW_ANONYMOUS` | =`DEBUG` | **生产必须 `false`**，否则公网可白嫖 |
| `MYSQL_ADDRESS` | 空 | 配了用 MySQL，不配用 SQLite |
| `MYSQL_DATABASE` | `django_demo` | |
| `MYSQL_USERNAME` / `MYSQL_USER` | `root` | 两种命名都支持 |
| `MYSQL_PASSWORD` | 空 | |

---

## 六、接口

### 去 AI 味（统一信封）

成功 `{"ok":true,"data":{...}}`，失败 `{"ok":false,"error":{"code":"XXX","message":"人话"}}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 探活，附带 `llmConfigured` 与已加载的 skill |
| GET | `/api/skills` | 列出可用 skill（前端应据此动态渲染，不要写死选项） |
| POST | `/api/analyze` | `{text}` → 体检报告 + 规则改写，同步毫秒级，不消耗额度 |
| POST | `/api/rewrite` | `{text, mode, skill}` → **混合模式**：优先同步返回结果，超时才给 `taskId` 轮询 |
| GET | `/api/task/<id>` | 轮询：`status` ∈ `pending/running/done/failed` |
| GET | `/api/quota` | `{used, limit, remaining}` |
| GET | `/api/usage` | 账户余额 + 今日用量汇总（任务数、token、费用） |

* `mode` 取 `general`（通用）或 `xhs`（小红书），决定 skill 的场景覆盖。
* `skill` 默认 `humanizer`；传 `legacy` 可回退到旧的硬编码提示词。传不存在的 skill 会返回
  `SKILL_NOT_FOUND`——**刻意不静默兜底**，否则用户以为在用新 skill、实际跑的是别的，极难排查。
* 任务完成时额外返回：`skill`、`skillVersion`、`llmReport`（模型产出的检测报告）、
  `protocolOk`。`protocolOk: false` 表示模型没遵守输出协议、走了容错解析——不影响使用，
  但值得监控：它变多说明 prompt 需要调整。

错误码：`TEXT_EMPTY`、`TEXT_TOO_LONG`、`QUOTA_EXCEEDED`、`UNAUTHORIZED`、`SKILL_NOT_FOUND`、
`LLM_NOT_CONFIGURED`、`TASK_NOT_FOUND`、`FORBIDDEN`、`METHOD_NOT_ALLOWED`、`INTERNAL`。

### 费用怎么算的（重要）

**用响应里的 `usage` 字段算，不用「调用前后查两次余额算差值」。** 后者实测行不通：

* 余额接口只返回 **2 位小数**（`7.40`），一次 humanizer 调用约 **0.0014 元**，远小于最小刻度；
* 实测调用 11704 token 后等 30 秒，余额纹丝不动。

`usage` 随响应返回、精确到 token，还区分缓存命中与未命中。任务完成后
`/api/task/<id>` 会带上：

```json
"usage": {
  "promptTokens": 11591, "completionTokens": 73, "totalTokens": 11664,
  "cacheHitTokens": 11392, "cacheHitRate": 0.9828, "costCNY": 0.0014
}
```

**缓存是这个功能成本的关键**：humanizer 的 system prompt 固定不变，实测
**缓存命中率 98%**，而缓存命中价与未命中价**差 50 倍**。响应里也给出了
`breakdown`（缓存命中/未命中/输出各占多少），便于定位成本去向。

估算单价按 `LLM_PRICE_*` 环境变量算，默认取 DeepSeek 高峰价（宁可高估）；
空闲时段是高峰的一半，要更准就按当前时段改。

余额接口仍然保留（`/api/usage`），但只用于「够不够用」的粗粒度监控。

### 模板原有：计数器示例（保持原格式）

```bash
curl https://<域名>/api/count
curl -X POST -H 'content-type: application/json' \
  -d '{"action": "inc"}' https://<域名>/api/count
```

响应是模板自己的 `{"code": 0, "data": 42}` 格式（**没有**改写成 deai 的信封，
以免破坏已有调用方）。`action` 取 `inc` 或 `clear`。

---

## 七、深度改写为什么是「混合模式」

`wx.cloud.callContainer` 单次请求上限 **15 秒**，并且小程序**切到后台 5 秒后请求会被
系统杀掉**（`fail interrupted`）。原设计因此走纯异步：立刻返回 `taskId`，前端轮询。

但**实测一次改写只要 0.5-2.3 秒**（缓存命中时更快），纯异步其实是过度设计——用户白白
多等一次轮询往返。所以改成混合模式：

```
POST /api/rewrite
  ├─ 同步等 ≤ DEAI_SYNC_WAIT_SECONDS（默认 12 秒，给 15 秒上限留 3 秒余量）
  │    └─ 等到了 → 直接返回 status=done + llmText + usage   ← 绝大多数请求走这里
  └─ 超时 → 返回 status=pending，任务继续在后台跑，前端轮询 /api/task/<id>
```

前端只需要判断 `status`：`done` 直接用，其它值才去轮询。**响应里始终带 `rulesText`
和体检报告**，所以即使模型失败或超时，用户手里也已经有一份可用的改写稿。

设 `DEAI_SYNC_WAIT_SECONDS=0` 可以退回纯异步（前端不必改，仍按 `status` 分支即可）。

引擎的评分与词库细节见 `deai/engine/`，其中：

* **评分**：严重度加权（high=3 / medium=1.4 / low=0.6，结构问题 ×1.5）换算成每百字
  加权命中密度，再映射到 0-100；命中 1 条高危结构问题至少 45 分，2 条至少 70 分。
* **只建议不自动改**：`conditional` 共 92 条，包括「首先」（教程步骤里合理）、
  「不可否认」（作定语时改成「确实」会变病句）、「精益求精」等成语（作定语时删掉
  会让句子缺成分）。规则层宁可改得少，也不能产出病句。

---

## 八、已知限制

- **容器重启会杀掉正在跑的改写线程**。任务状态已落库，超过
  `DEAI_TASK_TIMEOUT_SECONDS` 会判失败，用户重试即可。最小副本设为 0 时冷启动约几秒。
- **多副本必须开 MySQL**，否则默认 SQLite 各副本不共享，轮询可能查不到任务。
- **额度计数不是严格原子的**（`F()` 自增，但「先读后判」有极小竞争窗口）。作为免费
  额度够用，要严格计费请换成 `select_for_update` + MySQL。
- **规则层改写不保证通顺**，它的定位是「兜底 + 快速见效」；真正自然还得靠大模型那层。
- **不做流式输出**：`callContainer` 不支持 SSE，想要打字机效果得走 WebSocket
  （`connectContainer`）或自建公网域名。
- **改写可能丢信息**：改写结果会过一道保真校验（对比数字、字数比例）并在界面提示，
  但**请务必人工再读一遍再发布**。
- 模型密钥若写进 `container.config.json` 会进仓库，**请只用控制台环境变量**。

---

## 九、上游模板

- 微信云托管快速开始：<https://developers.weixin.qq.com/miniprogram/dev/wxcloudrun/src/basic/guide.html>
- 本地调试指南：<https://developers.weixin.qq.com/miniprogram/dev/wxcloudrun/src/guide/debug/>
- Dockerfile 最佳实践：<https://developers.weixin.qq.com/miniprogram/dev/wxcloudrun/src/scene/build/speed.html>

## License

[MIT](./LICENSE)
