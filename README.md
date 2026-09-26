# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/roster.py`：随行家属名单规则——关系/出生日期校验、未成年判定、新增/退出/重新加入/成年核对、变动流水。
- `src/materials.py`：材料计算——按当前名单逐人计算要求材料、已交材料和缺项，退出者保留历史。
- `src/rules.py`：状态转换、法定天数、补件期限、材料完整性、决定前逐人核对，以及名单/材料用例的规则编排。
- `src/repository.py`：SQLite建表、事务和查询（名单和材料随payload持久化，不另建表）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：名单、缺项、变动记录详情演示页。
- `tests/`：完整流程、规则计算、名单/材料计算、HTTP接口和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/roster`：随行家属名单变动，`data.op`为`add`（`person:{name,relationship,birth_date}`）、`remove`（`person_id,reason`）或`refresh`（按`as_of_date`核对成年状态）。
- `POST /api/records/{id}/documents`：把材料登记到具体人员名下（`person_id`，主申请固定为`primary`；`documents`代码列表）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 随行家属台账

- **收案登记**：创建记录时`data.dependents`可携带家属，每人必须有`name`、`relationship`（`spouse`/`child`/`parent`）和`birth_date`（YYYY-MM-DD）。
- **未成年子女**：未满18周岁的子女自动要求`guardianship_declaration`（监护声明）；按日历年龄计算，生日当天视为成年。
- **名单变动**：新增、退出和成年核对都写入`payload.roster_changes`（含序号、日期、类型、人员和说明），并同步写入审计时间线。人员编号（P1、P2…）退出后不复用；同一人（姓名+出生日期）再次加入会重新激活原记录，历史材料仍挂其名下。
- **材料重算**：每次名单或材料变动后，按当前在案人员（主申请人+在册家属）逐人重算`required_by_person`、`missing_by_person`，缺项并入去重后的`pending_documents`待补区。退出者不参与缺项计算，但其要求清单、已交材料和退出原因完整保留。
- **决定前逐人核对**：`submit`和`decide`会先按`as_of_date`重算年龄和材料，任一在案人员有缺项即拒绝，错误信息逐人列出（如“钱小宝缺少：监护声明”）；主管可在`submit`时用`supervisor_waiver`豁免。
- **重开核对**：新增`reopen`动作（仅主管，`decided → submitted`，需`reopen_reason`），重开后可继续调整名单、补交材料并再次决定。决定/归档状态下名单冻结。
- **权限**：名单变动允许`intake_officer`/`legal_rep`/`case_officer`/`supervisor`；材料登记允许`legal_rep`/`case_officer`/`supervisor`。

家属按关系要求的材料代码：配偶`id_copy,marriage_certificate,passport`；子女`id_copy,birth_certificate,passport`（未成年追加`guardianship_declaration`）；父母`id_copy,kinship_certificate,passport`。主申请人材料仍由创建时的`required_documents`决定。规则（`roster.py`）、材料计算（`materials.py`）和存储（`repository.py`）三者分开实现，互不依赖。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、随行名单与材料重算、HTTP接口、重复引用、权限拒绝和版本冲突。
