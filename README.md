# WorkBuddy 派猫猫旅行自动化 · cat-travel

下载即用：一键把 WorkBuddy 成长计划「派猫猫旅行」跑起来并领取每日积分。
全流程本机执行：读取登录态 → 触发旅行 → 等待到达 → 领取积分。**无需任何配置**。

> 本仓库是**主 skill**：目标是让任何人下载后**不改动任何文件**，按流程即可跑通「派发旅行 + 领取积分」。
> 若遇「定时任务没执行 / 会话启动失败」等异常，可自行排查本机登录态与计划任务配置，或参考通用的 WorkBuddy 自动任务排障思路（非本 skill 必需）。

## 功能

- 自动派发猫猫去地点旅行（默认咖啡馆，改 `location_id` 可换目的地）
- 按服务器返回的旅行时长自动等待到到达点（无需硬编码等待时长）
- 幂等领取积分（今日已领则返回成功、不报错）
- 可选每日定时（**是否启用、什么时间，由你决定**，见文末）
- **首次运行自动检测「成长计划」是否开通**，未开通则自动打开系统默认浏览器引导开通（小白友好，免手动找入口）

## 环境要求

- Windows / macOS / Linux
- 已安装并登录 **WorkBuddy 桌面端**，且「成长计划 → 派猫猫旅行」可用
- **Node.js**（可选：已单独安装则直接用；未安装则自动复用 WorkBuddy 桌面端自带的托管 Node，无需单独安装）
- **Python 3**（用于 `cat_travel.py` 调用接口）

## 安装

```bash
# 方式一：装到 WorkBuddy skill 目录（Agent 可识别）
git clone https://github.com/Coco-katarina/workbuddy-cat-travel.git ~/.workbuddy/skills/cat-travel
# Windows 路径：C:\Users\<USER>\.workbuddy\skills\cat-travel

# 方式二：任意目录，直接跑脚本
git clone https://github.com/Coco-katarina/workbuddy-cat-travel.git cat-travel && cd cat-travel
```

## 快速使用（零配置）

**无需填写任何 token、无需修改任何文件**，只要本机装了 WorkBuddy 桌面端并登录：

```bash
# 1) 查看实时状态（state=arrived 即可领）
python scripts/cat_travel.py status

# 2) 今日已派发且到达 → 仅领奖（幂等）
python scripts/cat_travel.py claim-only

# 3) 完整流程：触发旅行 → 按旅行时长自动等待 → 到达即领
python scripts/cat_travel.py run
```

第 3 条 `run` 一个命令就完成「派发 + 等待 + 领积分」，**下载后啥也不用动**。

> 想要**每天自动跑**，先运行一次 `python scripts/cat_travel.py setup` 选好运行方式与领取模式（当天领取 / 隔天领取），向导会自动建好系统定时任务；隔天模式下每日运行 `python scripts/cat_travel.py daily` 即可「先领昨日、再派今日」。

> 若你的账号还没开通「成长计划」，脚本会**自动用浏览器打开开通页**引导你点一下开通，开通后重跑即可——无需你自己去找入口。

## 令牌自动提取机制（为什么无需配置凭证）

你可能会疑惑：skill 里没有账号、没有 token，它怎么登录？

答案：**令牌不在 skill 里，而是运行时从你本机已登录的 WorkBuddy 桌面端动态提取**。

- `cat_travel.py` 运行时会调用自带脚本 `scripts/decrypt-token.js`
- 该脚本按 `os.homedir()` / `LOCALAPPDATA` / `APPDATA` **自动定位本机登录态文件**
  （新版明文：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`；
  旧版回退：`%APPDATA%\WorkBuddy\User\globalStorage\state.vscdb` 经 Electron safeStorage 解密）
- 读取出的 accessToken 只通过管道在**内存中**传给 `cat_travel.py` 用一次，**不落盘、不回显、不上传**
- 因此：你 clone 到任何机器，只要那台机器登录了 WorkBuddy，脚本就读**那台机器账号**的令牌；
  每人各跑各的登录态，天然隔离，这正是它脱敏可分发的原因

> 前提：本机必须已安装并登录 WorkBuddy 桌面端。若没登录，会提示「未找到本地登录态」——
> 这与缺配置无关，是没登录桌面端。

## 首次使用：自动检测成长计划开通

你**不需要**手动去找「成长计划」开通入口。脚本运行时会自动检测当前账号是否已开通：

- **已开通** → 直接跑流程，无感。
- **未开通** → 自动用系统默认浏览器打开开通页面，并打印提示（通常点一次「开通 / 加入」即可）；
  开通后在终端重跑 `python scripts/cat_travel.py run` 即可。

开通页地址默认 `https://www.workbuddy.cn/activity/growth`，也可用环境变量 `CAT_TRAVEL_GROWTH_URL` 指定。

## 自动定时（安装向导，推荐）

本 skill **默认不写入任何定时任务**。想每天自动跑，直接运行安装向导，按提示选即可，**无需手敲任何命令**：

```bash
python scripts/cat_travel.py setup
```

