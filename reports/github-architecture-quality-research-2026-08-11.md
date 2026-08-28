# GitHub/上游架构质量调研（2026-08-11）

状态：只读上游调研与本地设计映射。没有访问仓库凭据，没有触发 GitHub
workflow、release、tag、attestation、SBOM 或远端写入，也没有把上游能力冒充为
Orca 已实现。

## 直接可落地的上游基线

1. [SLSA GitHub Generator](https://github.com/slsa-framework/slsa-github-generator)
   可为任意文件型 release artifact 生成隔离的 SLSA provenance，并由独立 verifier
   验证。Orca 应把 exact commit、workflow identity、每个 DMG/ZIP/update artifact
   SHA-256 与 builder identity 绑定，在 `publish-release` 前验证，而不是发布后才跑
   tag E2E。上游也明确说明：只“生成”provenance 不等于完整达标，分发和消费端验证
   仍必须纳入门禁。
2. [GitHub actions/attest-build-provenance](https://github.com/actions/attest-build-provenance)
   用短期 Sigstore 证书把 artifact name/digest 绑定到 SLSA/in-toto attestation；其
   README 建议新实现直接使用 `actions/attest`。Orca 的 release gate 应下载并验证
   attestation，再比较安装后 App/CLI/Skill 的实际 digest，不能只相信 workflow 成功。
3. [in-toto Attestation Framework](https://github.com/in-toto/attestation)
   将 subject、predicate、认证 envelope 和 bundle 分层。Orca 可用一个 release
   subject 同时引用 SBOM、P0 E2E receipt、startup installed identity、R2 reader
   receipt 与 rollback preimage；每个引用必须带 digest，策略引擎只接受完整集合。
4. [The Update Framework](https://github.com/theupdateframework/python-tuf) 及
   [TUF specification](https://github.com/theupdateframework/specification/blob/master/tuf-spec.md)
   提供版本、expiry、threshold role、rollback/freeze/mix-and-match 防护。它适合
   Orca Skill/Bundle/desktop helper 的客户端更新元数据；单一 SHA 文件不能替代
   version monotonicity、角色阈值和过期策略。
5. [OpenSSF Scorecard](https://github.com/ossf/scorecard) 的
   [Pinned-Dependencies](https://github.com/ossf/scorecard/blob/main/docs/checks.md)
   要求 build/release workflow 的依赖使用不可变 digest。Orca 当前 release action
   的 major tags 应逐步换成完整 commit SHA，并由 Renovate/Dependabot 维护更新。
6. [GitHub dependency-review-action](https://github.com/actions/dependency-review-action)
   能在 PR 中让新增漏洞/许可证问题 hard fail。Orca 应明确 `warn-only: false`，设置
   严格 severity 与 runtime scope；它是合并前门禁，不替代 release artifact 扫描。
7. [Anchore sbom-action](https://github.com/anchore/sbom-action) 可用 Syft 生成 SBOM。
   SBOM 必须作为 exact artifact 的 attested reference，而不是只扫描源码目录。
8. Apple 的
   [XPC peer requirements](https://developer.apple.com/documentation/updates/xpc)
   和 [code-signing requirements](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)
   能在连接层校验 team、identifier、entitlement 或 lightweight code requirement。
   这正是 Desktop Safety Shim 需要的同用户进程隔离；`0600` Unix socket 只能隔离
   其他用户，不能证明 peer 是受信 broker。

## 对 Orca 的最小闭环映射

| 当前缺口 | 最小实现 | 机器验收 |
|---|---|---|
| release 后才跑 tag E2E | pre-publish exact-tag/exact-artifact E2E receipt | dispatch/identity/timeout/失败/陈旧 receipt 全阻断 publish |
| build 与发布物缺 provenance | `actions/attest` 或隔离 SLSA generator | verifier 绑定 commit、workflow、artifact digest，下载后再验 |
| 没有 SBOM/漏洞门禁 | exact artifact SBOM + dependency review + OSV policy | 新增高危依赖和缺失/陈旧 SBOM 均 hard fail |
| action 使用可变 major tag | third-party actions pin full commit SHA | Scorecard Pinned-Dependencies 通过，自动更新走 review |
| Skill/Bundle 只靠本地 SHA | TUF roles/version/expiry + in-toto/DSSE release bundle | rollback、freeze、mix-and-match、单签名者伪造测试全拒绝 |
| desktop raw socket 同用户可绕过 | 受签名 XPC broker/helper，连接双向 peer requirement | 未签名/错 team/错 entitlement/direct raw client 零效果 |
| receipt 各自成岛 | in-toto subject 引用 E2E/SBOM/ACK/R2/rollback digests | 缺任一 predicate、subject 不同或 installed digest 不同即拒绝 |

## 实施顺序

1. 先应用当前本地 release pre-publish gate 候选，并用 disposable draft/tag 做真实
   GitHub E2E；此项不等待完整供应链重构。
2. 将每个 packaged artifact 的 SHA、SBOM 和 P0 E2E receipt 纳入一份 in-toto
   release bundle，再用 GitHub/Sigstore 或 SLSA isolated builder 出具 provenance。
3. pin 所有 third-party actions 到完整 commit，加入 dependency review/Scorecard；
   明确失败阈值与 expiry，禁止 `warn-only` 形成伪门禁。
4. 为 Skill/Bundle/desktop helper 增加 TUF 风格 version/expiry/threshold metadata，
   installer 必须验证 staged、attested、installed 三个 digest 相同。
5. Desktop 从 Hammerspoon raw listener 迁到受签名 XPC service；在这之前保持
   `NO-GO_OFFLINE_CANDIDATE_ONLY`，不把 JS/Lua 租约测试当作同用户隔离证明。

## 边界

这些上游组件只能提供构件。Orca 仍需自己的 policy composition、安装后 identity、
回滚演练、freshness、用户批准和真实 packaged runtime E2E。任何 focused test、
本地 fake、attestation 生成成功或 SBOM 文件存在，都不能单独支持 production-ready
结论。
