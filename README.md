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
* **禁用词表是单一数据源**：`references/banned-words.md` 不只是给模型看的参考，
  规则引擎启动时会解析它（`deai/engine/skill_lexicon.py`）并编入体检词库。
  否则会出现「免费体检说这篇挺像人写的，深度改写却把『仿佛』『眼中闪过一丝』全改掉」
  这种自相矛盾。这些词**只标记、不自动替换**——「坚定」「仿佛」机械替换会直接毁句子。
  解析时会过滤表头、参考资料链接和 `xxx` 占位模板，避免污染报告。

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
│   ├── init_db.py              建库 + 建表 + 校验（幂等）
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
python3 -m unittest discover -s tests -t .   # 引擎与 skill 单测，111 项
python3 scripts/smoke_api.py                 # 接口冒烟，58 项
```

`smoke_api.py` 会故意把模型地址指向一个连不上的端口，从而把「建任务 → 后台线程 →
轮询 → 失败兜底 → 配额扣减 → 鉴权」整条链路真实走一遍，同时回归模板原有的
`/api/count` 与主页。

### 数据库初始化

`scripts/init_db.py` 把「建库 + 建表 + 校验」合成一条命令。**Django 的 `migrate`
只建表不建库**——数据库不存在时连接阶段就失败了，所以这三步缺一不可：

```bash
python scripts/init_db.py                 # 建库 + 建表 + 校验
python scripts/init_db.py --dry-run       # 只打印将要做的事
python scripts/init_db.py --print-sql     # 只打印迁移 SQL（交给 DBA 审核 / 手工建表）
python scripts/init_db.py --skip-db       # 跳过建库（库已存在或无 CREATE 权限）
python scripts/init_db.py --check-only    # 只校验业务表是否齐全
```

幂等，可反复执行。本地没配 `MYSQL_ADDRESS` 时会走 SQLite：建库这步自动跳过，
`migrate` 直接把 `.sqlite3` 文件建出来。

> `--print-sql` 输出的 SQL 方言**取决于当前配置的数据库**：配了 MySQL 就是 MySQL 语法，
> 否则是 SQLite 语法。要生成 MySQL 的建表语句，请带上 `MYSQL_ADDRESS` 运行。

#### 两个实测踩到的坑

**1. 字符集必须是 `utf8mb4`，不能是 `utf8`**

腾讯云 CynosDB 开通时给的库默认是 `utf8`（即 utf8mb3，最多 3 字节）。
实测 `SELECT CONVERT('😀' USING utf8)` → `'?'`：**emoji 会静默变成问号**，
用户输入一个 emoji 就丢字符，而且不报错。

`init_db.py` 会检测库的字符集，不对就自动 `ALTER DATABASE` 改成 utf8mb4；
已有表可以用 `--fix-tables` 一起转换（注意 `ALTER TABLE` 会锁表，大表慎用）。

**2. Django 4.2 不支持 MySQL 5.7**

Django 从 4.2 起要求 **MySQL 8.0+**（[ticket #33718](http://code.djangoproject.com/ticket/33718#comment:3)），
连上 5.7 会直接抛 `NotSupportedError: MySQL 8 or later is required`。
而云托管/CynosDB 上仍可能是 5.7 实例。

打开 `MYSQL_ALLOW_57=true` 可以绕过版本检查。本项目在 5.7.18 上实测通过：
连接、中文与 emoji 往返、以及全部建表 DDL 都正常——模型只用了
`varchar` / `longtext` / `integer` / `bigint` / `datetime(6)` / `numeric` / `date`
这些基础类型，没有 8.0 专有语法。

> ⚠️ 但这**仍是官方未支持的组合**：Django 不会为 5.7 做兼容测试，将来用到窗口函数、
> 表达式默认值之类的特性时会踩坑。**建议尽快把实例升级到 MySQL 8**，而不是长期开着这个开关。

容器启动时也会自动 `migrate`（见 Dockerfile 的 CMD），所以**线上通常不需要手动跑**这个脚本；
它主要服务于本地连远程库、迁移到新实例、以及人工审核 SQL 这几种场景。

---

## 四、部署到微信云托管

**前置**：已注册小程序，并在[微信云托管控制台](https://cloud.weixin.qq.com/)开通。

1. **建环境**：新建环境，记下**环境 ID**（形如 `prod-xxxx`）。
2. **建服务**：新建服务，服务名例如 `deai-api`。
3. **部署**：
   - 方式 A（推荐）：绑定本仓库，构建目录填 `.`。
   - 方式 B：把仓库打成 zip 上传。
   - **端口填 `80`**（必须与 Dockerfile 的 `EXPOSE 80` 一致，否则 `Readiness probe failed`）。
4. **配环境变量**（服务设置 → 环境变量）—— 最少这三个：

   | 变量 | 值 |
   | --- | --- |
   | `DJANGO_SECRET_KEY` | 随机串，生成：`python3 -c "import secrets;print(secrets.token_urlsafe(50))"` |
   | `LLM_PROVIDER` | `deepseek`（默认）或 `openrouter` |
   | `DEEPSEEK_API_KEY` | 对应 provider 的密钥（选 openrouter 就填 `OPENROUTER_API_KEY`） |

   其余变量都有默认值，完整清单见第五节。

   > 环境变量在**构建阶段读不到**，只在运行时注入，所以不要在 Dockerfile 里读。

5. **开数据库（强烈建议）**：控制台 → MySQL → 开通。

   开通后会自动注入 `MYSQL_ADDRESS` / `MYSQL_USERNAME` / `MYSQL_PASSWORD`，
   本项目无需改代码，容器启动时会自动 `migrate` 建表。

   **为什么建议开**：不开的话走容器内 SQLite，而 `minNum: 0` 会在 30 分钟无请求后
   缩容到 0——容器一重启，**每日额度表就归零，配额限制形同虚设**；多副本时各副本
   数据还互不可见，轮询会查不到任务。

   想确认建表结果，可在容器内执行：`python scripts/init_db.py --check-only`
   （该脚本也能手动建库建表，见第三节）。

6. **开手机号授权（想让用户从 5 次提到 10 次才需要）**：控制台 → 云调用：

   1. 打开「**开放接口服务**」开关；
   2. 在「微信令牌权限配置」里加路径白名单：`/wxa/business/getuserphonenumber`
      （**只填路径**，不带 `?` 和参数）；
   3. **打开开关后重新构建一次服务版本**——该开关按「版本创建时刻」生效，
      老版本即使开关打开了也不生效。

   开了之后容器内用 HTTP 请求 `api.weixin.qq.com` 且不带 access_token，
   由旁加载组件自动注入，**连 `WX_SECRET` 都不用配**。建议仍配 `WX_APPID`，
   用于校验手机号回包的 `watermark.appid`。

   > ⚠️ **先确认两个硬门槛，否则这一步白做**：
   > - **资质**：该能力「针对**非个人主体，且完成了认证**的小程序开放」。
   >   个人主体小程序调不通，服务端会返回 `48001`。
   > - **计费**：自 2023-08-28 起**每次成功调用 0.03 元**，每个小程序账号有
   >   **1000 次体验额度**（正式版/体验版/开发版共用），用完后必须买资源包。
   >   在「微信公众平台 → 付费管理」查看余额。

   > 注意：开启「开放接口服务」后 `sns/jscode2session` 会因白名单报错。
   > 本项目已不依赖它，无影响；详见第六节「手机号授权」。

7. **发布**：保存后发布版本，访问 `https://<域名>/api/health` 确认
   `llmConfigured: true`。

