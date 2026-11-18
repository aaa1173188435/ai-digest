# ai-digest

每天 06:00 自动运行的 AI 技术简报：抓取多家技术博客 → 合并同一新闻的重复报道 → 用 LLM 打分排序并生成摘要 → 发布成静态站点。

不是"RSS 转发器"。真正做的事情在**去重聚类**和**可观测的成本控制**上，见下面的设计决策。

---

## 架构

```
config/sources.yaml
        │
        ▼
┌────────────────┐   条件请求(ETag/If-Modified-Since) · 指数退避 · 信号量限并发
│    ingest      │◄─────────────────────────────────────────────────
│ fetch → parse  │
└───────┬────────┘
        │ Article(canonical_url UNIQUE)
        ▼
┌────────────────┐
│     SQLite     │   articles / feeds / digests / runs
└───────┬────────┘
        │ 滚动窗口 MAX_AGE_DAYS，减去"最近 7 天已推送过的"
        ▼
┌────────────────┐   精确哈希 → 64-bit simhash 单链聚类
│    dedupe      │
└───────┬────────┘
        │ 每个簇只留一个代表（按来源权重挑）
        ▼
┌────────────────┐   8 条/请求 · JSON 模式 · pydantic 校验 · 失败降级
│  enrich (LLM)  │
└───────┬────────┘
        │ score / topic / one_liner / bullets
        ▼
┌────────────────┐
│    render      │   Markdown + 静态 HTML（index / archive / 按日期归档）
└───────┬────────┘
        ▼
     public/  ──►  GitHub Pages
```

| 层 | 职责 | 关键点 |
|---|---|---|
| `ingest/` | 抓取与解析 | 条件请求、失败重试、并发上限 |
| `process/normalize.py` | URL 与文本规范化 | 去 tracking 参数、折叠空白 |
| `process/simhash.py` | 近似重复 | blake2b 哈希保证跨进程稳定 |
| `process/dedupe.py` | 聚类 | 单链聚类，O(n²) 且显式说明边界 |
| `process/rank.py` | LLM 富化 | 批量、校验、降级 |
| `db.py` | 持久化 | 全部写入幂等 |
| `render/` | 输出 | 从 DB 全量重建静态站 |

---

## 一次真实运行

2026-11-18，DeepSeek `deepseek-chat`：

| 指标 | 数值 |
|---|---|
| 信息源 | 11 个，全部成功 |
| 抓到条目 | 110（109 条新） |
| 4 天内候选 | 41 |
| 预筛后送 LLM | 30 |
| 最终展示 | 12 |
| LLM 请求数 | 4（批大小 8） |
| tokens | 4,543 输入 / 2,944 输出 |
| **成本** | **$0.0045**（约 ¥0.03，一个月约 ¥1） |
| 耗时 | 17.2s |

两段排序在这里就回本了：如果 41 篇全送模型，tokens 会翻倍，而多出来的 11 篇没有一篇能进前 12。

---

## 四个值得讲的设计决策

### 1. 去重分两阶段，代价逐级升高

同一个新闻会被五家媒体报道，标题还各不相同。

- **第一阶段：URL 规范化 + 唯一约束。** 去掉 `utm_*` / `fbclid` / 尾斜杠 / 片段，`canonical_url` 在 SQLite 里 UNIQUE。跨天重复抓取自然幂等。
- **第二阶段：64-bit simhash 单链聚类。** 对 `标题 + 摘要` 取 3-gram shingle 后按位投票。两个文档 Hamming 距离 ≤ 3 视为同一故事。

**为什么不用 embedding 聚类？** 每篇文章要额外一次 embedding 调用，成本和延迟都翻倍，而这两种的召回差异在"同一条新闻被复述"这个场景下很小——文本重合度本来就高。真要上语义去重，得先有评测集证明它值得，见 Roadmap。

**为什么手写 simhash 而不装库？** 40 行，且能解释清楚。哈希用 `blake2b` 而不是 Python 内置 `hash()`——后者按进程加盐，跨进程结果不稳定，会让"昨天判为重复、今天判为不同"。

### 2. LLM 批处理 + 严格校验 + 降级

- **批量。** 一次请求处理 8 篇，把 ~300 token 的 system prompt 摊薄 8 倍。这是"一天一分钱"和"一天一毛钱"的区别。
- **校验。** `response_format={"type":"json_object"}` 只是建议，不是保证。返回的每一项都过 pydantic：score 夹到 0–100，topic 必须在白名单里，缺失 id 的条目按缺失处理。
- **降级。** 整批失败就退回关键词启发式（`llm/fake.py`），简报照样发布。**每天 06:00 硬失败比发布一份 80% 好的简报更糟。**

