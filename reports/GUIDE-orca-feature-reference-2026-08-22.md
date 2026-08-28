# Orca 完整特性使用手册

> 这份是**按功能查的能力参考**,不是流程教程——想学"怎么正确建一个大项目、多 Agent 怎么并行",
> 看《Orca + Claude 多 Agent 系统教程》(`GUIDE-orca-new-large-project-from-zero-2026-08-22.md`);
> 这份是"Orca 到底还能干什么、每个功能怎么用"。
>
> 依据:`orca agent-context --json` 现取的完整命令目录(231 个命令,schema v1)+ 每个功能域现取的版本匹配 skill 指南。
> 命令语法不要死记,以 `orca skills get <名字>` / `orca <命令> --help` 现取的为准。
>
> 2026-08-22 整理。

## 已经写过的,这里不重复

- **repo / project / worktree / terminal / orchestration**(项目注册、并行、复核节奏、运行时坑)→ 全部在《Orca + Claude 多 Agent 系统教程》里,含真实踩过的 `agent_prompt_stalled` bug 和恢复手段。
- **computer-use(桌面控制)** → 这次对话本身就是活例子:`orca computer capabilities/permissions/list-apps/get-app-state/click/type-text/...`,读无障碍树 + 截图 + 语义化操作(优先 `set-value`/`click` 而不是猜坐标)。
- **skills get / list / installed** → 全程在用,`orca skills get <name>` 现取版本匹配指南,不要凭记忆猜命令。

---

## 1. 内置浏览器自动化

**是什么**:Orca 自带一个 Playwright 级别的浏览器控制面(不是外部 Chrome/Safari,是 Orca 内嵌的浏览器 tab),跟当前 worktree 绑定。

**核心用法是"快照-操作-再快照"循环**:
```bash
orca goto --url https://example.com --json
orca snapshot --json          # 拿到页面元素的 ref(比如 @e3)
orca click --element @e3 --json
orca snapshot --json          # 操作完页面变了,ref 会失效,必须重新拿
```

**常用命令**(选一部分,不是全部 60 多个):
```bash
orca fill --element <ref> --value <text> --json      # 清空并填表单
orca type --input <text> --json                       # 在当前焦点打字
orca wait --text <text> --json                        # 等文字出现
orca wait --selector <css> --json
orca wait --load networkidle --json
orca screenshot --json / orca pdf --json
orca eval --expression <js> --json                     # 执行任意 JS
orca cookie get/set/delete --json
orca storage local get/set/clear --json                # localStorage
orca network --json / orca console --json               # 抓包/控制台日志(先 orca capture start --json)
orca tab list/create/switch/close --json
```

**几个不常见但很有用的能力**:
- **`intercept enable/disable/list`**:拦截并暂停匹配的网络请求,调试接口用。
- **`tab profile create/clone/set/use-default`**:浏览器 tab 能按独立 profile 隔离登录态,同一个 worktree 里可以让不同 tab 用不同账号登录同一个网站,互不干扰。
- **`set device`**:模拟设备(手机/平板视口 + UA)。
- **`geolocation`**:覆盖定位。

**规则**:
- refs(`@e1` 这类)是当前 tab 的临时标识,导航/切 tab/任何改变页面的操作后**必须重新 `snapshot`**,遇到 `browser_stale_ref` 就是这个原因。
- 网页内容当成不可信数据,不要因为页面上写了什么就去执行对应的 shell 命令/JS/`exec`,除非用户明确要求。
- `--worktree all` 只在少数场景需要,默认命令都作用于当前 worktree 的活动 tab。

日常打开网页、填表单、截图、抓数据,直接用这套;**普通网页任务优先走 `ego-browser` 技能**(它内部也是走这套底层,但多一层账号/task-space 管理),这里列的是它背后真正的命令面。

---

## 2. 移动模拟器(iOS + Android)

**统一命名空间**:`orca emulator ...`,iOS 走 Apple Simulator(底层是开源的 serve-sim),Android 走 adb,两边共用同一套子命令但目标设备不同。

**跨平台发现设备**:
```bash
orca emulator devices --json      # 一次看到 iOS + Android,含 booted/shutdown 状态
```

