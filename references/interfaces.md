# 派猫猫旅行 · 接口状态与抓包 SOP

> 维护说明：每次抓包确认新接口后，更新本文件并回填 `scripts/cat_travel.py` 的 `CONFIG`。

## 已确认接口（截至 2026-08-31）

| 用途 | 方法 | 路径 | 请求体 | 响应要点 | 状态 |
|---|---|---|---|---|---|
| 状态查询 | GET | `/activity/growth/buddy/travel/status` | 无 | `data.state`(traveling/arrived/idle)、`duration_hours`、`record_id`、`arrive_at`、`server_now`、`daily_limit_reached`、`reward_credit` | ✅ 已确认 |
| 领取积分 | POST | `/activity/growth/buddy/travel/claim` | `{}` | `code=0`，`data.state=idle`，`reward_credit=N` | ✅ 已确认（实跑通过） |
| 触发派发 | POST | `/activity/growth/buddy/travel/depart` | `{"location_id":1}` | `code=0`，`data.state=traveling`，`arrive_at`/`depart_at` 用于计算到达时间 | ✅ 已确认（2026-08-31 抓包实跑） |

> 注：`location_id=1` 对应咖啡馆（coffee，duration_hours=3）。不同目的地 `location_id` 不同，旅行时长以 `arrive_at-depart_at` 为准，脚本等待逻辑自动适配。

## 抓包步骤（一次性）

1. WorkBuddy 桌面端 → 成长计划（猫猫旅行页面）。
2. F12 → Console，粘贴 `scripts/cat_travel_capture_helper.js` 整段，回车。
3. 点击「派猫猫旅行」按钮。
4. 控制台打印 `[CAT-TRAVEL] 旅行触发接口` 段（`method=POST`，真实 `url`，`body` 通常 `{}`）。
5. 把该段发给 AI → AI 填实 `CONFIG["start_path"]` / `start_method` / `start_body`。
6. 同步更新本文件"待确认接口"为"已确认"，并标注日期。

## 领取接口实测样本（2026-08-28）

请求：
```
POST /activity/growth/buddy/travel/claim
headers: Accept: application/json, text/plain, */*; Content-Type: application/json; X-Client-Platform: web
body: {}
```
响应：
```json
{"code":0,"msg":"OK","data":{"state":"idle","record_id":1234567,"reward_credit":5}}
```
重复领取（今日已领）：
```json
{"code":400,"msg":"no unclaimed travel"}
```

## 触发接口实测样本（2026-08-31，location_id=1 咖啡馆）

请求：
```
POST /activity/growth/buddy/travel/depart
headers: Accept: application/json, text/plain, */*; Content-Type: application/json; X-Client-Platform: web
body: {"location_id":1}
```
响应（节选，record_id 已脱敏）：
```json
{"code":0,"msg":"OK","data":{"state":"traveling","record_id":1234567,"location":{"id":1,"code":"coffee","name":"咖啡馆","duration_hours":3},"depart_at":1788144034,"arrive_at":1788154834,"server_now":1788144034,"daily_limit_reached":false}}
```
> 说明：`arrive_at - depart_at = 10800s = 3h`（咖啡馆）；脚本读取**旅行时长**并在到达后 **+15min 缓冲**触发领取（触发时间 = depart_at + 旅行时长 + 15min）。

## 旅行时长数据源与领取触发机制

- **能否读取旅行时长**：能。state=traveling 后，`status` 与 `depart` 均返回 `arrive_at`、`depart_at`、`duration_hours`。
- **读取优先级**：① `arrive_at - depart_at`（权威秒数）→ ② `location.duration_hours` → ③ 顶层 `duration_hours` → ④ 兜底固定 4.5h。
- **触发条件**：实际经过时间 ≥（旅行时长 + 15min）即触发领取；即 `触发时间 = depart_at + 旅行时长 + 900s`。
- **领取实现**：到触发点后 `POST /activity/growth/buddy/travel/claim`（body `{}`）；`HTTP 400 no unclaimed travel` 视为今日已领（幂等成功）。