向导依次询问：
1. **运行方式**：`单次手动执行`（推荐先试，随时自己 `run` / `daily`，不创建定时任务） / `配置为自动定时任务`。
   - 建议先跑通一次确认能领到积分，再重跑本向导配置定时，体验更稳。
2. **领取模式**（手动 / 定时都会问，决定 `run` / `daily` 行为）：
   - **当天领取**：旅行最长 4h + 15min 缓冲，自动建 **2 个**任务（旅行 + 缓冲后领取）。
   - **隔天领取**：每天先领昨日积分再派今日旅行，自动建 **1 个** `daily` 任务；首次运行无昨日积分会自动跳过，不报错。
3. **定时任务载体**（仅选定时时）：`系统计划任务`（Windows 任务计划 / macOS·Linux crontab，脚本直接建、睡眠也能跑、更稳） / `WorkBuddy 定时自动化`（生成配置，你在 WorkBuddy 里点一下创建，依赖 App 运行）。
4. **触发时间**（HH:MM）：**由你定**——常几点开机/在线就填几点（默认 09:00 仅建议，无强制）。

- 选「系统计划任务」：Windows `schtasks`（`CatTravel-Start` / `CatTravel-Claim` / `CatTravel-Daily`），macOS·Linux `crontab`（带 `cat-travel` 标记区块，重跑自动清理旧任务）。
- 选「WorkBuddy 定时自动化」：生成 `workbuddy_automation_config.json`（含各自动化的名称 / 时间 / 指令），在 WorkBuddy「自动化」里逐条创建或粘贴即可。

> 自动化/CI 可用环境变量跳过问答：`CAT_TRAVEL_RUN_MODE=manual|scheduled`、`CAT_TRAVEL_CLAIM_MODE=same-day|next-day`、`CAT_TRAVEL_SCHEDULE_BACKEND=system|workbuddy`、`CAT_TRAVEL_TRIGGER=HH:MM`。

## 可选定时任务（手动兜底）

若自动创建失败（无权限 / 非桌面环境），按 `setup` 末尾打印的参考手动创建：
- **当天领取**：2 个任务。旅行任务 `python scripts/cat_travel.py start-only`；领取任务 `python scripts/cat_travel.py claim-only`，时间 = 旅行任务时间 + 4h15m。
- **隔天领取**：1 个任务。每天 `python scripts/cat_travel.py daily`。

### WorkBuddy 定时自动化（依赖 App 在触发时刻运行）

在 `setup` 向导选「WorkBuddy 定时自动化」后会生成 `workbuddy_automation_config.json`，直接粘进 WorkBuddy「自动化」即可；或手动创建 recurring 自动化（时间填**你觉得合适的时刻**，prompt 写对应命令）：`start-only`（派发） / `claim-only`（当天领） / `daily`（隔天领）。

> 注意：WorkBuddy 自动化在电脑**睡眠/休眠时不会执行**（实测连续多天 9:00 零记录）。
> 若你的电脑常睡眠，优先用系统计划任务（方案 B）。

### 方案 B：系统计划任务（睡眠也能唤醒，更稳）

Windows 用 `schtasks` 注册（时间自行替换 `09:00`，隔天模式用 `daily` 一个任务）：

```powershell
schtasks /Create /TN "CatTravel-Start" /SC DAILY /ST 09:00 /TR "cmd /c python scripts/cat_travel.py start-only"
schtasks /Create /TN "CatTravel-Claim" /SC DAILY /ST 13:15 /TR "cmd /c python scripts/cat_travel.py claim-only"
```

Linux/macOS 用 `crontab -e` 加一行（时间自行替换，例如每天 9 点，隔天模式用 `daily`）：

```cron
0 9 * * * cd <脚本目录> && python3 scripts/cat_travel.py start-only >> cat_travel_cron.log 2>&1
15 13 * * * cd <脚本目录> && python3 scripts/cat_travel.py claim-only >> cat_travel_cron.log 2>&1
```

成功后想接微信/邮件通知，自行在脚本外层包一层推送即可（本仓库不含推送实现，避免内置第三方凭证）。

## 安全与隐私

- 令牌仅从**本机登录态**解密、仅在内存中使用，不落盘、不回显、不上传任何第三方
- 脚本不含任何硬编码账号 / uid / 绝对路径，跨用户、跨机器通用（路径用 `os.homedir()` / 环境变量动态解析）
- 请勿用于批量刷分或违反 WorkBuddy 用户协议的用途，使用者自行承担使用风险

## 排障（补充）

- 定时任务「今天没跑」、报 `Session spawn failed: spawn ENAMETOOLONG`、App 未在触发时刻运行等：多为电脑睡眠导致 App 未拉起，或 WorkBuddy 会话启动失败；可改用系统计划任务（见上文方案 B）规避，或重启 WorkBuddy 后重试。
- 令牌失效（401）：打开 WorkBuddy 刷新登录态，下次运行自动重新读取

## License

MIT —— 见 [LICENSE](./LICENSE)