8. **小程序前端**：前端是**独立项目**，不在本仓库。在那边配置：

   ```js
   module.exports = {
     CLOUD_ENV: 'prod-xxxx',   // 第 1 步的环境 ID（不是环境名）
     SERVICE_NAME: 'deai-api', // 第 2 步的服务名
   }
   ```

   并把小程序后台的**基础库最低版本设为 ≥ 2.23.0**（否则 `callContainer` 不可用）。
   前端不需要配「服务器域名」——`callContainer` 免域名校验、免备案。

9. **联调**：在开发者工具里跑一次「关于」页的配置自检，确认能打通。

> ⚠️ `container.config.json` 只在「控制台一键模板部署」那一次生效；自己新建服务 +
> 传代码包时它会被忽略，端口/规格/环境变量都要在控制台手填。

---

## 五、环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `deepseek` | 选 `deepseek` 或 `openrouter`，切换只改这一行 |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 密钥（`provider=deepseek` 时用） |
| `OPENROUTER_API_KEY` | 空 | OpenRouter 密钥（`provider=openrouter` 时用） |
| `LLM_API_KEY` | 空 | 兼容旧配置：上面两个专属 key 都没配时回退读它 |
| `LLM_BASE_URL` | 空 | 留空用 provider 默认端点 |
| `LLM_MODEL` | 空 | 留空用 provider 默认模型 |
| `LLM_TIMEOUT` | `50` | 秒，会被夹到 ≤55 |
| `USD_CNY_RATE` | `7.2` | OpenRouter 的美元成本折算成人民币的汇率 |
| `LLM_PRICE_CACHE_IN` | `0.04` | 缓存命中输入单价（元/百万 token），仅本地价格表用 |
| `LLM_PRICE_IN` | `2.0` | 缓存未命中输入单价（元/百万 token） |
| `LLM_PRICE_OUT` | `8.0` | 输出单价（元/百万 token） |
| `DJANGO_SECRET_KEY` | 不安全默认值 | **生产必须换** |
| `DJANGO_DEBUG` | `false` | |
| `DJANGO_ALLOWED_HOSTS` | `*` | 云托管 Host 不固定，默认放开 |
| `DEAI_MAX_INPUT_CHARS` | `5000` | 单次输入上限 |
| `DEAI_DAILY_LIMIT_ANONYMOUS` | `5` | 未授权手机号用户每天的 AI 改写次数 |
| `DEAI_DAILY_LIMIT_VERIFIED` | `10` | 已授权手机号用户每天的 AI 改写次数 |
| `DEAI_DAILY_LIMIT` | `0` | 旧变量：>0 时两档都用它（兼容老部署） |
| `WX_OPENAPI_ENABLED` | `true` | 用云调用（开放接口服务）调手机号接口，**免 access_token** |
| `WX_OPENAPI_BASE` | `http://api.weixin.qq.com` | 云调用地址，**必须 HTTP**（见下方「手机号授权」） |
| `WX_APPID` | 空 | 小程序 AppID。**建议配**：用于校验手机号回包的 `watermark.appid` |
| `WX_SECRET` | 空 | 小程序 AppSecret。**云调用模式下不需要**；仅自管 token / 旧 code2Session 用 |
| `WX_LOGIN_TIMEOUT` | `10` | 调微信接口的超时（秒） |
| `DEAI_TASK_TIMEOUT_SECONDS` | `120` | 超时仍无结果的任务判为失败 |
| `DEAI_WORKERS` | `4` | 后台改写线程数 |
| `DEAI_SYNC_WAIT_SECONDS` | `12` | 混合模式：同步等待多久，超时才转轮询；`0` 为纯异步 |
| `DEAI_ALLOW_ANONYMOUS` | =`DEBUG` | **生产必须 `false`**，否则公网可白嫖 |
| `MYSQL_ADDRESS` | 空 | 配了用 MySQL，不配用 SQLite |
| `MYSQL_ALLOW_57` | `false` | 允许连 MySQL 5.7（见下方说明，**建议升级而非长期开启**） |
| `MYSQL_DATABASE` | `django_demo` | |
| `MYSQL_USERNAME` / `MYSQL_USER` | `root` | 两种命名都支持 |
| `MYSQL_PASSWORD` | 空 | |

