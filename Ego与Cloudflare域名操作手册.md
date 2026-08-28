# 本机 Ego 与 Cloudflare 域名操作手册

更新：2026-08-08。本文把本机的 Ego 使用、资料（browser profile）切换，以及 Cloudflare 域名管理放在同一套流程中。资料名称、默认项、登入状态与 Cloudflare 菜单会变化；执行时以实时页面和 `profiles` 的结果为准，不把任何账号、邮箱、Cookie 或凭据写进文档或仓库。

## 本机默认覆盖范围

- 本机 Codex 的 `~/.codex/AGENTS.md`、当前 Orca 账户（它链接到该全局规则）和 Orca runtime template 都已规定：需要真实网页交互时优先使用 `ego-browser`。
- Hermes 的全局 `SOUL.md` 同样规定 Ego 为浏览器默认，并且当前 `ego-browser` skill 显示为 enabled。
- `ego-orca-profile` 只负责 **Orca 内** 的确定性资料选择与 task-space 回收；普通本机 Codex/Hermes 网页任务仍须遵守 Ego 的同一 task-space、串行调用和完成收尾规则。

## 先判断要进哪一个 Cloudflare 区域

| 目标 | Cloudflare 入口 | 不要误做成 |
| --- | --- | --- |
| 购买新域名 | `域名` → `注册` | 不要只在 DNS 添加一条记录；这不会购买域名。 |
| 将注册商迁到 Cloudflare | `域名` → `转移` | 不要以为转移是启用 CDN/DNS 的前置条件。 |
| 让现有域名使用 Cloudflare DNS、CDN 或 WAF | `网站` → `添加站点`，选择现有 zone → `DNS` → `记录` | 不必先把注册商转到 Cloudflare。 |
| 改 A、AAAA、CNAME、MX、TXT 等解析 | 对应站点（zone）→ `DNS` → `记录` | 不要从 `域名` 的注册页寻找记录编辑器。 |

2026-08-08 的已登录中文界面实测可见 `域名`、`注册`、`转移`、`添加域名`；官方文档也明确把“先添加域名到 Cloudflare”列为转入注册的第一步。菜单文字随版本调整时，按上述目标定位，而不是死记链接。

## Ego：创建、复用、结束一个安全的网页任务

在 Orca 里，先以动态资料创建任务空间。`default` 每次都会按当前 `isDefault` 解析，不等于某个固定 Profile 编号。

```bash
python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py profiles

python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py start \
  --task "cloudflare dns read-only review" --alias default
```

记录输出中的 `task_space_id`。同一项工作每一轮都复用这个数字 ID，不能只按相似名称再开一个空间。多 agent 时，所有网页轮次必须走 `run`；它会自动选回该 agent 自己登记的空间、持有全局互斥锁，并在成功后 `touch`：

```bash
python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py run \
  --task-space 123 <<'EOF'
await openOrReuseTab('https://dash.cloudflare.com/', { wait: true, timeout: 30 })
// 读取、操作并在关键点击后重新确认页面状态。
EOF
```

不要在 Orca 多 agent 任务中直接执行 `ego-browser nodejs`。每个 agent 必须有不同的 task 名称和 task-space ID，且不得在 heredoc 中自己创建、选择、接管或关闭 task space；这些动作仅能通过 `start`、`adopt`、`finish` 完成。

`run` 会在成功后为该任务登记**外部网站**标签，且只保留 hostname，不保留 URL 路径、参数、片段或页面内容。`localhost`、私有 / 回环 / 链路本地 IP、单标签主机名、`.local`、`.lan`、`.home.arpa`、`.internal` 等本地内网目标都不会被标记。需要查看时运行：

```bash
python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py sites \
  --task-space 123
```

任务关闭或被安全回收后，这些 hostname 标签仍会写入私有、最多 500 个站点的历史记录；查看最近记录：

```bash
python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py site-history \
  --limit 50
```

### 多 agent 不抢窗口、不互相覆盖的规则

1. **一 agent 一空间**：每个 agent 的 `--task` 使用唯一、可读的名称（例如 `dns-audit`、`tunnel-route-review`），只保存自己 `start` 返回的 ID；绝不复用、猜测或转交另一个 agent 的 ID。
2. **网页操作串行，分析可以并行**：所有 agent 的浏览器 heredoc 一律走 `profile-router run`。它在整个 CLI 轮次持有同一把私有锁，自动恢复该 agent 自己登记的空间，成功后刷新活跃时间。代码审阅、方案分析和测试准备不占这把锁，可并行执行。
3. **同一业务资源只有一个写入者**：task-space 隔离只解决窗口控制，不会消除两个人先后修改同一 DNS record、Tunnel route 或 Cloudflare rule 的业务冲突。每个 zone / hostname 指定一个写入 agent；其他 agent 只读审阅并把建议交给写入者。
4. **用户接管即停止**：登录、验证码、付款或用户手动接管时，agent handoff 后不得自动拿回窗口。任务结束使用 `finish`；它会复核精确名称和 agent-owned 所有权，不能关闭别的 agent 或用户窗口。
5. **禁止旁路**：手工运行原始 `ego-browser nodejs` 不会加入 router 锁，只会被 router 的忙碌检测阻止或报错；在多 agent Orca 任务中一律不用。

