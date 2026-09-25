# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请和响应情景；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转和审计；
- `fixtures/`：离线验收使用的证据规范与结构化事故记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和 CLI 验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m traffic_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m evidence_review.acceptance --workspace .
PYTHONPATH=src python3 -m penalty_ops.acceptance
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度与证据复核，并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
```

三个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。

## 证据复核任务租约

证据复核队列的分析任务只允许已登记的工作进程领取，租约全程绑定可追溯身份：

- `POST /workers`（统计负责人 `worker.register` 权限）登记工作进程，返回一次性调用凭证；凭证只保存散列。
- `POST /workers/{id}/revoke`（审计人员 `worker.revoke` 权限）撤销进程；`POST /users/{id}/deactivate`（`user.deactivate` 权限）停用账号。被撤销进程或登记账号被停用后，领取、续租、完成、失败全部拒绝。
- `POST /jobs/claim`：携带 `X-Actor-Id`（需 `analysis.run` 权限）、`worker_id` 与 `credential` 领取任务；同一进程重复领取幂等返回当前租约，租约过期后由新进程确定接管，尝试次数与所有权变化持久化。
- `POST /jobs/{id}/renew`：持有者在租约内续租；过期或非持有者续租被拒绝并记录原因。
- `POST /jobs/{id}/complete`、`POST /jobs/{id}/fail`：必须携带领取时返回的 `lease_fingerprint`，旧持有者被接管后的迟到提交会被确定性拒绝。
- `GET /jobs/{id}/leases`（审计人员 `audit.read` 权限）查询该任务每次占用、续租、释放与拒绝的原因。
