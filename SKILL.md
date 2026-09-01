---
name: cat-travel
description: WorkBuddy 成长计划「派猫猫旅行」自动派发与积分领取。自动解密本机登录态，调用旅行触发与领取接口完成每日积分获取，并支持定时任务与接口抓包补全。触发词：派猫猫旅行、自动去旅行、猫猫旅行、领猫猫积分、buddy travel、travel claim。
version: "1.0.0"
license: MIT
---

# WorkBuddy 派猫猫旅行自动化

自动完成 WorkBuddy 成长计划「派猫猫旅行」的派发与积分领取。全流程本机执行：读取登录态 → 触发旅行 → 等待到达 → 领取积分。无后端服务。

## 概述

猫猫旅行是 WorkBuddy 成长中心的每日玩法：派猫猫去某个地点旅行，到达后可领取积分（实测 5~7 分/天）。本 skill 把"派发 + 等待 + 领奖"封装成可一键触发的脚本 `scripts/cat_travel.py`，并配套抓包助手补全未确认接口。

**接口确认状态（2026-08-31）**：
- ✅ 状态查询 `GET /activity/growth/buddy/travel/status` — 已确认
- ✅ 领取积分 `POST /activity/growth/buddy/travel/claim`（body `{}`）— 已确认、已实跑通过
- ✅ 触发旅行 `POST /activity/growth/buddy/travel/depart`（body `{"location_id":1}`）— **已确认（2026-08-31 抓包实跑）**

> 关键事实：猫猫旅行**不会系统自动派发**，必须用户手动点击「派猫猫旅行」或由本 skill 调用触发接口。系统只会在"已派发且到达后"允许领取。

## 工作流

根据用户意图分两种执行路径：

### 路径 A：仅领奖（今日已派发且到达，或用户已手动派发）
适用场景：用户说"领猫猫积分""猫猫旅行到了""领取积分"。
1. 运行 `python scripts/cat_travel.py status` 查看实时状态（`state=arrived` 即可领）。
2. 运行 `python scripts/cat_travel.py claim-only` 领取（幂等：今日已领则返回"无可领奖"，不报错）。

### 路径 B：完整自动去旅行 + 领奖（零配置，默认推荐）
适用场景：用户说"自动去旅行""派猫猫旅行""帮我每天自动领猫猫积分"。
1. **下载即跑，无需任何配置**：触发接口已确认（`start_path=/activity/growth/buddy/travel/depart`，`start_body={"location_id":1}`），直接运行：
   ```bash
   python scripts/cat_travel.py run
   ```
   脚本自动完成：提取本机登录态 → 触发旅行 → 按"旅行时长 + 15min 缓冲"自动等待到到达点 → 领取积分（幂等）。用户**无需填写 token、无需改任何文件**。
   > **小白友好**：运行前会自动检测当前账号是否已开通「成长计划」，未开通则自动用系统默认浏览器打开开通页引导你完成（通常点一下即可），开通后重跑即可，无需手动找入口。
2. **若运行环境不适长时 sleep**（如 WorkBuddy automation 不能常驻 2~4 小时），可拆成两个定时（见文末"可选定时任务"）：
   - `start-only`（触发并把"旅行时长 + 触发时间"写入状态文件）
   - `claim-only`（用状态文件里的"旅行时长 + 15min"自行判断是否到点：未到则自动等待、到点立即领取）
   > 定时任务**默认不启用**，是否配置、什么时间由用户自行决定（见"可选定时任务"小节）。

### 接口抓包补全（start 接口已于 2026-08-31 确认；以下 SOP 供换机/换版本接口变更时复用）
1. WorkBuddy 桌面端 → 成长计划（猫猫旅行页面）。
2. F12 → Console，粘贴 `scripts/cat_travel_capture_helper.js` 整段，回车运行。
3. 点击「派猫猫旅行」按钮。
4. 控制台会打印 `[CAT-TRAVEL] 旅行触发接口` 段（`method=POST`，`url` 形如 `travel/depart`，`body` 形如 `{"location_id":1}`）。
5. 若与已确认路径不一致，把该段发给 AI，AI 填实 `CONFIG["start_path"]` / `start_method` / `start_body`，并同步更新 `references/interfaces.md`。

## I/O 约束

- **输入**：本机 WorkBuddy 登录态（桌面端已登录）；可选环境变量 `CAT_TRAVEL_NODE`、`CAT_TRAVEL_DECRYPT_JS` 覆盖运行时路径。
- **输出**：命令行日志 + `cat_travel_state.json`（状态文件，崩溃可续跑）+ `cat_travel.log`。
- **网络**：仅访问 `https://www.workbuddy.cn`；令牌仅在内存中经 `decrypt-token.js` 提取后立即用于请求，不落盘、不回显。
- **幂等**：start 检测到"已旅行中"跳过；claim 检测到"无可领/今日已领"判成功退出（`HTTP 400 no unclaimed travel`）。
- **等待/触发策略（基于旅行时长）**：触发后以"旅行时长 + 缓冲"作为领取触发条件，缓冲 `claim_buffer_seconds=900`（15min）。
  旅行时长数据来源（按优先级，均为服务器返回）：① `depart`/`status` 响应的 `arrive_at - depart_at`（权威秒数）；② `location.duration_hours`；③ 顶层 `duration_hours`；三者皆无则回退固定 `claim_delay_after_start_seconds=16200`（4.5h）。
  触发时间 = 出发 + 旅行时长 + 15min；`use_polling=False`。详见下方"旅行时长读取与触发机制"。

