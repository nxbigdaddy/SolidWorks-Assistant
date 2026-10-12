# 数据库设计

数据库位于当前用户本地数据目录，使用 SQLite、WAL、外键和显式 schema 版本。

## 1. 主要表

| 表 | 用途 |
| --- | --- |
| `library_roots` | 用户登记的扫描目录与递归选项 |
| `documents` | 文件路径、类型、指纹、状态和当前参数哈希 |
| `manifest_snapshots` | 规范化 Manifest、哈希、文件版本和读取时间 |
| `document_events` | 新增、变化、缺失、恢复和读取失败历史 |
| `tags` / `document_tags` | 标签与多对多关系 |
| `jobs` | 持久批任务、输入/输出、进度、租约、心跳和控制请求 |
| `job_items` | 有序幂等任务项、尝试次数、结果、错误和审计时间 |
| `semantic_documents` | 本地规范化搜索文本、字段令牌、源 manifest 哈希和索引版本 |
| `demonstration_sessions` / `demonstration_actions` | 人工操作会话、命令事件和步骤前后差异 |
| `recipes` | 已审批配方、上下文约束、回放统计和安全状态 |
| `capability_runs` | SolidWorks/Interop 版本下的能力结果、证据与环境标签 |
| `selection_references` | 已保存文档中的命名目标与SolidWorks持久引用 |
| `plan_history` | 用户需求、计划摘要和最终状态 |

## 2. 一致性

路径以绝对规范形式保存，另存大小写折叠键用于 Windows 唯一性。Manifest 使用稳定键排序和紧凑
UTF-8 JSON 后计算 SHA-256。数据库更新使用事务；文件发布与数据库提交之间用任务状态恢复。

## 3. 迁移

当前新库 schema 为 3。版本 1 会事务升级并增加命名目标引用表；版本 2 会在同一事务中升级批任务并新增语义索引、操作学习和能力实验表。遇到未知更高版本会拒绝启动，不会猜测升级。旧版数据库通过专用只读导入器读取，绝不让新仓储直接依赖旧表结构。