所有浏览器轮次结束且结果已验证后，必须关闭自己创建的空间：

```bash
python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py finish \
  --task-space 123
```

不确定时可只读检查：

```bash
python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py status
python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py reap --dry-run
```

注册表与别名文件在本机私有目录，自动回收器只会在 600 秒未 `touch` 后处理“已登记、名称精确相符、agent-owned”的任务空间；用户窗口、委派窗口和未知窗口不会被猜测性关闭。

## 如何换资料，而不是误用当前登入态

1. 先运行 `profiles`，根据实时显示名和 `isDefault` 确认所需资料。Profile ID、`Profile 1` 之类的编号不能代表账号。
2. 常用资料可建立无凭据别名；别名只保存显示名或“使用默认资料”的选择：

   ```bash
   python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py set-alias \
     --alias cloudflare-work --profile-name "实时显示的资料名称"
   python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py aliases
   ```

3. 为这个资料创建新空间：

   ```bash
   python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py start \
     --task "cloudflare zone change" --alias cloudflare-work
   ```

4. 已创建的 task space 不在中途切资料。先 `finish` 旧的 agent-owned 空间，再用另一资料 `start` 新空间。若 alias 无法解析，重新执行 `profiles`；绝不静默改用默认资料。

同一时间只串行运行一个 `ego-browser` CLI。遇到登入、验证码、付款、删除或最终确认时，将空间交给用户；在用户明确表示继续前，代理不得夺回控制权。

### 用户要求“Ego 展示”时：每次必须使用新空白资料

这是展示专用的更严格规则，不等同于一般网页任务的 task-space 隔离：每一次用户明确要求 Ego **展示 / show** 网站，都必须用一份新建、空白、没有个人登录态的 browser profile。不得复用 `default`、任何认证资料或之前的展示资料。

资料与 task-space 的生命周期必须只用 Ego 的底层 CLI/Node API，不得使用鼠标、Computer Use、Accessibility、AppleScript、Chrome、`ego-browser import`、复制资料目录或临时手工改写浏览器存储。`createTaskSpace` 仍会继承绑定资料的状态，不能当成新空白资料。

每次展示先走底层资料门禁：

```bash
python3 ~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py fresh-display \
  --task "fresh display task"
```

官网说明将 Space 描述为 BrowserContext，但本机运行态复测发现：在一个 Space 写入 Cookie 后，另一个 Space 可以读到它。因此不能把 Space、空标签页或新窗口称作独立资料，也不能作为“新空白资料”的替代品。当前公开底层 API 仅能列出和绑定资料，未提供 `createProfile`；结果为否时必须以 `EGO_FRESH_PROFILE_UNAVAILABLE` 停止，绝不改用默认、已认证、旧展示资料、导入/复制资料目录，或手工改写浏览器存储。

一般已授权网页任务仍以 `start` + `run --task-space` 进行；`run` 会记录外部网站标签，展示结束调用 `sites` 或 `site-history` 查看。本机内网不产生标签。

## Ego 官网资料与本机能力矩阵（2026-08-08）

