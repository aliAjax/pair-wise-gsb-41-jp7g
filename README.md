# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付、最终核定与**再保分保台账**服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`、`finance`（财务/再保）。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果，并同步生成再保分保台账

### 再保分保

合约按**灾害事件**配置（一个事件一套累计层上限）：自留额先扣（每案，不参与分保），
之后按层序走瀑布：每层在**剩余赔付上限**内吸收进入本层的损失，按分保比例摊给再保人，
公司承担剩余共保部分；本层占满后差额滚入下一层；全部层占满仍有剩余记为「未覆盖」。
同一事件内所有已核案件**累计占用同一套上限**。

- `POST /api/reinsurance/treaties`：配置事件合约（`event_id`、`treaty_no`、`name`、`retention`、`layers[]`，每层含 `layer_order`、`reinsurer`、`cession_pct`、`payout_cap`）
- `POST /api/reinsurance/layers/add`、`POST /api/reinsurance/layers/update`：加层/改层（上限不得低于已占用）
- `GET /api/reinsurance/state?event_id=...`：合约、各层占用（已吸收/已摊/剩余）、逐案逐层台账、事件汇总
- `GET /api/reinsurance/preview?event_id=...&payout=...`：试算分摊，不落账、不占容量
- `POST /api/reinsurance/recompute`：按案件核定顺序重算整套台账；`commit:true` 落账，`accept_uncovered:true` 允许未覆盖落账

核定（或重算）存在未覆盖差额时**默认拦截**，409 响应的 `details` 列出被挡案件、
涉及的合约层（层序/再保人/上限/案前占用/溢出）与未覆盖金额；前端可核对后以
`accept_uncovered:true` 重新提交，差额以 `uncovered` 台账行落账。分保结果随核定持久化，
重开页面可在「分保台账」区逐案逐层核对。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别、角色权限，以及分保瀑布、
事件累计上限占用、未覆盖拦截与明细、强制落账、改层重算和台账持久化。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；
自留额按每案固定金额、合约按单一灾害事件建模（不含合约共保人、分保佣金、未满期保费和层回复）；
支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
