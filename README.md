# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`reviewer2`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张，初始按登记人一人独占 100% 确认。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `POST /api/claims/{id}/shares`：登记/调整共同主张人份额（`share_percent` 为 0 表示退出），仅工作人员和审查员可操作。
- `POST /api/claims/{id}/reallocate`：按当前份额重新分配，用于分配失败后的重试。
- `GET /api/claims/{id}/allocations`：份额分配版本历史，仅工作人员和审查员可见。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人、内部事件和份额明细；主张人只能查看自己的主张且不能修改份额；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。

## 共同主张份额规则

- 每次份额改动先作废原确认，再按最新份额重新分配并生成新版本，上一版保留在分配历史中。
- 合计等于 100% 时进入 `confirmed`；不足 100% 停在 `pending_completion`（待补齐）；超过 100% 整体拒绝（`share_overflow`），份额与原确认保持不变，可修正后重试。
- 两名审查员同时修改时，后提交者基于最新状态分配，双方改动按提交顺序各自留版。
- 旧数据缺少份额记录的主张，在迁移时按一人独占 100% 回填并确认。