### 3. 每一步都可观测

- `runs` 表记录每次运行的开始/结束/状态/统计，`ai-digest status` 直接打印。
- token 用量与**估算成本**写进当日简报页脚——成本不可见就会悄悄上涨。
- 抓取失败、解析失败、LLM 降级都有独立计数，页脚会显示"抓取 7/8 源"。

### 4. 排序分两段，因为算力不该花在看不见的地方

第一次跑真实数据时：109 篇候选全部送进 LLM，最后只展示 12 条。**87% 的 token 花在了不会出现在页面上的文章上。**

改成两段：

```
109 篇 ──启发式打分（免费）──► 30 篇 ──LLM 打分（花钱）──► 12 条
```

启发式只做召回（关键词 + 来源权重），LLM 做精排。默认 `PREFILTER_N=30`、`TOP_N=12`、批大小 8，所以一天只发 4 次请求。

另加一条**来源多样性约束**（`MAX_PER_SOURCE=2`）：单纯按分数排序会让一个高产博客占满整页——第一次真跑的 12 条里有 6 条来自同一个人的 Datasette 版本号。先按上限选，不够再从溢出里回填。

---

## 调试记录：四个真实 bug

这四个都是真跑起来才暴露的，而且都不是"代码写错了一个字符"那种。留着比改掉更有价值——面试里能讲清楚"你怎么发现的"，比"我写了个 pipeline"有用得多。

**1. 簇代表重选时，有一篇文章凭空消失。**
`Cluster` 原本同时存 `representative` 和 `members`，聚类结束后重新挑选代表，结果旧代表被踢出 `members` 而新代表同时出现在两处。症状很隐蔽：条目总数是对的，但 `also_reported_by` 是空的，同一新闻的另一家报道再也没出现过。
**修法不是加判断，而是改类型：** 只存 `articles` 全量，`representative` 变成计算属性。让"代表和成员不一致"这种状态无法表示。

**2. 标题里的 `<br>` 直接渲染到了页面上。**
摘要做了 HTML 清洗，标题没有——因为"标题都是纯文本"这个假设在 BAIR 那种源上不成立。现在标题同样走一遍清洗。

**3. 简报里出现了一篇 2023 年的文章。**
候选窗口按 `fetched_at`（抓取时间）过滤，而旧博客的归档文章"今天被抓到"就被当成了"今天的新闻"。改成优先用 `published_at`，缺失时才回退到抓取时间。
顺带发现第二个坑：不同源的时区偏移不同，`2026-11-18T06:00+02:00` 和 `2026-11-18T04:30+00:00` 按字符串比较会比出错误顺序——所以入库前统一转 UTC。

**4. 最重要的那条新闻，摘要变成了 HN 的记账信息。**
Hacker News 的 RSS 把 `Article URL: … Points: 1138 # Comments: 720` 塞进 `<description>`，而不是正文摘要。而 hnrss 的 link 正指向原文，和 OpenAI 官方 feed 规范化后是同一个 URL——于是两行合并成一条，`ON CONFLICT` 让**权重 0.8 的聚合站覆盖了权重 1.3 的官方源**。
模型拿到的"正文"因此是一串 HN 元数据，写出了一句"HN 讨论热度极高"。

两处都得改，缺一不可：入库时按 `source_weight` 决定谁覆盖谁（`CASE WHEN`），以及清洗时剥掉聚合站的记账字段。
**这个 bug 的教训是**：幂等不等于正确。`ON CONFLICT` 保证了"不重复"，但没保证"留下的是对的那一份"。

---

## 评测

没有评测集的调参是猜的。这一层解决的问题是：**改了 prompt 之后，是变好了，还是只是换了个错法？**

### 五个命令

```bash
ai-digest eval snapshot                                  # 冻结候选池 → eval/cases/<date>.json
ai-digest eval annotate eval/cases/<date>.json           # 生成中文摘要（标注用）
ai-digest eval label    eval/cases/<date>.json           # 生成标注页
ai-digest eval import                                    # 收标注：默认找 Downloads 里最新的
ai-digest eval score    eval/cases/<date>.json eval/labels/<file>.json
```

`import` 存在的原因很实际：**反馈回路最容易断在"把文件挪到正确位置"这一步**。它自动去 `~/Downloads` 找最新的 `labels-*.json`，校验 case_id 和数据完整性，再归档到 `eval/labels/`。找不到就明确报错，不会静默用错文件。