---

## 六、接口

### 统一信封

```
成功  {"ok": true,  "data": { ... }}
失败  {"ok": false, "error": { "code": "XXX", "message": "人话" }}
```

前端按 `error.code` 分支处理，`message` 可直接展示给用户。

### 接口一览

| # | 方法 | 路径 | 作用 | 上送参数 | 消耗额度 |
| --- | --- | --- | --- | --- | --- |
| 1 | GET | `/api/health` | 部署自检：密钥是否配好、当前 provider、已加载 skill、prompt 指纹、手机号授权是否就绪 | 无 | 否 |
| 2 | GET | `/api/skills` | 列出可用 skill 与场景/强度选项（前端据此动态渲染，别写死） | 无 | 否 |
| 3 | GET | `/api/quota` | 查今日剩余改写次数 | 无 | 否 |
| 4 | GET | `/api/usage` | 账户余额 + 今日用量汇总（任务数、token、费用） | 无 | 否 |
| 5 | POST | `/api/analyze` | AI 味体检 + 规则层改写，毫秒级同步返回 | body `text` | **否** |
| 6 | POST | `/api/rewrite` | AI 深度改写（混合模式：优先同步，超时转轮询） | body `text` `mode` `skill` `intensity` | **是** |
| 7 | GET | `/api/task/<taskId>` | 取改写结果（轮询或补查） | 路径 `taskId` | 否 |
| 8 | POST | `/api/feedback` | 对某次改写结果评价 | body `taskId` `rating` `reason` `comment` | 否 |
| 9 | GET | `/api/feedback/summary` | 当前用户的评价统计 | 无 | 否 |
| 10 | POST | `/api/auth/login` | 手机号授权，额度从 5 次提升到 10 次 | body `phoneCode` | 否 |
| 10 | GET/POST | `/api/count` | 模板原有的计数器示例（保持原格式，未改动） | POST body `action` | 否 |
| 11 | GET | `/` | 模板原有的欢迎页 | 无 | 否 |

路径**不带尾斜杠**（`APPEND_SLASH=False`，避免 callContainer 遇到 301 重定向）。

---

### 接口详解

#### POST /api/analyze —— 体检 + 秒改（免费）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | ✅ | 待处理文本，1 ~ `DEAI_MAX_INPUT_CHARS`（默认 5000）字 |

**返回 `data`**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `report` | object | 体检报告，结构见下方「公共结构 · report」 |
| `rulesText` | string | 规则层改写结果（安全替换/删除后的文本） |
| `rulesChanges` | int | 实际改动的处数 |

不调大模型、不消耗额度，**可以随便调**——前端每次输入都能实时预览。

---

#### POST /api/rewrite —— AI 深度改写（消耗额度）

| 字段 | 类型 | 必填 | 取值 | 说明 |
| --- | --- | --- | --- | --- |
| `text` | string | ✅ | 1 ~ 5000 字 | 待改写文本 |
| `mode` | string | | `general`（默认）/ `xhs` / `academic` / `official` | 场景，决定 skill 的场景覆盖 |
| `skill` | string | | `humanizer`（默认）/ `legacy` | 用哪个 skill；`legacy` 是旧硬编码提示词 |
| `intensity` | string | | `light` / `medium`（默认）/ `heavy` | 改写力度 |

非法值**一律回落默认**（不报错），只有 `skill` 传了不存在的值才返回 `SKILL_NOT_FOUND`——
这个刻意不兜底，否则用户以为在用新 skill、实际跑的是别的，极难排查。

**返回 `data`**（混合模式，两种形态）：

| 字段 | 出现时机 | 说明 |
| --- | --- | --- |
| `taskId` | 总是 | 任务 ID，用于 `/api/task/<id>` |
| `rulesText` | 总是 | 规则层结果（**模型失败时这是兜底内容**） |
| `report` | 总是 | 体检报告 |
| `quota` | 总是 | `{used, limit, remaining}` |
| `status` | 总是 | `done`=已同步拿到结果 / `pending`=请轮询 / `failed`=快速失败 |
| `llmText` 等 | `status=done` | 与 `/api/task/<id>` 完成态返回的字段完全一致 |

**前端只需判断 `status`**：`done` 直接用，其它值去轮询。响应里始终带 `rulesText`，
所以模型失败或超时也有一份可用结果。

---

#### GET /api/task/&lt;taskId&gt; —— 取结果

无上送参数（`taskId` 在路径里）。响应 `data`：

| 字段 | 出现时机 | 说明 |
| --- | --- | --- |
| `status` | 总是 | `pending` / `running` / `done` / `failed` |
| `rulesText` | 总是 | 规则层兜底文本 |
| `elapsedMs` | 总是 | 已耗时（毫秒） |
| `feedback` | 已评价时 | `{rating, reason}`，用于回填「你已评价」 |
| `llmText` | `done` | 改写后的正文 |
| `model` / `provider` | `done` | 实际用的模型与供应商 |
| `skill` / `skillVersion` / `intensity` | `done` | 实际生效的 skill 与强度 |
| `promptFingerprint` | `done` | 编译后 prompt 的指纹，用于定位版本 |
| `llmReport` | `done` | 模型自评的检测报告（legacy 模式为空） |
| `addedFacts` | `done` | 模型自报「补充了哪些原文没有的内容」 |
| `protocolOk` | `done` | 模型是否守住了输出协议；`false` 表示走了容错解析 |
| `usage` | `done` | token 用量与费用，结构见下方 |
| `warnings` | `done` | 保真校验提示数组（可能为空） |
| `error` | `failed` | 人话错误信息 |

