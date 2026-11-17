# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10621 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 切窗预约关闭（门闩在服务端）

- 切窗策略簿 `window_bookings`：业务日唯一、口径 `CHECK(甲/乙)`，写入即闩死，无更新接口，当日禁止改口；同业务日改投另一方案按「混子窗」拒绝并登记 `policy_deviations`
- 到点判定只认服务端时钟与策略簿 `close_at`：方案甲到点后该业务日全只读，登记/裁决/放行/冻结写入一概拒绝；方案乙到点后仅受理迟到冻结区报名（只进 `late_frozen_enrollments`，永不触达正式切片与水位），成功放行与水位推进仍一概拒绝
- 一切到点后的放行尝试都落 `releases` 拒绝流水 + 偏差登记，水位零推进；已成功放行记录到点后再请求同样拒绝，禁止改写
- 模块级写入门闩：两名值班并发时后手显式收 409「切窗争用」，禁止静默写成成功
- 预约列表 / 策略旁注 / 关日详情 / 放行流水四视图同源对齐；页面禁用仅为提示，绕过前端直打 API 仍被服务端闩死

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-11
docker compose up -d --build
`

浏览器：http://127.0.0.1:10621/  
健康检查：GET http://127.0.0.1:10621/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10621
python -m pytest -q
`