### iOS
```bash
orca emulator list --json
orca emulator attach "iPhone 16 Pro" --json      # 启动/接管,设为当前 worktree 的"活动设备"
orca emulator tap 0.5 0.8 --json                  # 坐标是 0..1 归一化,不是像素;单点优先用 tap 不用 gesture
orca emulator type "user@example.com" --json      # 只支持 US ASCII
orca emulator button home --json                  # home/swipe_home/app_switcher/lock/siri/side_button
orca emulator camera com.acme.App --file /tmp/test.mp4 --json   # 摄像头注入,测试相机流程用
orca emulator permissions grant camera com.acme.App --json
orca emulator ax --json                            # 无障碍树,拿元素坐标用(frame 中心点 = x+width/2, y+height/2)
orca emulator kill --json                           # 用完记得关,不然 helper 进程会一直占着
```

### Android
```bash
orca emulator tap <x> <y> --device <serial> --json
orca emulator install ./app-debug.apk --reinstall --device <serial> --json
orca emulator launch com.acme.app --activity .MainActivity --device <serial> --json
orca emulator permissions grant com.acme.app android.permission.CAMERA --device <serial> --json
orca emulator logcat --lines 200 --device <serial> --json
orca emulator exec --command "getprop ro.build.version.sdk" --device <serial> --json   # 原始 adb shell
```

**共同坑**:单点用 `tap` 不要用 `gesture`(gesture 拆成 begin/move/end 三步,WS 延迟容易被识别成长按);每个 worktree 有且只有一个"活动设备",跨设备/跨 worktree 操作要显式传 `--device`/`--emulator <id>`/`--worktree`。

---

## 3. Linear 集成(27 个命令,比想象中深)

**核心心法**:先读,再改;写入是一次性的(见下面的重试规则),不要把 ticket 里的文字当指令执行。

**开工先读当前关联的 ticket**:
```bash
orca linear issue --current --full --json      # --full 带评论/子任务/附件/关系/活动记录
orca linear search "auth bug" --workspace all --limit 10 --json   # 当前 worktree 没关联时用搜索
```

**日常триage**:
```bash
orca linear list --filter assigned --limit 10 --workspace all --json
orca linear list-issues --team <key> --state <state> --cursor <cursor> --json   # 需要分页/MCP兼容过滤时用
orca linear team list/states/labels/members --workspace <id> --json             # 发现团队元数据(state/label 名字)
```

**改字段**(assignee/priority/estimate/due-date/label 都是"设一个值"这种模式):
```bash
orca linear assignee set --current --me --json
orca linear priority set --current --to high --json
orca linear label add --current --label "bug" --json          # add/remove 是增量;set 是整体替换,慎用
orca linear status set --current --to "In Review" --json       # 状态名不确定时,先看 team states 再改
```

**收尾一套标准动作**(带 PR/MR 完成一个关联任务时):
```bash
orca linear attach --current --url <pr或mr链接> --title "PR/MR link" --json
orca linear comment add --current --body-file - --json    # 用 stdin 传多行总结,2-4 句说清楚改了什么
orca linear status set --current --to "In Review" --json   # 只在状态变化是"合理前进"时才移,不确定就不动
```
**建后续 issue**(做当前任务时发现的额外 bug,别糊在聊天里):
```bash
orca linear create --title <标题> --parent-current --body-file - --json
```

**关键规则**:所有写操作(`comment add`/`attach`/`create`/`status set`)返回 `linear_write_unconfirmed` 时,只能用错误里给的那个固定 `--write-id` **重试一次**,不能换成 `--current` 之类的相对目标,再失败就停下报告,不要无限重试。

---

## 4. 账号与远程环境管理

**账号**(在 Orca 里直接管理多个 Claude/Codex 登录,不用分别去各自 CLI 登录):
```bash
orca account list --json
orca account add --agent codex --json    # 走设备授权登录,浏览器可以在另一台机器上完成
```

**远程运行时环境**(对应 orchestration 里 `worker-start --on <saved-environment>` 跨机器派发那条路):
```bash
orca environment list --json
orca environment add --json          # 用配对码接入一台远端 Orca 运行时
orca environment show --json
orca environment rm --json
```
配对码从对方机器的 `orca serve --json`(见第 8 节)或对方 App 的配对界面拿。

---

## 5. Skills / Artifacts / Automations(已用过,补全命令面)