---

#### POST /api/feedback —— 提交评价

| 字段 | 类型 | 必填 | 取值 | 说明 |
| --- | --- | --- | --- | --- |
| `taskId` | string | ✅ | | 要评价的任务 ID，**必须属于当前用户**（否则 403） |
| `rating` | string | ✅ | `good` / `bad` | 满意 / 不满意 |
| `reason` | string | | 见下方枚举 | 仅 `bad` 时有意义；`good` 时会被清空 |
| `comment` | string | | ≤1000 字 | 自由补充 |

`reason` 枚举（前端应从 `/api/feedback/summary` 的 `availableReasons` 读，别写死）：

```
added_facts   加了原文没有的内容      lost_info       丢了原文的信息
not_natural   还是很像 AI             changed_meaning 意思被改了
too_casual    改得太随意/口语         too_formal      改得太正式
too_long      变啰嗦了                too_short       变短了
other         其他
```

返回 `{accepted: true, created: bool, rating}`。`created=false` 表示覆盖了上次的评价
（同一任务同一用户只保留一条，重复提交视为「改主意」）。

---

#### POST /api/auth/login —— 手机号授权并提升额度

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `phoneCode` | string | ✅ | `open-type="getPhoneNumber"` 回调里的 `e.detail.code` |
| `code` | string | ❌ | **已废弃**，老的 `wx.login()` code，仅兼容旧前端 |

前端写法：

```html
<button open-type="getPhoneNumber" bindgetphonenumber="onPhone">授权手机号，每天 10 次</button>
```

```js
onPhone(e) {
  // errno 1400001 = 小程序手机号资源包额度不足，此时不会有 code
  if (e.detail.errno === 1400001) {
    return wx.showToast({ title: '授权服务繁忙，请稍后再试', icon: 'none' })
  }
  if (!e.detail.code) return wx.showToast({ title: '已取消授权', icon: 'none' })
  wx.cloud.callContainer({
    config: { env: '环境ID' },
    path: '/api/auth/login',
    header: { 'X-WX-SERVICE': '服务名', 'content-type': 'application/json' },
    method: 'POST',
    data: { phoneCode: e.detail.code },
  })
}
```

> ⚠️ **`e.detail.code` 必须传给 `phoneCode`**。`getPhoneNumber` 的 code 与
> `wx.login` 的 code **作用不同、不能混用**（官方原文），传错字段只会得到
> 一个「授权凭证已失效」。

返回 `{verified: true, phoneMasked: "138****8000", quota: {...}}`（**不回传完整手机号**）。
可能的错误码：`CODE_EMPTY`（400）、`WX_NOT_CONFIGURED`（503）、`WX_PHONE_FAILED`（400，
授权凭证过期／被拒／不属于本小程序／无手机号能力权限）、`NO_IDENTITY`（400，拿不到
openid，仅本地开发）。

**这个能力有两个硬门槛**

| 门槛 | 说明 |
| --- | --- |
| 资质 | 只对「**非个人主体，且完成了认证**的小程序」开放。个人主体调不通，返回 `48001` |
| 计费 | **每次成功调用 0.03 元**（2023-08-28 起）。每个账号 1000 次体验额度，正式/体验/开发版共用，用完要在「付费管理」买资源包 |

所以「授权后每天 10 次」不要做成诱导所有人狂点的弹窗，正常放一个入口即可；
额度耗尽时前端按钮回调里会拿到 `errno === 1400001`（此时不产生 code、不计费）。

**为什么用手机号而不是 `wx.login()`**

`openid` 是云托管**自动注入**的，未登录也有；`wx.login()` 换 openid 更是什么都证明不了——
谁都能触发，微信也不做任何校验，拿它当「登录」等于把 10 次额度白送。
手机号授权是**用户真的点了按钮、微信背书**的一次性 code（5 分钟有效、只能消费一次），
才有区分度。

**安全要点**

1. `getuserphonenumber` 回包里**没有 openid**，身份只能取请求头里的 `X-WX-OPENID`
   （云托管注入，前端改不了）。code 本身一次性、几分钟过期、由当前小程序会话下发。
2. 校验回包 `watermark.appid` 必须等于本小程序 AppID，否则拒绝（防拿到别人的号码）。
3. 手机号**打码存储**（`138****8000`）+ HMAC 指纹，库里不留完整号码。

**部署前置条件（不做就会一直报白名单错误）**

本项目默认走**云调用**（`WX_OPENAPI_ENABLED=true`），需要在云托管控制台：

1. 打开「云调用 → 开放接口服务」开关；
2. 在「微信令牌权限配置」里加白名单：`/wxa/business/getuserphonenumber`
   （**只填路径**，不带 `?` 和参数）；
3. **开关打开后重新构建一次服务版本**——这个开关是按「版本创建时刻」生效的。

云调用模式下容器内用 **HTTP** 请求 `api.weixin.qq.com` 且**不带 access_token**，
由旁加载组件自动注入，所以**连 AppSecret 都不用配**。

> ⚠️ 开启「开放接口服务」后 `sns/jscode2session` 会失效（它本身不带 access_token，
> 会被当成云调用去查白名单，而白名单里没有它）。本项目已不依赖 code2Session
> 判断身份，所以没有影响；只有老前端传 `code` 时才会踩到。
>
> 若确实无法开启开放接口服务，把 `WX_OPENAPI_ENABLED` 设为 `false`，
> 改走自管 access_token（需要配 `WX_SECRET`），token 缓存在
> `deai_wx_access_token` 表里给多副本共享。

### 用户身份与额度

| 状态 | 身份来源 | 每日额度 | 用户要做什么 |
| --- | --- | --- | --- |
| 未授权 | `X-WX-OPENID`（云托管自动注入） | **5 次** | **什么都不用做** |
| 已授权手机号 | 同一个 openid | **10 次** | 点「授权手机号」按钮一次 |

