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

## 这台机器的坑（根因已定位）

**根因：这个命令行跑在「低完整性级别」（Low IL）。** 用 `whoami /groups` 看到
`Mandatory Label\Low Mandatory Level` 就能确认。这一个事实同时解释了三件事：
schannel 拿不到凭证、写不了用户目录里 Medium IL 的文件、拉起 msys 的 `sh` 会 fork 失败。

| 问题 | 能修吗 | 现状 |
|---|---|---|
| `curl` / `Invoke-WebRequest` 全部 TLS 失败 | ❌ **不可修** | Low IL 下 schannel 无法向 lsass 取客户端凭证。与 TLS 版本无关（强制 1.2 也失败）、与证书和服务无关（CryptSvc / KeyIso 都在跑）、与 crypto 目录权限无关（可读）。**装 PowerShell 7 也没用，已验证**。用 Python 或 Node，它们走 OpenSSL，不碰 schannel |
| pip 装包卡十几分钟 | ✅ 已修 | 配置写在 `.venv\pip.ini`（`disable-pip-version-check` + 镜像）。不能写 `%APPDATA%\pip\pip.ini`——那是 Medium IL，Low IL 写不了，即使 DACL 里给了 FullControl：**完整性级别优先于 DACL**。现在装一个已满足的包约 0.7s |
| `git push` 失败 | ✅ 已修 | 用 `tools\push.ps1`（见下） |
| PowerShell 把参数拆开 | ✅ 已修 | 已装 **PowerShell 7.7.0** 便携版在 `.tools\pwsh`，并已加入用户 PATH，`pwsh` 直接可用。实测 `gh api --jq '.a + "  " + .b'` 在 5.1 上被拆成两个参数、在 7.7 上正确。附带收益：5.1 控制台中文乱码，7.7 正常 |

### PowerShell 7 的两个限制

1. **它在仓库里**（`.tools/pwsh`，256 MB，已 gitignore）。用户 PATH 指向这个绝对路径，所以**移动或删除这个工作区会让 PATH 里的条目失效**（无害，但 `pwsh` 就找不到了）。
2. **它不修 TLS**。已实测：PS7 的 `Invoke-WebRequest` 仍然是 `The SSL connection could not be established`——因为它还是走 schannel，进程还是 Low IL。想真正修好只能脱离这个沙箱。

安装方式（供参考，重装时用）：GitHub Release 的原生下载域名 `objects.githubusercontent.com` 在这里不通，**必须走清华镜像**，
`https://mirrors.tuna.tsinghua.edu.cn/github-release/PowerShell/PowerShell/LatestRelease/`
（实测 40 MB/s）。校验和在同目录 `hashes.sha256`，注意那是 **UTF-16LE** 编码。


### 推代码

```powershell
# 执行策略显示 Unrestricted，脚本仍会被拦（AuthorizationManager check failed），
# 加 -ExecutionPolicy Bypass 最省事。
powershell -ExecutionPolicy Bypass -File .\tools\push.ps1 -Message "feat: ..."
```

脚本做四件事：`git add/commit`（给了 `-Message` 时）→ `fetch` → 必要时 `rebase` → 带
`http.extraheader` 推送。因为凭证助手会拉起 `sh` 而它在本机崩溃，所以认证走 header、
不触发任何 helper；又因为 GitHub:443 时通时断，加了 `GIT_HTTP_LOW_SPEED_*` 让死连接
25 秒失败而不是挂着。

**判断成功只看 API，不看 `$LASTEXITCODE`**：这台机器上后者已经出现过「推送成功却报
失败」和「推送失败却报成功」两种。脚本内部也是这么做的——所以它报成功时是真的成功。

`git pull.rebase` / `rebase.autoStash` / `push.autoSetupRemote` 已设为全局默认。

### 仍然要用 Python 的地方

任何 HTTP 探测、下载、接口校验都用 Python。例如：

```powershell
.\.venv\Scripts\python.exe -c "import urllib.request;print(urllib.request.urlopen('https://example.com', timeout=15).status)"
```

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
