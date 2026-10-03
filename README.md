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

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/shares`：审查员登记/替换共同主张人的份额（`parties` 为 `[{"claimant_id","display_name","share"}]`）。改动即作废原确认并重新分配，合计不足 100% 时停在待补齐。
- `GET /api/claims/{id}/shares`：查看份额明细（仅审查员、工作人员，或作为当事人的主张人；公众拒绝）。
- `POST /api/claims/{id}/redistribute`：重新分配（失败后可重试），当前份额合计 100% 才确认。
- `POST /api/claims/{id}/parties/{party_id}/withdraw`：标记某位共同主张人退出，释放其份额。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。份额未确认分配（待补齐）时不能完成返还。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

共同主张支持多位继承人按份额共有：份额改动会先作废原分配确认并重新分配，合计不足 100% 时停在待补齐；主张人无权修改份额，公众看不到份额明细；两位审查员同时修改时后提交的以最新为准，且每次改动都追加版本史、保留上一版；重新分配失败后可重试；旧数据中没有份额记录的主张，回填为登记的主张人一人独占 100%。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