**一个容易误解的点**：微信小程序里 `openid` 是**静默获取**的，不需要任何授权弹窗，
云托管会自动把它注入到 `X-WX-OPENID` 请求头。所以**「不登录也能唯一区分用户」这件事
现在就已经做到了**，不需要额外方案，也不需要前端本地存储。

但反过来，这也意味着「未登录」和「已登录」**在身份上没有本质区别**——要区分两档额度，
必须有一个用户主动做过、且微信背书的动作，这就是手机号授权的作用。

**为什么匿名额度也记在后端**：有人会想「未登录就用前端本地存储计数」。但那样用户
**清一次小程序缓存次数就归零**，等于没有限制，而且前端数据可被篡改。用 openid 记在
后端，清缓存重置不了（要换微信号才行），代价只是一次数据库查询。

`/api/quota` 返回 `verified`、`anonymousLimit`、`verifiedLimit`、`phoneMasked`，
前端据此显示「授权手机号后每天可用 10 次」的引导。未授权额度用尽时，
`QUOTA_EXCEEDED` 的错误文案里也会带上这个引导。



**`report`（体检报告）**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `score` | int | AI 味分数 **0-100，越高越像 AI** |
| `level` | string | `low` / `medium` / `high` |
| `verdict` | string | 一句话结论，如「AI 味偏重（89 分）：主要是序数词分点骨架 3 处」 |
| `totalHits` | int | 命中总数 |
| `charCount` | int | 字符数 |
| `categories` | array | 按类聚合：`{key, name, count, severity, samples[]}` |
| `advice` | array | 可执行的改写建议（字符串数组） |
| `stats` | object | 节奏统计：`sentenceCount` `paragraphCount` `avgSentenceLen` `sentenceLenStd` `longSentenceRatio` `rhythmVariance` |
| `hits` | array | 逐条命中，结构见下 |

**`report.hits[]`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `start` / `end` | int | 在**原文**中的字符下标（前端用来高亮） |
| `text` | string | 命中的原文片段 |
| `kind` | string | `replace`（已自动改写）/ `flag`（仅标记） |
| `severity` | string | `high` / `medium` / `low` |
| `reason` | string | 为什么它是 AI 味 |
| `suggestion` | string \| null | 建议替换成什么（可为 null） |
| `category` / `categoryName` | string | 类别 key 与中文名 |
| `ruleId` | string | 规则 ID，便于定位词库条目 |

**`usage`（任务完成时）**

```json
{
  "promptTokens": 15233, "completionTokens": 77, "totalTokens": 15310,
  "cacheHitTokens": 14592, "cacheHitRate": 0.9579, "costCNY": 0.002482
}
```

| 字段 | 说明 |
| --- | --- |
| `promptTokens` / `completionTokens` / `totalTokens` | 输入、输出、合计 token |
| `cacheHitTokens` / `cacheHitRate` | 命中 prompt 缓存的量——**这是成本的关键**，命中价与未命中价差 50 倍 |
| `costCNY` | 本次费用（元）。由供应商上报的美元成本折算，或按本地价格表估算 |

### 错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| `TEXT_EMPTY` | 400 | 文本为空 |
| `TEXT_TOO_LONG` | 400 | 超过 `DEAI_MAX_INPUT_CHARS` |
| `BAD_JSON` / `BAD_ENCODING` | 400 | 请求体不是合法 JSON / UTF-8 |
| `METHOD_NOT_ALLOWED` | 405 | 请求方法不对（返回的仍是 JSON 信封） |
| `SKILL_NOT_FOUND` | 400 | 传了不存在的 skill（刻意不兜底） |
| `CODE_EMPTY` | 400 | 登录时没传 code |
| `WX_NOT_CONFIGURED` | 503 | 服务端没配微信 AppID / AppSecret |
| `WX_LOGIN_FAILED` | 400 | 微信登录失败（code 无效、已用过、被风控等） |
| `OPENID_MISMATCH` | 400 | 登录换回的 openid 与当前请求身份不一致 |
| `BAD_RATING` | 400 | `rating` 不是 `good`/`bad` |
| `UNAUTHORIZED` | 401 | 拿不到调用方身份 |
| `FORBIDDEN` | 403 | 访问他人的任务 |
| `TASK_NOT_FOUND` | 404 | 任务不存在或已过期 |
| `QUOTA_EXCEEDED` | 429 | 今日额度用完 |
| `LLM_NOT_CONFIGURED` | 503 | 服务端没配模型密钥 |
| `INTERNAL` | 500 | 服务端异常 |

### 模板原有：计数器示例（保持原格式）

```bash
curl https://<域名>/api/count
curl -X POST -H 'content-type: application/json' \
  -d '{"action": "inc"}' https://<域名>/api/count
```

响应是模板自己的 `{"code": 0, "data": 42}` 格式（**没有**改写成 deai 的信封，
以免破坏已有调用方）。`action` 取 `inc` 或 `clear`。

### 场景覆盖层：为什么它必须放在 prompt 最前面

原 skill 是**自媒体调性**：要求口语化、禁冒号、禁引号、鼓励「注入灵魂」。
直接用在学术论文或公文上会毁掉文本。所以 `overrides/<scene>.json` 提供了覆盖层。

**踩过的坑**：覆盖层最初放在 prompt 末尾，结果被前面一万多字的通用方法论淹没——
academic 场景明明写着「不要口语化」，模型照样写成「这两年」「越堆越多」。
把覆盖层**提到最前面**并声明「冲突时以场景补充为准」之后才生效。

