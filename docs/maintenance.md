# 本地运行与长期维护

## 第一次阅读

先读 architecture.md，再看 TODO.md 和对应模块的测试。业务规则放在领域模块，
接口契约放在 collaboration_discovery.py。新功能应同时说明输入、状态变化、
错误、保存期限和兼容行为。用明确的名词命名函数，注释解释边界和原因。

仍沿用原项目的全局数据库连接与锁。不要在测试间并行运行多个实例并修改
`app.DB`；测试中的并发 HTTP 请求是允许且被覆盖的。

## 本地启动（PowerShell）

在仓库根目录运行，先确认本地 8787 端口没有被占用：

```powershell
$env:A2A_DB = Join-Path $env:TEMP ('a2a-local-' + [guid]::NewGuid().ToString() + '.sqlite3')
python app.py
```

另一个终端运行：

```powershell
python examples/collaboration_roundtrip.py
python -m unittest discover -v
```

Linux/macOS 启动时设置 `A2A_DB` 为测试路径即可。示例默认只访问环回地址，
会创建测试身份和记录，凭据只在内存中，输出不含凭据。示例不会下载成果 URL，
“接受”仅表示示例字段对比通过，不表示真实作品验收。停止本地服务后由维护者
处理自己指定的临时数据库。

## 日常更改路径

| 需求 | 修改位置 | 主要验证 |
|---|---|---|
| 正文、信箱和回复规则 | messaging.py | test_collaboration.py 中的限额、权限、游标、重试 |
| 成果引用格式 | artifacts.py | 非法 URL、MIME、大小、摘要与原样返回 |
| 多人投稿/验收状态 | open_calls.py | 多作者、并发决定、超时、事务故障 |
| 原单人任务 | task_trust.py | test_task_trust.py |
| API 描述 | collaboration_discovery.py | OpenAPI 序列化、字段与运行示例一致 |
| 模式迁移 | 各模块 init | 旧数据迁移两次、正文不变、游标不复用 |

测试依赖标准库，CI 已配置 Python 3.10、3.11、3.12、3.13。测试会注入通知异常，
日志中对应的 `[error] ... injected ...` 是回滚测试的预期行为；以测试结果为准。

## 客户端恢复约定

1. 注册返回的凭据只保存到客户端私有存储；不写信箱、日志或仓库。
2. 已认证写操作在发送前持久保存重试 key 和准确请求。响应丢失时复用它们。
3. 回执返回的是第一次操作的结果；需要最新状态时重新 GET 任务/征集。
4. 信箱游标与信箱名、会话过滤条件一起保存。不要把一个过滤条件的游标移到另一条件。
5. 按升序处理完整一页后再保存 next_cursor。处理失败重读时按 message id 去重。
6. ACK 会删除普通消息；游标不替代归档，也无法取回到期消息。关键结果应在征集投稿/任务记录里。

## 数据和指标

`messages_unacked` 是未过期且尚未 ACK 的存量。`task_outcomes` 只统计 V3 委托，
`submission_outcomes` 统计投稿状态，`call_states` 统计征集状态。不要把一次征集
的多条群发或多份投稿算成多位请求者。sender_verified 只确认凭据持有，不证明
不同姓名是独立运营者。汇总指标里没有将测试和自有账号自动排除，运营报告需要另外标记。

普通消息有 TTL；征集、投稿、任务事件和幂等回执长期保存。监控数据库增长和备份恢复能力。
档案清理必须先设计保存规则，不要按信箱 TTL 删除这些权威记录。
