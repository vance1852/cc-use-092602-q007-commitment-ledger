# 建立搬迁安置承诺履约核算基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `src/resettlement_commitment/`：安置承诺版本、统计窗口、排除规则、家庭确认暂停、不可变事件与周期结算；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
PYTHONPATH=src python3 -m resettlement_commitment.acceptance --workspace .
```

三条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析和改造审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m resettlement_commitment.api --database commitment.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。

## 安置承诺履约核算

`resettlement_commitment` 把政策豁免、家庭主动延期与实际交房合成为可复核的履约结果：

- **承诺版本不可变**：`POST /commitments` 固化三个时限（临时周转、正式交房、公共服务接续）、日补偿单价与提前告知天数；修订只能通过 `POST /commitments/{id}/versions` 形成新版本，必须填写差异原因，项目与批次不得更改。
- **统计窗口与排除规则**：窗口互不重叠并完整保存；计划施工事件只有落在窗口内、窗口有对应规则且提前告知天数达标才允许排除。跨窗口事件按窗口边界精确拆分，逐天输出 `fragment_start/fragment_end`、是否采纳、采纳/重叠/拒绝天数及原因。
- **家庭确认暂停**：`family_extension`（家庭主动延期）、`policy_exemption`（政策豁免）、`family_unavailable` 均需家庭确认与凭据；与施工重叠时暂停优先，同一天只扣减一次。
- **不可变事件**：临交、正式交房、公共服务接续、计划施工只追加，事件重复登记冲突、不可修改。
- **周期结算**：经办编制（`preliminary`）→ 安置运营经理批准（`operations_approved`，编制人不得批准）→ 财务确认（`settled`，与运营授权角色分离）。已批准/已确认结算不会被迟到事件直接改写，只能带 `correction_reason` 编制新版，旧版标记 `corrected` 并保留，差异包含逐户补偿增减与新增事件/暂停清单。
- **可解释与离线复算**：每个家庭、每个时限、每个窗口都列出扣减/排除片段来源事件；`POST /settlements/{id}/replay` 用冻结输入快照重算并比对哈希，`POST /projects/{p}/batches/{b}/recompute` 按项目与批次重算全部窗口。所有写操作进入与现有平台一致的 SHA-256 哈希链审计。
