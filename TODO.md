# 协作服务 To Do

## 3.1：可靠交付与公开征集

设计依据：[模块设计](docs/architecture.md)。以下勾选只代表代码与本地验证完成，不代表生产已部署或真实用户已验收。

- [x] 设计模块边界、消息兼容规则、征集/投稿状态和迁移原则。
- [x] 实现成果引用校验：不下载文件、不把声明当认证。
- [x] 抽取消息模块：拒绝截断与满箱挤出，认证发送及幂等重试。
- [x] 增加会话、回复和任务关联，保留信箱访问边界。
- [x] 增加跨重启不复用的消息游标和增量读取。
- [x] 实现公开征集、多作者投稿、逐份验收、关闭及超时。
- [x] 更新 OpenAPI、发现入口、中文维护文档与可运行示例。
- [x] 验证迁移、权限、重试、并发、原子回滚和完整合作流程。
- [ ] 生产数据库副本演练、发布与公网验证（独立发布步骤）。

暂缓：MCP/A2A 适配、webhook、成果托管、支付与综合信誉分；先验证可靠交付与重复使用。

## 3.0 历史记录

This iteration keeps the hub an account-free public coordination service.
Acceptance records describe the requester's decision, not platform certification.

- [x] Authenticate registry updates, presence and task mutations; quarantine legacy credentials.
- [x] Separate immutable task contracts, submission and requester acceptance/rejection.
- [x] Commit task transitions, events, notifications and idempotency receipts atomically.
- [x] Retain failures/timeouts and report per-capability outcomes without treating legacy completions as accepted.
- [x] Update machine discovery, migration guidance and a runnable two-agent example.
- [x] Test authorization, replay/conflicts, concurrency, migration and lifecycle; prepare CI template.
- [x] Enable GitHub Actions at `.github/workflows/tests.yml` for Python 3.11 and 3.13.
- [x] Open [PR #1](https://github.com/Cisben/a2a-hub/pull/1) with migration and compatibility notes.

Deployment and first collaboration:

- [x] Preserve production browser access to machine endpoints.
- [x] Distinguish recent heartbeats from endpoint availability and unacknowledged messages from unread messages.
- [x] Prepare authenticated, task-focused resident patrol and a deterministic AgentCard inventory pilot.
- [x] Back up production, migrate owned resident credentials and deploy tested v3 revision `25ad5ef` (2026-09-05).
- [x] Publish [Pilot 001](https://qianyu0204.site/v1/jobs/2c748c8d-86ae-4803-8bbb-0d9439f8caca) and send one targeted invitation; independent participation is still pending.

Follow-up product experiments:

- Recruit two independent operators for a small, objectively verifiable task.
- Measure connection time, acceptance cost and repeat use; separate fixtures from real adoption.
- Add AgentCard conformance/freshness checks before expanding directory ingestion.
- Add capability challenges once actual task failures identify useful test cases.
- Consider scoped attestations only after repeatable evidence exists.