`snapshot` 冻结的是**文本本身**，不是分数。之后每次 `score` 都用当前代码和 prompt 重新打分——所以它测的是"现在的系统"，不是"那天的历史输出"。这是能做 A/B 的前提：明天的 feed 是另一批文章，不冻结就没法对比。

`score` 还会打印逐条明细（上了榜的、漏掉的、不该上的），因为**数字只告诉你「好不好」，明细才告诉你「错在哪」**。

### 每天的闭环

评测要变成证据，必须每天有数据，而"每天手动跑一遍五个命令"是活不下去的。所以 `daily-digest` 工作流在生成简报之后，自动做完这些：

1. `eval snapshot` —— 冻结当天的候选池
2. `eval annotate` —— 给全部候选写中立中文摘要（额外约 $0.008/天）
3. 把标注页渲染进站点：`…/ai-digest/eval/label-<date>.html`
4. **把 case 提交回 main**（机器人 commit），这样标注回来时才有东西可挂

于是人这一侧只剩两步：

```
打开 …/eval/label-<date>.html  →  标 41 条（约 3 分钟）→  导出 JSON
```

导出后**不需要手动挪文件**：`ai-digest eval import` 会去 Downloads 里找最新的那份，校验后归档到 `eval/labels/`，然后 `eval score` 出分。

设计上有两处必须同时存在，少一个就断：

- **case 必须进仓库**，不能只留在 Actions 缓存里——缓存 7 天不访问就没了，历史 case 也就没了。
- **标注页必须由两条工作流都生成**。`rebuild-site` 从干净 checkout 重建 `public/`，如果它不生成标注页，一次 push 就会把线上标注页删掉。



### 标注页的两个反锚定设计

用英文标题标注，实际是在考标注者的阅读速度和背景知识，测的就不是内容质量了。所以 `annotate` 会给每条生成中文摘要。但这里有两个容易做错的地方：

**1. 摘要必须是中立的，不能用排序 prompt 生成。**
第一版直接复用了排序 prompt，生成的摘要是「Quad9 退出法国……**与 AI 技术无关**」「jQuery 4.1.0……**属前端库更新日志**」——把模型自己的相关性判断写进了材料里。标注者看到这种句子，就不再是独立判断，而是在确认模型的判断，指标会被系统性抬高。
所以 `annotate` 用一个单独的 prompt，明确禁止「重要」「无关」「值得」这类措辞，只允许陈述内容。

**2. 页面不显示模型给的分。**
同理，看到「95 分」的标注者已经不是独立标注了。`AnnotatedItem` 数据结构里根本没有 score 字段，并且有一条测试守着这一点——防止以后有人"顺手"把它加回来。

### 指标

| 指标 | 含义 |
|---|---|
| `precision@12` | 发出去的 12 条里，有几条真的值得读 |
| `recall@12` | 池子里所有值得读的，捞回来了几条 |
| `ndcg@12` | 给"排在前面"加权——第 1 名和第 12 名对调，分数要变 |
| `random_precision@k` | 随机挑 k 条能拿到的分数（= 池子里的相关率） |
| `lift_over_random` | precision 除以随机基线。**这个才是不受标注松紧影响的指标** |
| `mean_score_relevant / irrelevant` | 两类内容的平均分差。差距小说明排序靠运气而非信号 |

`ndcg@12` 和 `precision@12` 的区别值得说：两者都只看前 12，但 nDCG 在意顺序。一条好新闻埋在第 11 位，precision 不受影响，nDCG 会扣分——而那恰好是要防的失败模式。

### 标注 rubric v2：把"值得读"换成强制取舍

v1 的问题不是标注者不认真，而是**问题问得太松**：「这条值得读吗」→ 41 条里 26 条都说值得，随机基线 0.63，指标失去分辨率。

v2 改成强制取舍：

> 假设今天的简报**只有 12 个位置**，候选 41 条。**这条必须占一个位置吗？**
>
> 必须留 = 少了它今天的简报明显变差；可以不看 = 晚几天看也不亏。

四个配套改动，每一个都是为了防污染：

| 改动 | 防的是什么 |
|---|---|
| 按钮改为「必须留 / 可以不看」 | "值得读"这种宽松措辞必然带出宽松标签 |
| 顶部实时显示「必须留 X / 12」，超了变红 | 让你看见自己的标准是不是在松动 |
| localStorage key 带 rubric 版本号 | v1 的标注不会在 v2 页面里静默复活 |
| 导出文件名带 `-rubric2` | 不覆盖旧标签，两轮数据可并存对比 |

