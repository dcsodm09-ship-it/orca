# Codex 向 Claude 派单的 bypass 策略

状态：用户批准的长期协作策略  
生效时间：2026-08-15（Asia/Taipei）  
范围：所有由 Codex 主控通过 Orca 向 Claude worker 发出的 Dispatch  
来源：用户在当前会话中明确要求“codex给claude派单必须bypass”  
保留策略：持续有效，直到用户明确替换或撤销  
失效条件：用户明确修改该规则，或经独立验收的新启动契约取代本策略

## 强制规则

1. 所有 `Codex -> Claude` Dispatch 必须请求并实际生效
   `permissionMode=bypassPermissions`。
2. 启动收据必须同时记录 `requested` 与 `effective`；缺失、不同或无法验证时，
   必须在产生任务副作用前失败，禁止静默降级为 `plan`、`manual` 或其他模式。
3. Claude worker 默认只加载 `user` setting source。项目 MCP 仅能通过已审核的
   显式配置和 strict MCP 模式加载。
4. `bypassPermissions` 只取消 Claude CLI 的逐项权限询问，不扩大任务授权。
   外部发送、生产变更、秘密输入、删除、发布及其他高影响动作仍需用户授权。
5. 每个可写 Dispatch 仍须使用独立工作树、单写者路径租约、明确任务范围和
   `worker_done` 结算。只读审阅者即使使用 bypass，也必须由工具策略保持不可写。

## 当前兼容路径

在已安装 Orca launcher 尚不能证明上述权限模式与 setting sources 的
requested/effective 一致时，使用受跟踪的自定义 Claude 终端、精确启动参数、
ready 检查、注入 Dispatch、`worker_done` 和精确资源释放流程。不得把未验证的
普通 `worker-start --agent claude` 当作本策略已经生效。

## 证据边界

- 这是用户批准的控制策略，不是已安装 launcher 原生支持的证明。
- 当前启动上下文为 `ORCA_CONTEXT_NACK_V1`；图谱收录不等于模型已经读取，
  仍需后续有效 startup ACK。
- 不保存账号、凭据、原始会话、工具输出或隐藏推理。