## 旅行时长读取与触发机制（实现细节）

**1. 旅行时长能否读取？能。** 旅行功能开启（`state=traveling`）后，两个官方接口都会返回旅行时长相关数据：

- `GET /activity/growth/buddy/travel/status`（实时查询）：返回 `arrive_at`、`depart_at`、`duration_hours`、`state` 等。
- `POST /activity/growth/buddy/travel/depart`（触发派发）：返回同样字段，且嵌套 `location.duration_hours`（如咖啡馆=3）。

**2. 旅行时长的读取方式（优先级，见 `resolve_claim_schedule`）**：

| 优先级 | 字段 | 计算 | 说明 |
|---|---|---|---|
| ① 最权威 | `arrive_at` − `depart_at` | 两个服务器 Unix 时间戳之差（秒） | 直接得精确旅行时长，不受整小时取整影响 |
| ② 次选 | `location.duration_hours` | ×3600 得秒 | depart 响应嵌套在 `data.location` 里 |
| ③ 再次 | 顶层 `duration_hours` | ×3600 得秒 | 部分接口直接返回在 `data` 顶层（idle/部分场景可能为 0，故排末尾） |
| ④ 兜底 | 无 | 固定 `claim_delay_after_start_seconds`（4.5h） | 仅当以上全部缺失，避免空等死锁 |

**3. 触发判断**：`触发时间 = 出发时刻 + 旅行时长 + 缓冲(15min)`。即实际经过时间 ≥（旅行时长 + 15min）时，才允许领取。`claim-only` 会读取状态文件里的 `depart_at`/`arrive_at` 解析该触发时间：未到 → `wait_until` 自动等待；已到 → 立即进入领取。

**4. 积分领取实现**：到达触发点后调用 `POST /activity/growth/buddy/travel/claim`（body `{}`），服务端按当前会话推导 record_id 并返回 `reward_credit`。幂等处理：返回 `HTTP 400 {"msg":"no unclaimed travel"}` 视为"今日已领"，判成功退出，不报错。

> 状态持久化：触发/接管时通过 `persist_travel_schedule()` 把 `depart_at`、`arrive_at`、`travel_duration_seconds`、`claim_at` 写入 `cat_travel_state.json`，供 `claim-only` 与崩溃恢复复用。

## 示例

```bash
# 查看状态
python scripts/cat_travel.py status
# 仅领奖（今日已派发且到达）
python scripts/cat_travel.py claim-only
# 完整流程（触发接口已确认时）
python scripts/cat_travel.py run
```

## 可选定时任务（是否启用、什么时间由用户决定）

本 skill **默认不写入任何定时任务**。每日自动领取是可选增强，由用户自行决定要不要、几点跑。

**方案 A：WorkBuddy 定时自动化（最简单，但依赖 App 在触发时刻运行）**
时间填用户觉得合适的时刻（下面 `BYHOUR`/`BYMINUTE` 仅为示例）：
```json
{
  "name": "派猫猫旅行-领奖",
  "scheduleType": "recurring",
  "rrule": "FREQ=DAILY;BYHOUR=13;BYMINUTE=30",
  "cwds": ["<工作目录>"],
  "status": "ACTIVE",
  "prompt": "运行 python scripts/cat_travel.py claim-only（仅领已有旅行，无需 start 接口）。汇报：领取成功得几分 / 无可领（今日已领或还没派发） / 令牌失效需刷新登录。"
}
```
> 注意：WorkBuddy 自动化在电脑**睡眠/休眠时不会执行**。若常睡眠，优先方案 B。

**方案 B：系统计划任务（睡眠也能唤醒，更稳）**
Windows（时间自行替换 `06:00`）：
```powershell
schtasks /Create /TN "CatTravelDaily" /SC DAILY /ST 06:00 /TR "cmd /c cd /d <脚本目录> && python scripts/cat_travel.py run"
```
Linux/macOS（`crontab -e`，例如每天 6 点）：
```cron
0 6 * * * cd <脚本目录> && python3 scripts/cat_travel.py run >> cat_travel_cron.log 2>&1
```

> 成功后想接微信/邮件通知，自行在脚本外层包一层推送即可（本 skill 不含推送实现，避免内置第三方凭证）。

## 边界情况