官网的 [Space 说明](https://lite.ego.app/document/en/docs/space)、[浏览器自动化说明](https://lite.ego.app/document/en/docs/ego-browser) 和 [更新日志](https://lite.ego.app/changelog) 说明了 task space、资料绑定、底层 Node/CLI helpers 与多窗口行为；[官方仓库](https://github.com/citrolabs/ego-lite) 和 [v1.2.5 发布说明](https://github.com/citrolabs/ego-lite/releases/tag/v1.2.5) 是技能与兼容性变更的来源。本机 CLI 为 `ego-browser 0.4.6.12`（Chromium `150.0.7871.101`）。

官网把 Space 描述为独立 BrowserContext，但本机用两个 agent-owned Space 在同一站点复测：A 写入 test Cookie 后，B 能读取。因此这里的安全策略以运行态为准：**Space 只能隔离窗口/任务所有权，不能作为 Cookie、登录态或“独立资料”的隔离边界。**

| 范围 | 实测结论 | 标准用法 |
| --- | --- | --- |
| 任务空间、标签页、导航 | 通过：`start/run/finish`、`listTabs`、`currentTab`、`ensureRealTab`、`openOrReuseTab`、`gotoAndWait`、`gotoUrl`、`createTab`、`closeTab`、`switchTab` | 一律通过 router；`switchTab` 传 tab 对象，不能传 tab ID 字符串 |
| 观察 | 通过：`pageInfo`、`snapshotText`、`snapshotRaw`、`snapshot`、`drainEvents`、`captureScreenshot` | 先语义快照；截图可用但每次仍须检查返回结果 |
| 鼠标、滚动、输入、文件 | 通过：`click`、`doubleClick`、`hover`、`dragMouse`、`scrollBy`、`scroll`、`scrollToBottomUntil`、`fillInput`、`typeText`、`pressKey`、`uploadFile` | 普通表单优先 selector/ref；rich editor 先做小写入探针 |
| 等待、网络、底层 | 通过：`wait`、`waitForElement`、`waitForLoad`、`waitForNetworkIdle`、`serverFetch`、`browserFetch`、`js`、`cdp` | 页面 DOM 用 `js`，不使用过时的 `elementEval` |
| 并发与标签 | 通过：两条各等待 2 秒的 `run` 实测时间段不重叠；本地 `127.0.0.1` 回归的外部标签数为 0 | 所有 agent 只能走 router 的 `run`；外网仅记 hostname，内网永不记标签 |
| 已知差异 | `dispatchKey` 三次功能探测均未触发目标事件；`newTab`、`elementEval`、`httpGet` 在运行态为 `undefined`；`help('click')` 返回 `Unknown helper` | 键盘用 `pressKey`/`typeText`；新标签用 `createTab`；评估用 `js`；请求用 `serverFetch`/`browserFetch`；不要依赖 `help()` |
| 受安全约束未主动测试 | `handOffTaskSpace`、`claimTaskSpace`、`takeOverTaskSpace` | 它们会改变人/agent 控制权，只能在用户明确接手或明确要求继续时执行 |

可重复运行本机无认证回归（脚本临时启动 `127.0.0.1` 服务，结束时关闭 task space 和服务）：

```bash
EGO_FIXTURE_PORT=8773 ./scripts/verify_ego_capabilities.sh
```

脚本会把 `dispatchKey` 作为已知能力缺口报告，但其余必需项、两次并发 `run` 的互斥和截图都会作为通过条件。若脚本出现新的必需项失败，不应改用鼠标、Chrome 或其他浏览器后端掩盖问题；先保留输出并修复 Ego/CLI 兼容性。

## Cloudflare 域名和 DNS 的实际操作顺序

### A. 只需要 Cloudflare 托管 DNS / CDN（最常见）

1. 在 `网站` 选择 **添加站点**，输入已有域名并选择计划。
2. 让 Cloudflare 扫描现有 DNS。逐条比对 A/AAAA、CNAME、MX、TXT、DKIM、SPF、验证记录和子域名；扫描结果不是完整性证明。
3. 在原注册商处，把权威 nameserver 改为 Cloudflare 页面给出的那一对值。不要从旧笔记或别的域名复制 nameserver。
4. 等 Cloudflare 将 zone 显示为 active，再进入该站点的 `DNS` → `记录` 进行最小必要修改。
5. 记录编辑后，分别在页面与外部 DNS 查询验证记录和值；对网站流量再验证实际 HTTP(S) 行为。DNS 页面保存成功不等于邮件、证书或应用已恢复。

记录类型与代理状态要匹配用途：A/AAAA/CNAME 的 Web 流量才考虑橙云代理；MX、邮件相关 CNAME/TXT、以及非 HTTP 服务必须保持 DNS only。每次改动前先截取或导出当前记录，改一组就验证一组，避免把可用邮件或验证记录一起覆盖。

### B. 购买新域名

1. 进入 `域名` → `注册`，在搜索框确认域名可用性与可注册后缀。
2. 核对注册年限、注册联系人要求、价格与续费设置，再进行付款与最终确认。
3. 成功后仍要进入对应 zone 核对 DNS、自动续费、联系资料及 DNSSEC 的实际状态。不要把“下单页”当作已生效证明。

官方参考：[Register a new domain](https://developers.cloudflare.com/registrar/get-started/register-domain/)。

### C. 将注册商转入 Cloudflare

1. 先按 A 的流程把域名作为网站添加到 Cloudflare，并确认 zone / nameserver 迁移状态。
2. 在旧注册商确认该后缀允许转出、解除 transfer lock，并取得所需 authorization / EPP code（具体要求以该后缀与旧注册商为准）。
3. 进入 `域名` → `转移`，逐项填写和确认；涉及付款、联系人和最终转出确认时交给用户完成。
4. 通过页面的 transfer status 跟踪结果。注册商迁移完成前后都复核 nameserver、DNSSEC 与续费状态，避免把注册迁移和 DNS 托管混为一件事。

官方参考：[Transfer your domain to Cloudflare](https://developers.cloudflare.com/registrar/get-started/transfer-domain-to-cloudflare/)。

### D. 修改已有 DNS 记录

1. 选中正确 zone，打开 `DNS` → `记录`；先按名称和类型定位已有记录，避免新增一个同名冲突记录。
2. 添加或编辑一条记录时，核对 name、type、content、TTL 和 proxy status。TXT/MX/SRV 记录尤其要逐字符复核。
3. 点击保存后，重新读取该行以确认 Cloudflare 已接受值；再用外部递归 DNS 查询确认对外解析。若变更影响生产服务，先遵守项目既有备份、审批和运行态验证要求。

官方参考：[Manage DNS records](https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-dns-records/)。

### E. Cloudflare Tunnel：改公网解析、改源站地址与“CF 优选”是三件事

| 实际目标 | 正确位置 / 正确值 | 不能误改成 |
| --- | --- | --- |
| 给 Tunnel 增加或更换公网主机名 | Cloudflare Zero Trust 的 `Networks` → `Tunnels`，选中正确 Tunnel，在 `Routes` / `Published application` 添加或编辑 hostname；DNS 记录应为 CNAME，目标是 `<Tunnel-UUID>.cfargotunnel.com`。 | 不要把 CNAME 内容填成源站 IP，也不要填所谓“CF 优选 IP”。 |
| 公网域名不变，只改变 Tunnel 要连接的源站 IP、协议或端口 | 在同一 Tunnel 的 hostname route 编辑 **Service**，例如受控的 `http://127.0.0.1:PORT` 或内部服务地址；先确认 cloudflared 所在主机实际可连该服务。 | 不要修改该 hostname 的 CNAME；它仍须指向同一 Tunnel UUID。 |
| 希望访客或 connector 更快接入 Cloudflare | 保持 Cloudflare 默认 Anycast / Tunnel 多连接机制；分别检查访客网络、Tunnel health、connector 所在网络和源站响应时间。 | Cloudflare 控制台没有“将 Tunnel 绑定到某个 CF 优选 IP”的 DNS 开关。 |

操作顺序：

1. 在 Tunnel 页面确认正确的 Tunnel、connector 为 healthy，并确认目标 hostname 没有已有冲突的 A、AAAA 或 CNAME 记录。
2. 若是**改源站**，只编辑该 hostname route 的 Service；保存前核对协议、IP/主机名、端口和 Host header 要求。此步骤不会自动替换现有 DNS CNAME。
3. 若是**改公网 hostname**，由 Published application 路由创建/关联 CNAME，或在 `DNS` → `记录` 手动建立同帐号的 `<UUID>.cfargotunnel.com` CNAME；检查名称和 UUID 后再保存。
4. 保存后，重新检查 Tunnel health、DNS CNAME、HTTPS 访问和应用日志。DNS 记录与 Tunnel 健康是独立的：CNAME 仍在但 Tunnel 停止时，访客会得到 1016 错误。

“CF 优选”通常是把 Cloudflare 任播边缘 IP 当作可手工固定的单一 IP；这不适用于 Tunnel。Cloudflare 的代理 IP 是共享 Anycast 网络，访客会被路由到邻近数据中心；每个 `cloudflared` connector 也会维持多条到至少两个数据中心的长连接。固定社区列表中的 IP 不会改变 Tunnel 的 CNAME 规范，还可能降低容错或造成 TLS / 路由问题。

官方参考：[Tunnel DNS records](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/dns/)、[Published applications](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/)、[Cloudflare IP addresses](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/)。

## 高风险动作前的停止点

- 改 nameserver、DNSSEC、注册联系人、自动续费、注册商转移、删除 zone / DNS 记录，或点击付款/最终确认前：重新读取当前页面，指出影响对象和回退方式，并等用户确认。
- Cloudflare Access 策略、Tunnel 的 Public hostname / Service route、WAF 或生产主机访问不是“域名操作”的普通延伸；仍须遵守已有服务器连接与变更规则。
- 未登录、资料不确定、页面要求验证码，或 task space 已被用户接管时：停止自动化。可以把任务空间交给用户，但不能绕过这些步骤或改用未知资料。
