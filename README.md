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

## 可选定时任务（是否启用、什么时间由你决定）

本 skill **默认不写入任何定时任务**。每日自动领取是可选增强，由你自行决定要不要、几点跑。

### 方案 A：WorkBuddy 定时自动化（最简单，但依赖 App 在触发时刻运行）

在 WorkBuddy 创建两个 recurring 自动化（时间填**你觉得合适的时刻**）：

```json
{
  "name": "派猫猫旅行-触发",
  "scheduleType": "recurring",
  "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
  "cwds": ["<脚本所在目录>"],
  "status": "ACTIVE",
  "prompt": "运行 python scripts/cat_travel.py start-only（派发旅行并写入到达时间）。"
}
```
```json
{
  "name": "派猫猫旅行-领奖",
  "scheduleType": "recurring",
  "rrule": "FREQ=DAILY;BYHOUR=13;BYMINUTE=30",
  "cwds": ["<脚本所在目录>"],
  "status": "ACTIVE",
  "prompt": "运行 python scripts/cat_travel.py claim-only（按旅行时长自动判断到点后领取，幂等）。"
}
```

> 注意：WorkBuddy 自动化在电脑**睡眠/休眠时不会执行**（实测连续多天 9:00 零记录）。
> 若你的电脑常睡眠，优先用方案 B。

### 方案 B：系统计划任务（睡眠也能唤醒，更稳）

Windows 用 `schtasks` 注册（时间自行替换 `06:00`）：

```powershell
schtasks /Create /TN "CatTravelDaily" /SC DAILY /ST 06:00 /TR "cmd /c cd /d <脚本目录> && python scripts/cat_travel.py run"
```

Linux/macOS 用 `crontab -e` 加一行（时间自行替换，例如每天 6 点）：

```cron
0 6 * * * cd <脚本目录> && python3 scripts/cat_travel.py run >> cat_travel_cron.log 2>&1
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