**另一条经验：给正反例比给规则有效得多。** 光写「不要口语化」几乎没用；
补上「『深度学习这几年进展很快』✗ / 『近年来深度学习领域进展迅速』✓」这样的对照，
效果立竿见影。`overrides/academic.json` 和 `intensity=light` 的提示词都用了这个手法。

### skill 的发布与版本管理

**skill 随镜像发布**：`deai/skills/` 是仓库里的文件，改了就要重新构建镜像。
没有 DB 存储、没有热更新——这是刻意的，它换来的是「skill 变更走 Git review、
和代码同生共死」，以及零分布式一致性问题。

灰度与回滚直接用云托管的能力：发布新版本时可以分批放量，出问题切回上一个镜像版本。

**但 `version` 会说谎**——它是 `skill.json` 里手写的，改了内容忘记 bump 是常事。
所以每次编译都会自动算一个 **prompt 指纹**（编译产物的 sha256 前 12 位）：

| 位置 | 用途 |
| --- | --- |
| `GET /api/health` → `promptFingerprints` | 部署后一眼确认线上跑的是哪份 prompt |
| `GET /api/task/<id>` → `promptFingerprint` | 定位某次改写用的是哪份 prompt |
| `RewriteTask.prompt_fingerprint` | 配合 `Feedback` 表回答「改了 prompt 后好评率变了没」 |

实测各配置的指纹互不相同（general/xhs/academic/official × light/medium/heavy），
所以它足以定位到具体哪一份编译产物，也能告诉你该回滚到哪一版。

### 用户反馈：没有自动评测时的唯一信号

当前**没有**自动评测，所以「改得好不好」只能靠用户说。这也是唯一能发现
「prompt 改坏了」的线上信号。

`POST /api/feedback` 接受 `rating`（`good`/`bad`）+ 可选的 `reason` 标签 + 自由文本。
`reason` 是**枚举**而非自由文本，因为要能统计出「哪类问题最多」——那才是改进 prompt
的依据：

```
added_facts     加了原文没有的内容      lost_info       丢了原文的信息
not_natural     还是很像 AI             changed_meaning 意思被改了
too_casual      改得太随意/口语         too_formal      改得太正式
too_long        变啰嗦了                too_short       变短了
```

两个刻意的设计：

* **必须校验任务归属**。不校验的话，任何人拿到一个 `taskId` 就能刷评价，
  统计就失真了——而统计是这个接口存在的全部意义。
* **同一任务同一用户只保留一条**（`unique_together` + `update_or_create`）。
  重复提交视为「改主意」并覆盖，而不是堆出多条自相矛盾的记录。

`GET /api/task/<id>` 会回填 `feedback` 字段，前端据此显示「你已评价」。
注意它在 status 分支**之外**——失败的任务也能评价（「它根本没改对」也是有效反馈）。

`/api/feedback/summary` 只返回当前用户自己的统计，全局统计请直接查库。

### 切换模型供应商（DeepSeek / OpenRouter）

两个 key 可以同时配好，用 `LLM_PROVIDER` 一行切换：

```bash
LLM_PROVIDER=deepseek        # 或 openrouter
DEEPSEEK_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-v1-...
```

两者都兼容 OpenAI 协议，但**有两处实质差异，代码里都做了适配**：

| | DeepSeek 直连 | OpenRouter |
| --- | --- | --- |
| 缓存命中字段 | `prompt_cache_hit_tokens` | `prompt_tokens_details.cached_tokens` |
| 费用 | **不返回**，按本地价格表算 | **直接返回 `cost`（美元）** |
| 余额接口 | `/user/balance` | `/credits` + `/key` |

`estimate_cost` 会标明 `source`：`provider`（供应商上报，可信）或 `local_table`
（本地估算，仅供参考）。`/api/health` 的 `llm` 字段会回显当前 provider、模型和端点。

**实测结论：这个场景下 DeepSeek 直连明显更好**（同一段文本、同一个 skill）：

| | DeepSeek 直连 | OpenRouter |
| --- | --- | --- |
| 改写质量 | 「先把目标定下来，流程边走边调」 | 「接下来最重要的是流程要持续改进」（套话没去掉） |
| 缓存命中 | **95.8%** | **0%**（不透传 prompt 缓存） |
| 单次费用 | ¥0.0025 | **¥0.0288（贵 11.6 倍）** |

OpenRouter 的价值在于一个 key 访问多家模型、方便对比试验；但它的代理层不透传
DeepSeek 的 prompt 缓存，而 humanizer 的 prompt 有 15k token——缓存一失效，
成本和延迟都会明显上去。**如果要换，建议先跑几段文本对比再决定。**

### 保真校验（这个功能最大的信任风险）

「去 AI 味」要求**具体化、给判断**，硬约束却是**不新增事实**——两者本质冲突。实测模型确实
会补出原文没有的细节：原文只说「繁琐的重复性工作」，它写成「填表、整理、来回搬运数据」。

`warnings` 字段会给出三层提示：

| 检查 | 说明 |
| --- | --- |
| 数字守恒 | 原文的数字不能在改写后消失 |
| 长度比 | 暴涨（疑似加内容）或暴缩（疑似丢信息） |
| **新增内容** | 模型自报（`addedFacts`）+ 启发式（列举项 / 引号短语在原文找不到出处） |

`addedFacts` 是让模型在 `<REPORT>` 里自报「补充了哪些原文没有的内容」。协议里明确要求
诚实申报，且**「没写这一行」与「写了无」是两件事**——前者是未知，不会被当成没问题。

启发式一定会误报（改写本来就可能引入列举），所以文案是「确认一下」而不是断言。
它用的是**逐项前缀匹配**而不是整组字符串比对：原文写「批处理、快捷键和离线模式」、
改写换成「批处理、快捷键、离线模式」，连接词一变整组就不相等了，那样会大面积误报。

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

---

## 七、数据库表结构

