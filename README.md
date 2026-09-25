# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`、`finance`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果
- `POST /api/reinsurance/treaties`：按灾害事件建立分层分保合约（`finance`/`supervisor`）
- `GET /api/reinsurance/treaties`：列出全部事件合约与汇总占用
- `GET /api/reinsurance/ledger?event_id=...|treaty_id=...`：逐案逐层分保台账
- `POST /api/reinsurance/confirm`：财务保存确认分保结果（乐观锁 `expected_version`）

## 再保分保规则

按灾害事件配置一套分保程序：**自留额先扣**，其后按层序分保；每层含再保人、分保比例、赔付上限。

- 层赔付上限是**摊回口径**且为事件内全部已核案件共享：每层最多吸收毛赔款 `上限 / 分保比例`，摊回 = 毛额 × 比例。
- 案件瀑布：先占自留额；剩余按层序逐层吸收，本层占满后的差额滚入下一层；全部层占满仍有剩余记为**未覆盖**。
- 同一事件内案件按核定先后累计占用同一套上限；后续核定自动重算整表，并把已确认合约退回待确认。
- 确认时若存在未覆盖金额，返回 409，`details.blocked_claims` 逐案列出超赔案件、溢层路径（合约层、超出金额）。
- 分摊结果（`cession_entries`）与溢层记录（`cession_overflows`）持久化到 SQLite，重开页面可逐案逐层核对。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别和角色权限。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