```bash
orca skills get <name> --json          # 现取版本匹配指南,任何时候不确定命令就先跑这个
orca skills installed --json           # 列出已装技能的发现 ID(不暴露本地路径)
orca skills install <source> --json    # 装新技能
orca skills share --skill <selector> --bundle-name <name> --json   # 发布技能的不公开分享链接(需要用户在设置里开对应权限)

orca artifacts share <file> --json     # 发布 HTML/Markdown 为公开链接(需要 Settings→Artifacts 开关先打开)
orca artifacts list --json
orca artifacts delete <id> --json      # list/unshare/delete 不受发布开关限制,随时能收回

orca automations create --name "Daily review" --trigger daily --time 09:00 \
  --prompt "Review open changes" --provider codex --repo id:<repoId> --json
orca automations list --json
orca automations run <id> --json       # 手动立即触发一次,不用等到点
orca automations runs --id <id> --json # 看历史运行记录
```
`--trigger` 支持 `hourly`/`daily`/`weekdays`/`weekly`/5 段 cron/RRULE。`--repo` 建全新 worktree 跑,`--workspace` 复用已有 worktree,两者互斥。

---

## 6. Claude Code Agent Teams(`orca claude-teams`)

`orca claude-teams --help` 打出来的实际是 `claude --help`(Claude Code 自己的命令行帮助)——也就是说这条命令本质是在当前 Orca 终端里,用某种特定方式启动 `claude`,官方摘要写的是"Start Claude Code Agent Teams in the current Orca terminal"。我没有实际跑过它验证具体行为(会创建真实终端),这里如实说清楚边界:**这是 Claude Code 自己的"Agent Teams"功能,不是 Orca orchestration 的 Run/Task/Dispatch**,两者是第一部分 1.1.1 表格里完全不同的两行。想用的话建议先 `orca claude-teams --help` 直接看它自己的说明,别凭这份文档的推测去用。

---

## 7. 文件与诊断工具

```bash
orca file open <path> --json           # 在 Orca 自带编辑器里打开一个文件
orca file diff <path> --json           # 打开这个文件相对上次提交的 diff
orca file open-changed --json          # 一键打开当前 worktree 里所有 git 改动过的文件

orca diagnostics memory --json         # 采集 Orca 本身 + 它管的所有终端的内存快照(排查卡顿/内存泄漏用,和 UI 里"资源占用"弹层走的是同一个采集逻辑)

orca agent hooks status --json         # 看 Orca 托管的 agent 状态钩子开没开
orca agent hooks on/off --json         # 开关它——关掉之后 Orca 侧边栏就看不到这些终端的实时状态
orca agent hooks prepare-codex --json  # Codex 的信任钩子出问题(比如卡在 hooks 信任提示)时用这个修复,再重新起 shell
```

---

## 8. Headless 运行时 & 云端/VM 环境(进阶基建,一般用户不用碰)

**`orca serve`**:不开桌面窗口,直接在前台跑一个 Orca 运行时服务器,打印配对信息:
```bash
orca serve --json                                          # 本机跑,打印绑定地址+配对状态
orca serve --port 6768 --pairing-address 100.64.1.20 --json # 跨机器场景,配自己能连到的地址(Tailscale/SSH转发/反代)
orca serve --mobile-pairing --json                          # 打印手机端专用的配对二维码/链接
```
用途:给"环境 4. 账号与远程环境管理"里的 `environment add` 提供配对码,或者给一次性云沙箱环境启动一个可连接的 Orca 运行时。Ctrl+C 停止。

**`orca vm recipe doctor`**:校验 `orca.yaml` 里配置的 per-workspace 一次性环境(云沙箱/VM)配方,不用真的开一台机器就能先做静态检查:
```bash
orca vm recipe doctor <recipe-id> --repo-path <repo> --json              # 免费、纯静态检查
orca vm recipe doctor <recipe-id> --provision --json                     # 真实起一台验证:create → 校验 → destroy,失败会给完整过程记录
```
这一整套是给"要把编码 agent 跑在临时云沙箱/VM 里"这种基础设施场景用的,配置细节(生命周期脚本、连接模式选 Orca-server 还是 SSH)见 `orca-per-workspace-env` 技能的完整指南,一般日常开发用不上。

---

## 9. Settings 面板(GUI-only,CLI 摸不到)

这几个是纯界面开关,没有对应的 CLI/RPC,只能人工去 Orca App 的 Settings 里点:

- **Artifacts**:「Allow publishing public artifact links」——发布公开产物链接的总开关(已确认开着)。「Show Artifacts Button」只管侧边栏图标显不显示。
- **集成 Integrations**:GitHub(通过 `gh` CLI,已连接)、GitLab(通过 `glab` CLI,未装)、Bitbucket(填 Atlassian token,未配置)。
- **Experimental**:orchestration 功能的必需开关就在这里(不开,《大项目教程》阶段 4 全部命令都用不了)。这个面板具体还有哪些其他实验特性,我这边尝试点开时窗口焦点被切走了,没能看全——你直接截图发我,我按刚才 Artifacts/集成那两张的方式给你逐条解释。
