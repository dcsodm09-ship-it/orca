# Claude Design 网页版（claude.ai/design）功能实测清单

调查方式：Ego 浏览器实测，账号 `skskskkssmsksk00@gmail.com`（用户本人邮箱，测试前该账号 Claude Design 下无任何项目）。新建了一个一次性测试项目 "Capability probe test canvas" 走完整个真实创建流程，全程只读观察 + 一次授权范围内的最小生成动作，未修改/删除任何已有内容。对比基准是 Claude Code 内置 `/design` 命令（早期预览版）自身 SKILL.md 里写明的已知限制。

## 一、首页（新建入口）

- **模板库**：Blank（空白）、Mobile app design、Slides、Document、Wireframe、Animation、UI mockups、Résumé、3D object、Research、HTML email、Color + type pairing、Diagram、Flier，共 14 个，未发现需要下滑才能看到的更多模板。
- **Design system 下拉**：默认 "None"，可关联一个已有设计系统（未展开细节，见下文限制）。
- **"Start from code" 开关**：默认关闭，字面上支持"从代码开始"这一路径（本地预览完全没有这个选项）。
- **Model 选择**：默认显示 "Opus 5"，本地测试全程使用高（High）推理档，未逐一枚举下拉里的完整模型列表（未点开）。
- **"Set up a design system!" 横幅**：首页常驻提示，文案"Create more consistent, on-brand designs"——本次只看到入口，没有实际走完这个流程（未创建设计系统，避免产生不必要的账号状态）。
- **Projects / Design systems / Templates 三个 tab**，搜索框、"仅显示星标"、列表/缩略图两种视图切换——这些都是纯项目管理功能，本地预览没有对应物（本地预览没有"项目列表"这个概念，每次都是发布成独立 Artifact）。

## 二、创建流程：这是最大的实测差异

**本地 `/design` 是一次性生成**：用户给一句话描述 → 直接产出 `.dc.html` → 发布。没有对话、没有追问、没有迭代记忆。

**网页版是一个持续存在的 Agent 会话，不是一次性生成**：

1. 点 "Blank" 新建项目后，右侧不是画布，而是一个**聊天面板**，标题是项目名，里面先弹出一个结构化的"Ask user"表单，而不是直接开始画：
   - "What is this canvas for?"（这个画布是干什么用的：变着花样的沙盒 / 深挖单一能力的压力测试 / 会持续追加需求的草稿面 / 要给别人看的演示）
   - "Which capabilities to exercise?"（勾选要测试的能力，给了 12 个选项：交互原型/App UI、幻灯片、动效、数据可视化/图表、可打印文档或传单、3D 对象、地图、表单+校验状态、复杂网格布局、字体排印样张、微交互/悬停态、**"Live API calls (Claude)"**——最后这条意味着生成的设计本身可以真的去调用 Claude API，这是本地预览完全没有的能力）
   - "Depth per item"（速写 / 中等保真 / 完全打磨三档）
   - "How many probe panels?"（滑块，2–12，默认 6）
   - **"Visual direction" → "Choose a design system… Browse"**（可以在创建时就直接挂载一个已有设计系统，走的是设计系统的真实数据而非我瞎猜的 token）
   - **"Any existing code to build on?" → "Choose a repository from GitHub… Connect" / "Attach a local codebase… Browse"**（可以连 GitHub 仓库或本地代码库作为设计依据——本地预览完全没有代码感知能力，只能凭我读到的项目文件手工临摹）
   - 一个 400 字符的自由备注框
   - 三个动作按钮："Decide for me"（AI 自行决定）/ "Ask me follow-up questions"（AI 继续追问）/ "Send answer"（提交我填的答案）
2. 实测发现 **"Decide for me" 按钮点击多次都没有触发流程推进**（点击本身没有报错，但界面停留原状，原因未查明——不确定是我这次操作手法问题还是这个按钮本身有问题，如实记录，不确定的不瞎猜）。改成在备注框里手写一句话 + 点 "Send answer" 后，流程正常往下走。
3. 提交答案后进入 "Designing" → "Creating Component" → "Designing, Finishing up" 几个可见阶段，聊天面板里会实时显示一句自然语言总结（"One panel, dark editorial, probing live state, CSS motion, a hoverable bar series, and hover feedback. Tweaks: headline, stamp, tag row on/off."），然后显示 "Created Capability Probe.dc.html"。
4. **生成完成后立刻弹出一个 1–5 星评分组件："How did this design turn out?"**——这是一个质量反馈回路，本地预览完全没有。
5. 聊天输入框在生成完之后依然在，可以继续用自然语言追加要求（"Describe what you want to create..."），也就是说这是一个可以无限轮次对话式迭代的画布，不是一次性产物。