后两条容易被忽略但最要命：**两轮标注回答的是不同问题**。v1 的"值得读"和 v2 的"今天必须发"混在一起，是那种不报错、只让所有指标悄悄失真的数据污染。

### 真实基线

2026-11-18，41 篇候选，rubric v2，标注者判 12 篇"必须留"（正好占满 12 个位置）：

| 指标 | 数值 | 怎么读 |
|---|---|---|
| precision@12 | 0.750 | 发出去的 12 条里 9 条确实该留 |
| recall@12 | 0.750 | 12 条该留的捞回 9 条 |
| ndcg@12 | 0.831 | 排序位置也基本合理 |
| random_precision@12 | 0.293 | 池子里 29% 相关，瞎挑 12 条能到 0.29 |
| **lift_over_random** | **2.562** | **比随机好 1.5 倍以上** |
| 相关项 / 无关项平均分 | 54.5 / 25.0 | 差 29.5 分，区分度是真实的 |

### 三轮迭代的教训

**1. 自评标注会把 lift 吹大三分之一。**

| 标注来源 | 相关率 | 随机基线 | precision@12 | lift |
|---|---|---|---|---|
| 助手自评 | 22% | 0.22 | 0.667 | 约 3.0 |
| 本人标注（同 rubric 下） | 29% | 0.29 | 0.667 | 2.28 |

precision 一模一样，lift 差 30%。同一份代码，换个标注来源就从"很强"变成"还行"。**指标必须配对随机基线读，且标注必须来自真实使用者。**

**2. 标注口径是第一位的工程决策，比模型选择更重要。**

同一个系统，同一批文章：

| rubric | 相关率 | 随机基线 | lift |
|---|---|---|---|
| v1「值得读吗」 | 63% | 0.63 | 1.18（几乎没用） |
| v2「只有 12 个位置，必须占位吗」 | 29% | 0.29 | 2.56（有明确增益） |

代码一行没改，结论从"这套排序不如瞎猜"变成"有 2.5 倍增益"。**先修 rubric，再谈模型。**

**3. 有些规则不该交给模型。**

prompt 里明确写了"纯版本号发布说明给 30 分以下"，模型仍然给 `llm 0.37.3` 打 58 分并让它占了一个发布位。换成一条正则——标题形如 `包名 1.2.3` 就夹到 20 分——precision 0.667 → 0.75，lift 2.28 → 2.56，零模型成本、零延迟、可解释。

机械规则写代码，语义判断给模型。让模型做它做不稳的事是设计失误，不是模型能力问题。

### 一个还没解决的问题：rubric 里「技术」和「AI 技术」有歧义

12 条"必须留"里有 3 条与 AI 无关（Java4Shell 漏洞、Solid 2.0 前端框架、Quoting Go Proverbs），而一条纯技术的 AI 长文（Pre-Training Without Learning Rate Decay）被判"可以不看"。

这说明问题仍可能被读成"这个技术内容值得看吗"，而不是"这个 AI 技术内容必须占位吗"。下一轮要把范围写死在提问里。

### 仍然待办

1. **一致性检查**：隔一天重标 10 条，不一致率超过 20% 说明 rubric 本身有歧义，而不是标注者不认真。
2. **扩样本**：5–10 天的 case。现在所有结论都来自 41 条、1 天。
3. **CI 门禁**：用 `--min-lift` 而不是 `--min-precision`，因为 precision 依赖相关率，会骗人。

---

## 工程实践

| 项 | 做法 |
|---|---|
| 幂等 | `canonical_url` UNIQUE + `ON CONFLICT`，重跑不产生重复行 |
| 可测试性 | `LLMClient` 与 `Fetcher` 都是注入的；测试用 `httpx.MockTransport` + `FakeLLM`，**零网络** |
| 容错 | 单个源失败不影响整轮；LLM 失败降级；损坏的 digest 行跳过而不是让整站构建崩溃 |
| CI | GitHub Actions 先跑 `pytest`，再抓取、再部署；测试挂了就不会发布坏页面 |
| 状态 | SQLite 是唯一跨运行状态；站点每次从 DB 全量重建 |

```bash
pytest -q          # 无需 API key，无需联网
```

---

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev]"

cp .env.example .env              # 填入 DEEPSEEK_API_KEY