四张业务表（另有 Django 自带的 `django_*` / `auth_*` 表，随 `migrate` 一起建，不用管）。

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| `deai_rewrite_task` | 改写任务：状态、结果、用量、费用 | 主键 `id`；索引 `(openid, created_at)` |
| `deai_feedback` | 用户对改写结果的评价 | 唯一 `(task_id, openid)` |
| `deai_quota_usage` | 每日免费额度计数 | 唯一 `(openid, day)` |
| `deai_user_profile` | 用户登录状态（决定走哪档额度） | 主键 `openid` |
| `Counters` | 模板原有的计数器示例 | 主键 `id` |

> **字符集必须是 `utf8mb4`**，否则 emoji 会静默变成 `?`（见第三节的踩坑记录）。
> **表名大小写**：`lower_case_table_names=1`（Linux 默认）时 `Counters` 实际存为 `counters`，
> Django 查询不受影响，但手写 SQL 时要注意。下面统一用模型里的逻辑名。

---

### 7.1 `deai_rewrite_task` —— 改写任务

一次「AI 深度改写」就是一行。任务必须落库：`callContainer` 上限 15 秒，而模型要跑
1-3 秒甚至更久，且轮询请求可能落到不同副本，状态不能只放进程内存。

| 字段 | 类型 | 取值 / 默认 | 含义 |
| --- | --- | --- | --- |
| `id` | varchar(40) | 主键，`uuid4().hex` | 任务 ID，轮询时用它 |
| `openid` | varchar(64) | 索引 | 调用者微信 openid；本地匿名调用为 `anonymous` |
| `status` | varchar(16) | `pending`(默认) / `running` / `done` / `failed` | 任务状态。**轮询接口靠它分支** |
| `mode` | varchar(16) | `general`(默认) / `xhs` / `academic` / `official` | 场景，决定 skill 的场景覆盖 |
| `intensity` | varchar(16) | `light` / `medium`(默认) / `heavy` | 改写力度 |
| `skill` | varchar(32) | `humanizer`(默认) / `legacy` | **实际生效**的 skill。编译失败回退时这里会与请求值不同——正是排查线索 |
| `skill_version` | varchar(16) | 可空 | skill 版本号（`skill.json` 里**手写**，可能忘记 bump） |
| `prompt_fingerprint` | varchar(16) | 可空 | 编译后 prompt 的 sha256 前 12 位，**不会说谎**，用于精确定位版本 |
| `source_text` | longtext | | 用户原始输入（≤ `DEAI_MAX_INPUT_CHARS`） |
| `rules_text` | longtext | 可空 | 规则层改写结果。**模型失败时这就是兜底内容** |
| `llm_text` | longtext | 可空 | 大模型改写结果（`<REWRITTEN>` 内容） |
| `llm_report` | longtext | 可空 | 模型自评报告（`<REPORT>` 内容），legacy 模式为空 |
| `protocol_ok` | bool | 默认 `true` | 模型是否守住输出协议；`false` 表示走了容错解析——变多说明 prompt 要调 |
| `llm_added_facts` | longtext | 可空 | 模型**自报**补充了哪些原文没有的内容，供用户核对 |
| `prompt_tokens` | int | 默认 0 | 输入 token |
| `completion_tokens` | int | 默认 0 | 输出 token |
| `cache_hit_tokens` | int | 默认 0 | 命中 prompt 缓存的 token。**成本关键**：命中价与未命中价差 50 倍，实测直连 DeepSeek 时命中率约 96% |
| `cost_cny` | numeric(12,6) | 默认 0 | 本次费用（元）。供应商上报的美元成本折算，或按本地价格表估算 |
| `error` | longtext | 可空 | 失败原因（人话，直接给用户看） |
| `model_name` | varchar(64) | 可空 | 模型实际返回的模型名（可能与你请求的不同，如 `deepseek-chat` → `deepseek-flash`） |
| `provider` | varchar(16) | 可空 | 实际用的供应商：`deepseek` / `openrouter` |
| `warnings` | longtext | 可空 | 保真校验提示，**JSON 数组字符串**（不是关联表） |
| `elapsed_ms` | int | 默认 0 | 模型调用耗时（毫秒），不含排队 |
| `created_at` | datetime(6) | 自动写入 | 创建时间，索引字段 |
| `updated_at` | datetime(6) | 自动更新 | 最后更新时间 |

索引 `deai_task_openid_created_idx (openid, created_at)` 是为「查我的历史任务」建的。

### 7.2 `deai_feedback` —— 用户评价

没有自动评测，这张表就是**唯一**能知道「改得好不好」的线上信号。

| 字段 | 类型 | 取值 / 默认 | 含义 |
| --- | --- | --- | --- |
| `id` | bigint | 自增主键 | |
| `task_id` | varchar(40) | 索引 | 指向 `deai_rewrite_task.id`。**刻意不用外键**——避免任务清理时级联删掉宝贵的反馈 |
| `openid` | varchar(64) | 索引 | 评价者 |
| `rating` | varchar(8) | `good`(满意) / `bad`(不满意) | 评价 |
| `reason` | varchar(32) | 可空；见下方枚举 | 仅 `bad` 时有意义，`good` 时会被清空 |
| `comment` | longtext | 可空，≤1000 字 | 自由补充 |
| `created_at` | datetime(6) | 自动写入 | |
| `updated_at` | datetime(6) | 自动更新 | |

`reason` 枚举（前端应从 `/api/feedback/summary` 的 `availableReasons` 读，别写死）：

| 值 | 含义 |
| --- | --- |
| `added_facts` | 加了原文没有的内容 |
| `lost_info` | 丢了原文的信息 |
| `not_natural` | 还是很像 AI |
| `changed_meaning` | 意思被改了 |
| `too_casual` | 改得太随意 / 口语 |
| `too_formal` | 改得太正式 |
| `too_long` | 变啰嗦了 |
| `too_short` | 变短了、信息变少 |
| `other` | 其他（**未知标签也会收敛到这里**，而不是丢弃） |