## 三、生成后的画布编辑器工具栏（实测截图观察，非猜测）

顶部工具栏实际出现的控件：

- **Reload**（重新加载）
- **Tweaks**（独立的"调节"入口按钮——本地预览把可调 prop 做成画板上方的一排 chip，网页版是一个单独的工具栏按钮）
- 文件名按钮（当前文件 "Capability Probe"，暗示多文件/多组件项目内可切换）
- **Zoom level**（100%，可点开调整，本地预览没有显式缩放读数控件，只有手势缩放）
- **"Canvas mode" 分组，三个互斥模式：Interact / Comment / Edit**——这是本地预览完全没有的显式模式切换。本地预览是"打开就是可编辑状态"，没有专门的"仅浏览/仅评论/仅编辑"三态切换；网页版把这三者做成了正式的顶栏模式按钮。
- **Present**（演示模式按钮）——对应"Slides"模板场景，本地预览无此功能。
- Share、Account menu。

右侧还实测看到一个**属性/Tweaks 面板**，直接列出这次生成暴露出的可调项：`headline`（文本框，当前值 "Four probes, one restrained surface."）、`showTags`（开关型）、`stamp`（文本框，当前值 "PROBE 001"）——这跟本地预览的 `data-props` tweak chip 概念是同一件事，但网页版做成了常驻侧栏，不是画板上方的浮动 chip。

## 四、跟本地 `/design` 预览的差异汇总

本地 `/design` 的 SKILL.md 原文自己承认："这是 early preview……跟 claude.ai/design 的功能对等不是这个 preview 阶段的目标"，以及"设计系统色彩 token 和 request-tweaks 智能体循环不可用，依赖 claude.ai/design 后端"。本次实测**验证属实，并补充了三条 SKILL.md 里完全没提到的差异**：

| 能力 | 本地 `/design` 预览 | 网页版实测 |
|---|---|---|
| 生成方式 | 一次性、非对话式 | **持续的 Agent 会话**，先结构化提问再生成，可无限轮次自然语言追加需求 |
| 代码感知 | 无——只能靠我手动读项目文件临摹 | **可连 GitHub 仓库 / 本地代码库**直接作为设计依据 |
| 设计系统 | 完全不可用（色彩 token 不可用） | 首页可直接挂载已有设计系统，创建表单里也有"Visual direction → 选设计系统"入口 |
| Tweaks（可调属性） | 画板上方浮动 chip | 独立工具栏按钮 + 常驻侧栏面板，交互形式不同但底层概念一致 |
| 画布模式 | 单一模式（打开即可编辑） | **Interact / Comment / Edit 三态显式切换** |
| 演示 | 无 | **独立 Present 演示模式** |
| 质量反馈 | 无 | 每次生成完成后弹 **1–5 星评分组件** |
| "Live API calls (Claude)" 能力 | 无 | 创建表单里的一个可勾选选项，生成物本身可真实调用 Claude API |
| 模板库 | 无 | 14 个起始模板 |
| 编辑器代码更新 | 打包死在每个已发布画布里，官方后续修复不会同步 | （未验证，合理推测是持续更新的在线服务，未在本次实测中直接确认版本迭代行为） |
| 多人协作/冲突 | 后保存者覆盖，不合并 | 未在本次实测中触发多人同时编辑场景，未验证网页版是否有版本历史/合并能力，如实标注为未验证 |

## 五、未验证 / 存疑事项（如实标注，不瞎猜）

- "Decide for me" 按钮多次点击未见反应的具体原因未查明（可能是我操作问题，也可能是该按钮当时状态异常）。
- "Set up a design system" 完整流程（具体要求上传什么、支持哪些 token 格式）没有走完，只看到入口文案，没有展开细节。
- Model 选择器的完整候选列表没有点开查看，只确认了默认值 "Opus 5"。
- GitHub 仓库连接 / 本地代码库上传的具体交互（授权方式、支持的文件类型限制）没有实测，只看到入口按钮文案 "Connect" / "Browse"。
- Comment 模式、多人协作、版本历史等均未触发验证。

## 结论

本地 Claude Code 里的 `/design` 命令官方自己承认是"早期预览、不追求跟网页版对等"，这次实测证实这个说法是准确的——网页版不只是"画布编辑器更完整"，而是整个产品形态不同：网页版是一个**持续对话的设计 Agent + 项目管理系统 + 代码/设计系统集成平台**，本地预览只是其中"发布成 Artifact 展示"这一个环节的简化单点实现。这些差距（对话式迭代、代码感知、设计系统关联、演示模式、质量反馈回路）都是 Anthropic 产品侧的既有设计，不在这个仓库、也不在 Claude Code 用户可修改代码的范围内，无法通过本地开发"补齐"。