- **触发接口已确认（2026-08-31）**：`start_path=/activity/growth/buddy/travel/depart`。如需换目的地，改 `start_body` 的 `location_id`；若接口报 404/400，说明版本可能有变，回到"接口抓包补全"重新确认。
- **今日已派发但还没到触发点**：`claim-only` 按状态文件"旅行时长 + 15min"判断，未到则自动等待至触发点再领（或交给定时 `claim-only`，到点自触发）。
- **今日已领过**：claim 返回 `no unclaimed travel`，判幂等成功，不报错。
- **令牌过期（401）**：打开 WorkBuddy 刷新登录态，下次运行重新读取 token。
- **每日限额已到（`daily_limit_reached=true`）**：说明今日旅行已派发，无法再派新旅行，仅能领已有。
- **目的地时长不同**：旅行时长直接取自服务器的 `arrive_at - depart_at`（咖啡馆 3h、其它 1~4h 不等），触发点 = 出发 + 旅行时长 + 15min，自动适配，无需手动调 4.5h；仅当旅行时长完全读不到才回退 4.5h 兜底。
- **跨机器迁移**：路径常量已支持环境变量 `CAT_TRAVEL_NODE` / `CAT_TRAVEL_DECRYPT_JS` 覆盖，`DECRYPT_JS` 默认优先本 skill 自带 `scripts/decrypt-token.js`，无需改代码即可换机。
- **未开通成长计划**：运行时会自动检测，未开通则自动打开浏览器到开通页（`{api_base}/activity/growth`，或 `CAT_TRAVEL_GROWTH_URL` 覆盖）引导开通，并退出码 2；开通后重跑即可，无需手动找入口。

## 令牌自动提取（为什么不需要配置凭证）

你可能会疑惑：skill 里没有账号、没有 token，它怎么登录？

**令牌不在 skill 里，而是运行时从用户本机已登录的 WorkBuddy 桌面端动态提取。**

- `cat_travel.py` 运行时会调用自带脚本 `scripts/decrypt-token.js`
- 该脚本按 `os.homedir()` / `LOCALAPPDATA` / `APPDATA` **自动定位本机登录态文件**
  （新版明文：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`；
  旧版回退：`%APPDATA%\WorkBuddy\User\globalStorage\state.vscdb` 经 Electron safeStorage 解密）
- 读取出的 accessToken 只通过管道在**内存中**传给 `cat_travel.py` 用一次，**不落盘、不回显、不上传**
- 因此：skill 克隆到任何机器，只要那台机器登录了 WorkBuddy，就自动读**那台机器账号**的令牌；
  每人各跑各的登录态，天然隔离 —— 这正是它脱敏可分发、下载即跑的原因

> 前提：本机必须已安装并登录 WorkBuddy 桌面端。若没登录，会提示「未找到本地登录态」，
> 这与缺配置无关，是没登录桌面端。

## 自动检测成长计划开通（小白引导）

本 skill 面向小白设计：运行时会**自动检测当前账号是否已开通「成长计划」**，无需用户自己去找开通入口。

- **检测方式**：调用只读接口 `/activity/growth/buddy/travel/status`，用强信号字段（`state` 枚举 / `daily_limit_reached` / `reward_credit`）判断；
  401/403 或业务码非 0（含"未开通 / 未参与 / 未加入 / forbidden / 无权"等关键词）视为未开通。
- **未开通时**：`ensure_growth_opened()` 打印友好中文提示，并**用系统默认浏览器自动打开开通页面**
  （URL 默认 `{api_base}/activity/growth`，可用环境变量 `CAT_TRAVEL_GROWTH_URL` 覆盖），
  通常用户只需在页面点一次「开通 / 加入」即可；随后脚本以退出码 2 结束，引导用户重跑。
- **生效范围**：`run` / `start-only` / `claim-only` / `status` 全部命令，保证开箱即用。

## 安全说明

- 令牌等同账号凭证，仅在内存中使用，经 `decrypt-token.js` 提取后立即消费，**不写入日志、不落盘、不回显**。
- 网络仅发往 `www.workbuddy.cn` 官方接口，不上传任何第三方。
- 请勿用于批量刷分或违反 WorkBuddy 用户协议的用途；使用者自行承担使用风险。

### 为何需要这些能力（上下文说明）

本 skill 名为"派猫猫旅行"，但完整链路需以下本机能力，均为本地运行、无后端：
- **读取本地令牌**：WorkBuddy 桌面端登录后把登录态存于本地，本 skill 自带 `scripts/decrypt-token.js`（通用版，已随 skill 分发，无需单独安装 `workbuddy-checkin`）提取 `accessToken`，才能调用官方接口。
- **Node.js 运行时**：`decrypt-token.js` 运行依赖 Node（v5.3.8+ 主路径）。
- **Python 运行时**：`cat_travel.py` 用 urllib 发请求，逻辑全在本机。
- **定时任务**：用于每日幂等补做；skill 不自动写入系统定时，由用户/Agent 显式配置。