ai-digest run                     # 抓取 → 去重 → 排序 → 输出到 public/
ai-digest serve                   # 本地预览 http://127.0.0.1:8000
ai-digest status                  # 数据库与运行历史
```

不想花 API 额度时，用假模型跑通整条链路：

```bash
ai-digest run --provider fake
```

---

## 改前端

页面由 Jinja2 模板生成，改这三个文件：

```
src/ai_digest/render/templates/digest.html.j2    日报页
src/ai_digest/render/templates/archive.html.j2   归档页
src/ai_digest/render/templates/style.css         样式（内联进 <style>）
```

**不要改 `public/` 里的 HTML。** 那个目录是产物、被 gitignore，而且每次运行都从数据库全量重建——手改的内容下一次运行就没了。

改样式时的循环是这样，**不联网、不调模型、0.3 秒**：

```bash
ai-digest render      # 从数据库重建整个站点
ai-digest serve       # 浏览器看效果
```

推送到 GitHub 后，`rebuild-site` 工作流会自动从缓存的数据库重新生成并部署——不抓取、不调模型、零成本，约两分钟上线。想手动触发就 `gh workflow run rebuild-site.yml`。

**两条工作流的分工：**

| 工作流 | 触发 | 做什么 | 成本 |
|---|---|---|---|
| `daily-digest` | 每天 06:00 / 手动 | 抓取 → 去重 → LLM 排序 → 部署 | ~$0.0045 |
| `rebuild-site` | push 到 main / 手动 | 从缓存数据库重建页面 → 部署 | 零 |

两者共用同一个 Pages 并发组，不会互相覆盖。

---

## 配置

所有 `settings.py` 里的字段都能用同名环境变量覆盖。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `deepseek` | `deepseek` / `fake` |
| `DEEPSEEK_API_KEY` | — | 必填（`fake` 除外） |
| `TOP_N` | `12` | 每天精选条数 |
| `MAX_AGE_DAYS` | `4` | 候选文章的时间窗口 |
| `NEAR_DUP_THRESHOLD` | `3` | simhash Hamming 阈值，越大合并越激进 |
| `LLM_BATCH_SIZE` | `8` | 每次请求处理的文章数 |

信息源在 `config/sources.yaml`，加一个源不需要改代码。

---

## 部署

GitHub Actions 每天 22:00 UTC（北京时间 06:00）运行，产物发布到 GitHub Pages。

1. 仓库 Settings → Pages → Source 选 **GitHub Actions**
2. Secrets 里添加 `DEEPSEEK_API_KEY`
3. 手动跑一次 `workflow_dispatch` 验证

**已知限制：** SQLite 通过 `actions/cache` 在运行之间传递，而 GitHub 的缓存 7 天不活跃就会被回收——那之后历史归档会丢失（文章和已推送记录一起）。真正的修法是换成外部数据库（Supabase / 一台常开机器上的 Postgres）。当前选择是刻意用简单换成本，代价写在这里而不是藏起来。

---

## Roadmap

- [x] **评测集。** 冻结 case + 真人标注 + precision@k / recall@k / nDCG@k，带逐条归因明细。见上一节。
- [ ] **扩充标注。** 目前只有 1 天 41 篇、且标注来自助手。需要 5–10 天、每天由本人标注，才能把指标当证据用。
- [ ] **评测接入 CI。** `ai-digest eval score --min-precision 0.6` 已经支持以非零退出码失败，还差一个带 Secret 的评测工作流。
- [ ] **语义去重。** 当前实测的一条漏网：OpenAI 官方发布 GPT-6.2 和 Simon Willison 的解读是两个不同 URL、内容也不同，simhash 距离 25 bit，永远合不到一起——但读者看到的是同一条新闻占了两个席位。这需要按实体/主题聚类，而不是文本相似度，也应该先有评测集再动手。
- [ ] **正文抽取。** 目前只用 feed 摘要，短且偶尔是残缺的。抓正文要处理付费墙和反爬，所以先不做。
- [ ] **Agent 层。** 一个手写的 tool-calling loop：`search_articles` / `get_digest` / `add_source`，支持"这周 agent 领域有什么进展"这类追问。不用框架，因为面试要问的是失败重试和循环终止。
- [ ] **推送。** Telegram / 邮件，把"打开网页"变成"主动送达"。

---

## 目录结构

```
ai-digest/
├─ config/sources.yaml          信息源配置
├─ src/ai_digest/
│  ├─ ingest/                   抓取、RSS 解析、源配置
│  ├─ process/                  规范化、simhash、聚类、LLM 富化
│  ├─ llm/                      provider 抽象、DeepSeek、启发式兜底
│  ├─ render/                   Markdown、静态 HTML、模板
│  ├─ pipeline.py               端到端编排
│  ├─ db.py                     SQLite（articles/feeds/digests/runs）
│  └─ cli.py                    run / status / serve
├─ tests/                       零网络测试
└─ .github/workflows/digest.yml 定时 + 测试 + 部署
```