**唯一约束 `(task_id, openid)`**：同一用户对同一任务只保留一条记录，重复提交视为
「改主意」并覆盖（`update_or_create`），不会堆出多条自相矛盾的记录。

### 7.3 `deai_quota_usage` —— 每日额度

规则层体检不计次，只有「AI 深度改写」才消耗额度。

| 字段 | 类型 | 取值 / 默认 | 含义 |
| --- | --- | --- | --- |
| `id` | bigint | 自增主键 | |
| `openid` | varchar(64) | | 用户 |
| `day` | date | | 日期。容器时区为 `Asia/Shanghai`，所以是北京时间当天 |
| `used` | int | 默认 0 | 当日已用次数，上限由 `DEAI_DAILY_LIMIT` 控制（默认 20） |

**唯一约束 `(openid, day)`** + `F()` 原子自增，保证并发下不丢计数。
计数用「先读后判」有极小竞争窗口（见「已知限制」），作为免费额度够用。

> 这张表是**最不能丢**的：容器缩容重启后如果归零，配额限制就形同虚设。
> 这也是必须用 MySQL 而不是容器内 SQLite 的主要原因。

### 7.4 `deai_user_profile` —— 用户验证状态

决定该用户走未授权档（5 次）还是已授权档（10 次）。openid 直接做主键，一个用户一行。

| 字段 | 类型 | 取值 / 默认 | 含义 |
| --- | --- | --- | --- |
| `openid` | varchar(64) | **主键** | 用户标识。与 `X-WX-OPENID` 同源 |
| `session_key` | varchar(64) | 空串 | 旧的 `code2Session` 返回的会话密钥，仅兼容老数据 |
| `phone_masked` | varchar(20) | 空串 | 打码手机号，如 `138****8000`。**库里不存完整号码** |
| `phone_hash` | varchar(64) | 空串，索引 | 手机号的 HMAC 指纹（`SECRET_KEY` 加盐）。可比较、不可反查 |
| `login_method` | varchar(20) | 空串 | `phone` = 手机号授权；`code2session` = 旧登录方式（弱验证） |
| `verified_at` | datetime(6) | 可空，索引 | 最近一次成功验证的时间。**为空 = 从未授权 = 走匿名额度** |
| `created_at` | datetime(6) | 自动写入 | |
| `updated_at` | datetime(6) | 自动更新 | |

> 注意 `QuotaUsage` 里**不区分**未授权与已授权——额度计数只有一份，档位由这张表决定。
> 这样用户授权后已用的次数不会清零（否则「先匿名用完 5 次再授权拿 10 次」会变成 15 次）。

### 7.5 `deai_wx_access_token` —— 微信 access_token 缓存

**只在 `WX_OPENAPI_ENABLED=false`（自管 token 模式）时使用**；默认的云调用模式由
开放接口服务自动注入 token，这张表一直是空的。

固定只有 `id=1` 这一行。存数据库而不是进程内存，是因为微信的 access_token 全局唯一，
新发一个旧的立即作废，而云托管是多副本运行——各副本各存内存缓存会互相顶掉，
表现为随机 `40001`。

| 字段 | 类型 | 取值 / 默认 | 含义 |
| --- | --- | --- | --- |
| `id` | int | **主键**，固定 `1` | 单行表 |
| `token` | varchar(512) | | 当前的 access_token |
| `expires_at` | datetime(6) | | 过期时间（已提前 5 分钟） |
| `updated_at` | datetime(6) | 自动更新 | 最近一次刷新时间 |

### 7.6 `Counters` —— 模板原有

上游模板自带的计数器示例，保持可用、未改动行为。

| 字段 | 类型 | 取值 / 默认 | 含义 |
| --- | --- | --- | --- |
| `id` | bigint | 自增主键 | |
| `count` | int | 默认 0 | 计数值，`/api/count` 读写 |
| `createdAt` | datetime(6) | `timezone.now` | 创建时间 |
| `updatedAt` | datetime(6) | `timezone.now` | 更新时间 |

---

## 八、深度改写为什么是「混合模式」

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

## 九、已知限制

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
- **手机号快速验证有两个硬门槛**：①只对**非个人主体 + 已认证**的小程序开放
  （个人主体返回 `48001`）；②**每次成功调用 0.03 元**，每账号 1000 次体验额度
  （正式/体验/开发版共用），用完后要在「微信公众平台 → 付费管理」买资源包。
  所以「授权提额度」不要设计成诱导所有人狂点，正常放一个入口即可。
  额度不足时前端按钮回调拿到 `errno === 1400001`（不产生 code、不计费），
  服务端侧的 `48001` 已被翻译成「该小程序没有手机号快速验证的权限
  （需非个人主体且已认证）」。
- **`deai_wx_access_token` 表只在自管 token 模式下有内容**。默认云调用模式下它是空表，
  容器里也不存任何微信凭证；如果开了 `WX_OPENAPI_ENABLED=false`，请确保 MySQL 可用，
  否则每次调用都要重新换 token。
- 模型密钥若写进 `container.config.json` 会进仓库，**请只用控制台环境变量**。

---

## 十、上游模板

- 微信云托管快速开始：<https://developers.weixin.qq.com/miniprogram/dev/wxcloudrun/src/basic/guide.html>
- 本地调试指南：<https://developers.weixin.qq.com/miniprogram/dev/wxcloudrun/src/guide/debug/>
- Dockerfile 最佳实践：<https://developers.weixin.qq.com/miniprogram/dev/wxcloudrun/src/scene/build/speed.html>

## License

[MIT](./LICENSE)
