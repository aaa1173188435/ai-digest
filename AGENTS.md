# 给接手的 agent 看

这个文件是**会话之间的交接文档**。新会话请先读完它，再动手。

## 一句话

`ai-digest`：每天抓 11 个 AI 技术源 → 去重 → LLM 打分排序 → 发布静态简报，并带一套可离线重放的评测。

- 仓库：https://github.com/aaa1173188435/ai-digest
- 站点：https://aaa1173188435.github.io/ai-digest/
- 本地：`C:\Users\86157\Documents\deepseek-harness\default-workspace\ai-digest`
- 解释器：`.\.venv\Scripts\python.exe`（命令统一写成 `python -m ai_digest ...`）

## 当前状态（2026-11-18）

| 项 | 值 |
|---|---|
| 评测口径 | **rubric v2**：`只有 12 个位置，这条必须占一个位置吗` |
| 基准 case | `eval/cases/2026-11-18.json`（41 条候选，标注 12 条必须留） |
| 基线指标 | precision@12 **0.750**、nDCG@12 0.831、lift over random **2.562** |
| 排序 prompt | 已显式编码「纯技术优先」 |
| 机械规则 | 标题形如 `包名 1.2.3` 的版本发布，分数夹到 20（正则，不走模型） |
| 测试 | 86 个，`pytest -q` 全绿 |

**这些数字只来自 1 个 case，不能当结论。** 攒够 5–10 个 case 才谈得上证据。

## 用户的标注怎么来

用户每早在线上标注页标完导出，文件落在 `C:\Users\86157\Downloads\labels-*.json`。

```bash
python -m ai_digest eval import     # 自动找 Downloads 里最新的那份，校验后归档到 eval/labels/
```

`import` 会校验 case_id 和 id 完整性，对不上就报错——**不要绕过它手工拷贝**。

每天的标注页地址：

```
https://aaa1173188435.github.io/ai-digest/eval/label-<YYYY-MM-DD>.html
```

## 节奏规则（最重要，别搞错）

1. **不要在用户说「标好了」的当天就算分调参。** 1 个 case 上多捞一条就是 ±0.083，单日噪声远大于任何真实改进。
2. 用户标注、`import`、`git commit` 之后，**等积攒到 5–10 个 case 再跑一次**：

   ```bash
   python -m ai_digest eval report      # 全部 case 一张宏平均表，写入 eval/reports/summary.md
   ```

3. 需要看**单个 case 错在哪**时才用 `eval score`（它打印逐条明细）。日常判断用 `report`。

## 调整排序 agent 的正当流程

用户说「我想让它更偏好 X」时，按这个顺序，别跳步：

1. **先确认有没有足够 case。** 少于 3 个就明说结论不可靠，先攒数据。
2. **跑一次 `eval report` 记录当前基线**（`eval/reports/summary.md`）。
3. **只改一个变量**：要么 prompt，要么某条机械规则，不要同时改两处。
4. **再跑 `eval report`**，比较 `mean_lift`。**用 lift，不要用 precision**——precision 依赖相关率，相关率高的 case 会让烂系统也看起来不错。
5. 只有**多数 case 同时变好**才算改进。只在 1 个 case 上涨、其他持平或下降 → 回滚。

机械可判定的规则（版本号、招聘广告、URL 模式）用代码，不要用 prompt。模型对它做不稳的事，改十次措辞也没用。

## 不要做的事

- **不要改 `public/` 里的 HTML。** 它每次运行全量重建，改了会被覆盖；改模板（`src/ai_digest/render/templates/`）。
- **不要把模型分数放进标注页。** 会锚定标注者；`AnnotatedItem` 没有 score 字段，有测试守着。
- **不要用排序 prompt 生成标注用的中文摘要。** 会把模型的判断混进标注材料；用 `eval annotate` 自带的中立 prompt。
- **不要每轮都调 prompt。** 见上面的节奏规则。
- **不要用 `--min-precision` 做门禁**，用 `--min-lift`。

## 这台机器的坑（会反复遇到）

1. **PowerShell 的 TLS 是坏的**（`SEC_E_NO_CREDENTIALS`）。`Invoke-WebRequest` / `curl` 全部失败，但 **Python 和 Node 正常**。测网络用 Python。
2. **pip 必须用镜像**，且关掉版本检查，否则会卡十几分钟：

   ```powershell
   $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]" `
     --index-url https://mirrors.aliyun.com/pypi/simple/ --timeout 20
   ```

3. **git push 经常失败**。三个原因叠在一起：远端可能被 CI 机器人提交（需先 fetch+rebase）、GitHub 443 时通时断、凭证助手会拉起 `sh` 而它在本机崩溃。可用的组合：

   ```powershell
   $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$((gh auth token).Trim())"))
   $env:GIT_CONFIG_COUNT = "1"
   $env:GIT_CONFIG_KEY_0 = "http.extraheader"
   $env:GIT_CONFIG_VALUE_0 = "AUTHORIZATION: basic $b64"
   $env:GIT_HTTP_LOW_SPEED_LIMIT = "1000"
   $env:GIT_HTTP_LOW_SPEED_TIME = "25"
   git fetch origin main; git rebase origin/main; git push origin main
   ```

   推完用 `gh api repos/aaa1173188435/ai-digest/commits/main` 确认远端真的动了——不要相信 `$LASTEXITCODE` 在管道里的值。

4. **PowerShell 传带空格/引号的参数给原生 exe 会被拆开**（`gh api --jq '.a + "x"'` 会报参数个数错误）。让 gh 输出 JSON 再用 `ConvertFrom-Json`。

## 工作流分工

| 工作流 | 触发 | 做什么 | 成本 |
|---|---|---|---|
| `daily-digest` | 每天 06:00 / 手动 | 抓取 → 排序 → 部署；另外冻结 case、生成中文摘要、渲染标注页、把 case 提交回 main | 约 $0.013 |
| `rebuild-site` | push 到 main 的代码改动 | 从缓存数据库重建页面 + 重新生成标注页 → 部署 | 零 |

两条工作流共用 `pages` 并发组。`rebuild-site` 必须重新生成标注页，否则一次 push 会把线上标注页删掉。

## 生命周期

- Actions 缓存 7 天不访问就回收（里面是 SQLite：文章、已推送记录、ETag）。
- 公开仓库定时任务 60 天无提交会自动停用。
- **case 和 label 都提交在 `eval/`，不依赖缓存**——这是历史能留存的原因。
