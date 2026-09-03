#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 成长计划 · 派猫猫旅行自动化
=====================================
流程：
  1. 提取本机 WorkBuddy 登录态（decrypt-token.js）
  2. 调用「派猫猫旅行」接口触发旅行
  3. 旅行结束（到达）后，按所选模式领取积分：
     - 当天领取：旅行最长 4 小时 + 额外 15 分钟缓冲，拆成两个定时任务
       （旅行任务 + 缓冲后领取任务）
     - 隔天领取：每天先领取「昨日」积分，再开始「当日」旅行；
       首次运行因无昨日积分会导致领取失败，已做优雅处理，后续正常运行
  4. 调用「领取积分」接口完成领奖

使用方式：
  python cat_travel.py setup        # 交互式安装：选运行方式（手动/定时）+ 领取模式
  python cat_travel.py run          # 单次手动完整流程：触发旅行 → 等待 → 领取
  python cat_travel.py start-only   # 只触发旅行，不等待（配合定时任务拆分）
  python cat_travel.py claim-only   # 只执行领取（用于电脑重启后补领 / 当天模式）
  python cat_travel.py daily         # 隔天模式每日任务：先领昨日积分，再开始今日旅行
  python cat_travel.py status        # 查看当前旅行状态与已选配置

安装交互说明：
  其他用户下载安装本 skill 后，运行 `python cat_travel.py setup` 即可按提示选择：
    ① 运行方式：单次手动执行 / 配置为自动定时任务
    ② 若选定时：积分领取模式 = 当天领取 / 隔天领取，以及每日触发时间
  脚本会自动创建系统计划任务（Windows 任务计划 / macOS·Linux crontab）。

接口配置：
  请在下方 CONFIG 区域填入实际接口路径、方法和请求体。
  获取方式：在 WorkBuddy 桌面端打开「成长计划」，按 F12 / Ctrl+Shift+I 打开开发者工具，
  点击「派猫猫旅行」按钮，观察 Network 面板中对应的请求；领奖同理。
"""
import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# ============================================================
# 配置区：小主需要按抓包结果填写
# ============================================================
CONFIG = {
    # API 基础域名（成长中心实际域名，2026-08-28 抓包确认）
    "api_base": "https://www.workbuddy.cn",

    # ① 派猫猫旅行 - 状态查询接口（2026-08-28 抓包确认：GET）
    "status_path": "/activity/growth/buddy/travel/status",
    "status_method": "GET",

    # ② 派猫猫旅行 - 触发接口（2026-08-31 抓包确认：POST /activity/growth/buddy/travel/depart）
    #    实测请求头：Accept: application/json, text/plain, */*；Content-Type: application/json；X-Client-Platform: web
    #    实测请求体：{"location_id":1}（location_id=1 → 咖啡馆 coffee，duration_hours=3）
    #    实测返回：{"code":0,"data":{"state":"traveling","record_id":3068718,"arrive_at":...,"depart_at":...}}
    #    注：location_id 为地点编号；当前固定 1（咖啡馆）。不同目的地 duration_hours 不同，
    #        实际旅行时长以 depart 响应的 arrive_at-depart_at 为准，等待逻辑自动适配。
    "start_path": "/activity/growth/buddy/travel/depart",   # ✅ 已确认（2026-08-31）
    "start_method": "POST",
    "start_body": {"location_id": 1},

    # ③ 派猫猫旅行 - 领取积分接口（2026-08-28 抓包确认：真实 POST，body={}）
    #    实测请求头：Accept: application/json, text/plain, */*；Content-Type: application/json；X-Client-Platform: web
    #    实测返回：{"code":0,"msg":"OK","data":{"state":"idle","record_id":2801536,"reward_credit":5,...}}
    "claim_path": "/activity/growth/buddy/travel/claim",    # 已确认（2026-08-28）
    "claim_method": "POST",
    "claim_body": {},                                        # 已确认：请求体为空 {}

    # 旅行等待 / 领取策略（2026-08-31 升级为"基于旅行时长 + 缓冲"动态触发）：
    # 触发条件 = 出发时刻 + 旅行时长 + 缓冲(claim_buffer_seconds)，
    #   旅行时长优先取 depart/status 响应里的 arrive_at - depart_at（服务器权威时间戳），
    #   次取 location.duration_hours，再取顶层 duration_hours，最后回退固定延迟。
    # 这样无论目的地是 1h/2h/3h/4h，都在「到达后 +15min」精确触发领取，不再依赖固定 4.5h。
    "use_polling": False,
    "travel_wait_seconds": 2 * 3600,                        # 旧字段保留，不再主流程使用
    "claim_delay_after_start_seconds": 4 * 3600 + 30 * 60,  # 兜底上限（仅当取不到任何旅行时长时使用 4.5h）
    "claim_buffer_seconds": 15 * 60,                        # 缓冲：旅行时长基础上额外叠加 15 分钟

    # 当天领取模式的最长旅行时长（用于"旅行任务 + 缓冲后领取任务"的固定偏移计算）
    "travel_max_hours": 4,

    # 重试策略
    "max_retries": 3,
    "retry_delay_seconds": 5,

    # 状态文件（用于崩溃恢复 / claim-only / daily）：写入用户级缓存目录，避免污染 skill 目录
    "state_file": os.path.join(os.path.expanduser("~"), ".workbuddy", "cache", "cat-travel", "cat_travel_state.json"),

    # 配置文件（记录用户选的运行方式 / 领取模式 / 触发时间）：同样写缓存目录，不进 skill 仓库
    "config_file": os.path.join(os.path.expanduser("~"), ".workbuddy", "cache", "cat-travel", "cat_travel_config.json"),

    # 日志文件
    "log_file": os.path.join(os.path.expanduser("~"), ".workbuddy", "cache", "cat-travel", "cat_travel.log"),
}

# ============================================================
# 运行时常量
# ============================================================
HERE = os.path.dirname(os.path.abspath(__file__))


def _find_node():
    import shutil
    n = shutil.which("node")
    if n:
        return n
    # WorkBuddy 桌面端自带托管 Node（用户未单独安装 Node / 未加入 PATH 时自动复用）
    base = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions")
    if os.path.isdir(base):
        exe = "node.exe" if os.name == "nt" else "node"
        # 版本目录可能带 -2 等后缀，按版本名倒序取最新可用的
        for ver in sorted(os.listdir(base), reverse=True):
            cand = os.path.join(base, ver, exe)
            if os.path.isfile(cand):
                return cand
    return None


def _find_decrypt_js():
    """按优先级查找 decrypt-token.js，保证 skill 自包含、可跨机分发：
       1) 本 skill 自带副本（scripts/decrypt-token.js）
       2) 兼容：与 workbuddy-checkin 同属 ~/.workbuddy/skills/ 时复用其副本
    """
    cands = [
        os.path.join(HERE, "decrypt-token.js"),
        os.path.normpath(os.path.join(HERE, "..", "..", "workbuddy-checkin", "scripts", "decrypt-token.js")),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


# Node 运行时：环境变量 CAT_TRAVEL_NODE > 系统 PATH 中的 node > 托管 Node（~/.workbuddy 下，跨用户通用）
NODE = (os.environ.get("CAT_TRAVEL_NODE")
        or _find_node()
        or os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions", "22.22.2", "node.exe"))
# 解密脚本：环境变量 CAT_TRAVEL_DECRYPT_JS > 本 skill 自带副本 > 兼容 workbuddy-checkin
DECRYPT_JS = (os.environ.get("CAT_TRAVEL_DECRYPT_JS")
              or _find_decrypt_js())


# ============================================================
# 日志
# ============================================================
def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(CONFIG["log_file"]), exist_ok=True)
    except Exception:
        pass
    try:
        with open(CONFIG["log_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ============================================================
# 登录态提取（复用 workbuddy-checkin skill 的 decrypt-token.js）
# ============================================================
def extract_token():
    if not NODE:
        raise RuntimeError("未找到 Node.js 运行时：请安装 Node.js 并确保在 PATH 中可用，"
                           "或设置环境变量 CAT_TRAVEL_NODE 指向 node 可执行文件。")
    if not DECRYPT_JS or not os.path.exists(DECRYPT_JS):
        raise RuntimeError("未找到 decrypt-token.js：请确认 cat-travel/scripts/decrypt-token.js 存在，"
                           "或设置环境变量 CAT_TRAVEL_DECRYPT_JS 指向该文件。")
    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    try:
        r = subprocess.run(
            [NODE, DECRYPT_JS],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="ignore", env=env, timeout=90,
        )
    except Exception as e:
        raise RuntimeError("调用 decrypt-token.js 失败：%s" % e)

    token = uid = domain = ent = ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("DECRYPT_RESULT:"):
            token = line.split(":", 1)[1].strip()
        elif line.startswith("ACCOUNT_UID:"):
            uid = line.split(":", 1)[1].strip()
        elif line.startswith("AUTH_DOMAIN:"):
            domain = line.split(":", 1)[1].strip()
        elif line.startswith("ENTERPRISE_ID:"):
            ent = line.split(":", 1)[1].strip()

    if not token or token.startswith("ERR"):
        raise RuntimeError("获取令牌失败：%s" % token)

    os.environ["WB_TOKEN"] = token
    os.environ["WB_UID"] = uid
    if domain:
        os.environ["WB_DOMAIN"] = domain
    if ent:
        os.environ["WB_ENT_ID"] = ent
    log("登录态提取成功（uid=%s）" % uid)


# ============================================================
# 成长计划开通状态探测 + 小白引导
# ============================================================
def is_growth_opened():
    """探测当前账号是否已开通「成长计划」（猫猫旅行依赖它）。
    返回 True=已开通，False=未开通 / 无法确认。
    判定强信号：status 接口返回 code=0 且 data 含成长计划字段
    （state 枚举 / daily_limit_reached / reward_credit）。
    401/403 或业务码非 0 → 视为未就绪（需登录或开通）。
    """
    try:
        code, text = api_call(CONFIG["status_path"], CONFIG["status_method"], None)
    except Exception as e:
        log("[探测成长计划] 状态接口异常：%s" % e)
        return False
    try:
        resp = json.loads(text)
    except Exception:
        return False
    if not isinstance(resp, dict):
        return False
    biz = resp.get("code")
    if biz not in (0, None):
        msg = str(resp.get("msg", "")).lower()
        if any(k in msg for k in ("未开通", "未参与", "未加入", "not open",
                                  "not joined", "forbidden", "no permission", "无权", "请先开通")):
            return False
        # 其他业务错误保守视为未就绪，交由开通引导兜底
        return False
    data = resp.get("data")
    if not isinstance(data, dict):
        return False
    if data.get("state") in ("traveling", "arrived", "idle") \
            or "daily_limit_reached" in data or "reward_credit" in data:
        return True
    return False


def ensure_growth_opened():
    """小白引导：未开通成长计划时，自动打开系统默认浏览器到开通页并提示。
    返回 True=已开通；False=未开通（已引导，调用方应退出）。
    开通页 URL：环境变量 CAT_TRAVEL_GROWTH_URL 优先，默认 {api_base}/activity/growth。
    """
    if is_growth_opened():
        return True
    url = (os.environ.get("CAT_TRAVEL_GROWTH_URL")
           or (CONFIG["api_base"] + "/activity/growth"))
    log("=" * 56)
    log("⚠️  当前账号尚未开通「成长计划」，猫猫旅行的派发与领积分都依赖它。")
    log("")
    log("    正在自动打开浏览器到开通页面，请按页面提示完成开通——")
    log("    通常只需点击一次「开通 / 加入」即可，无需任何复杂操作。")
    log("")
    log("    开通完成后，重新运行本脚本即可自动派发旅行 + 领取积分。")
    log("")
    log("    开通页面：%s" % url)
    log("=" * 56)
    try:
        import webbrowser
        webbrowser.open(url, new=2)
        log("✅ 已为你打开浏览器，请切到浏览器窗口完成开通。")
    except Exception:
        log("（无法自动打开浏览器，请手动复制上方链接到浏览器打开）")
    return False


def ensure_ready():
    """开箱即用入口：提取登录态 + 检测成长计划开通；
    未开通则打开浏览器引导并退出（退出码 2，便于定时任务区分）。"""
    extract_token()
    if not ensure_growth_opened():
        sys.exit(2)


# ============================================================
# 通用 HTTP 请求（带重试）
# ============================================================
def build_headers():
    token = os.environ.get("WB_TOKEN", "").strip()
    uid = os.environ.get("WB_UID", "").strip()
    domain = os.environ.get("WB_DOMAIN", "").strip()
    ent_id = os.environ.get("WB_ENT_ID", "").strip()
    if not token or not uid:
        raise RuntimeError("缺少 WB_TOKEN 或 WB_UID")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid,
    }
    if domain:
        headers["X-Domain"] = domain
    if ent_id:
        headers["X-Enterprise-Id"] = ent_id
        headers["X-Tenant-Id"] = ent_id
    return headers


def api_call(path, method="POST", body=None, max_retries=None):
    if max_retries is None:
        max_retries = CONFIG["max_retries"]
    headers = build_headers()
    url = CONFIG["api_base"] + path
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else b"{}"

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=20) as resp:
                code = resp.getcode()
                text = resp.read().decode("utf-8", "ignore")
            return code, text
        except urllib.error.HTTPError as e:
            code = e.code
            text = e.read().decode("utf-8", "ignore")
            if code in (401, 403):
                raise RuntimeError("令牌过期或无权限（HTTP %d）：%s" % (code, text[:200]))
            if code == 429:
                last_err = "请求被限流（HTTP 429）"
            elif 500 <= code < 600:
                last_err = "服务器错误（HTTP %d）：%s" % (code, text[:200])
            else:
                # 4xx 确定性错误（含 400 业务错误、404 路径错误）：不重试，
                # 直接返回交由业务层判断（如「今日已领」应判幂等成功）。
                return code, text
        except Exception as e:
            last_err = str(e)

        if attempt < max_retries:
            wait = CONFIG["retry_delay_seconds"] * attempt
            log("第 %d 次请求失败：%s，%d 秒后重试..." % (attempt, last_err, wait))
            time.sleep(wait)

    raise RuntimeError("请求 %s 在 %d 次重试后仍然失败：%s" % (path, max_retries, last_err))


# ============================================================
# 状态持久化（统一用 current_travel 结构）
# ============================================================
def load_state():
    try:
        with open(CONFIG["state_file"], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(CONFIG["state_file"]), exist_ok=True)
    except Exception:
        pass
    with open(CONFIG["state_file"], "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _date_of(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).date()
    except Exception:
        return None


# ============================================================
# 配置文件（记录用户选的运行方式 / 领取模式 / 触发时间）
# ============================================================
def load_config():
    try:
        with open(CONFIG["config_file"], "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("run_mode", None)       # manual / scheduled
    cfg.setdefault("claim_mode", None)     # same-day / next-day
    cfg.setdefault("schedule_backend", "system")  # system / workbuddy
    cfg.setdefault("scheduled_claim_method", "auto")  # auto / remind / other
    cfg.setdefault("automation_dir", None)  # WorkBuddy 自动化配置存放目录
    cfg.setdefault("trigger_hh", 9)
    cfg.setdefault("trigger_mm", 0)
    return cfg


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG["config_file"]), exist_ok=True)
    except Exception:
        pass
    with open(CONFIG["config_file"], "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ============================================================
# 旅行状态查询 & 动态等待
# ============================================================
def get_travel_status():
    """查询当前旅行状态：GET /activity/growth/buddy/travel/status。"""
    code, text = api_call(CONFIG["status_path"], CONFIG["status_method"], None)
    try:
        resp = json.loads(text)
    except Exception:
        resp = {"_raw": text}
    return code, resp


def wait_for_arrival(poll_interval=60, hard_timeout=3 * 3600):
    """
    轮询 status 接口，直到旅行到达（arrive_at <= server_now）。
    优先用服务器返回的 arrive_at / server_now 计算剩余时间，比本地 sleep 准。
    返回到达时的 data 字典。
    """
    deadline = time.time() + hard_timeout
    while True:
        code, resp = get_travel_status()
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        arrive_at = data.get("arrive_at")
        server_now = data.get("server_now")
        state = data.get("state")
        if arrive_at and server_now is not None:
            remaining = arrive_at - server_now
            if remaining <= 0:
                log("旅行已到达（state=%s），可领取。" % state)
                return data
            log("距离到达还有 %s（state=%s）" % (_fmt_seconds(int(remaining)), state))
            # 分段睡眠：睡到接近到达，但不超过 10 分钟一轮，避免错过
            sleep_t = max(1, min(remaining, poll_interval * 8))
            sleep_t = min(sleep_t, 600)
            time.sleep(sleep_t)
        else:
            log("status 未返回 arrive_at，按兜底间隔轮询...")
            time.sleep(poll_interval)
        if time.time() > deadline:
            raise RuntimeError("等待旅行到达超时（超过 %d 秒上限）。" % hard_timeout)


# ============================================================
# 旅行时长读取 + 领取触发点计算（核心机制）
# ============================================================
def resolve_claim_schedule(data, started_local=None):
    """根据旅行数据解析「领取触发时间」。
    返回 (claim_at_dt, travel_duration_seconds, source_desc)。
    触发条件 = 出发 + 旅行时长 + 缓冲(claim_buffer_seconds=15min)。

    旅行时长数据来源（按优先级，均为服务器返回，最权威）：
      1) depart/status 响应里的 arrive_at - depart_at（两个服务器 Unix 时间戳之差，得精确秒数）
      2) depart 响应嵌套 location.duration_hours（小时，如咖啡馆=3）
      3) 顶层 duration_hours（部分接口直接返回在 data 顶层）
    兜底：以上皆无 → 回退 claim_delay_after_start_seconds（4.5h，已含缓冲），避免空等死锁。
    """
    buffer = CONFIG.get("claim_buffer_seconds", 15 * 60)
    depart_at = data.get("depart_at")
    arrive_at = data.get("arrive_at")
    loc = data.get("location", {}) or {}
    loc_dur_h = loc.get("duration_hours") if isinstance(loc, dict) else None
    top_dur_h = data.get("duration_hours")

    # ① 最权威：arrive_at - depart_at 直接得精确旅行时长
    if arrive_at and depart_at and arrive_at > depart_at:
        travel_dur = arrive_at - depart_at
        claim_at = datetime.fromtimestamp(arrive_at) + timedelta(seconds=buffer)
        return claim_at, travel_dur, "arrive_at - depart_at（服务器时间戳，最权威）"

    # ② 次选：depart_at + duration_hours
    dur_h = loc_dur_h if loc_dur_h not in (None, 0) else top_dur_h
    if depart_at and dur_h not in (None, 0):
        try:
            travel_dur = int(float(dur_h) * 3600)
        except Exception:
            travel_dur = 0
        if travel_dur > 0:
            base = datetime.fromtimestamp(depart_at)
            claim_at = base + timedelta(seconds=travel_dur + buffer)
            return claim_at, travel_dur, "depart_at + duration_hours=%s（%s 小时）" % (dur_h, dur_h)

    # ③ 再次：仅有 duration_hours、无 depart_at → 用本地出发记为基准
    if dur_h not in (None, 0) and started_local:
        try:
            travel_dur = int(float(dur_h) * 3600)
        except Exception:
            travel_dur = 0
        if travel_dur > 0:
            claim_at = started_local + timedelta(seconds=travel_dur + buffer)
            return claim_at, travel_dur, "本地出发时间 + duration_hours=%s（无 depart_at 兜底）" % dur_h

    # ④ 兜底：未读到任何旅行时长 → 固定 4.5h
    base = started_local or datetime.now()
    travel_dur = CONFIG["claim_delay_after_start_seconds"]
    claim_at = base + timedelta(seconds=travel_dur)
    return claim_at, travel_dur, "兜底：固定 %.1f 小时（未读到旅行时长）" % (travel_dur / 3600.0)


def _begin_travel(state):
    """触发一次新旅行，并把本次旅行的时长 / 触发时间写入 state['current_travel']。
    返回更新后的 current_travel 字典。"""
    result = start_travel()
    resp_data = (result.get("response") or {}).get("data", {})
    started_local = datetime.now()
    claim_at, travel_dur, src = resolve_claim_schedule(resp_data, started_local)
    ct = {
        "depart_local": started_local.isoformat(),
        "depart_at": resp_data.get("depart_at"),
        "arrive_at": resp_data.get("arrive_at"),
        "location_id": (resp_data.get("location") or {}).get("id") if isinstance(resp_data.get("location"), dict) else None,
        "location_name": (resp_data.get("location") or {}).get("name") if isinstance(resp_data.get("location"), dict) else None,
        "record_id": resp_data.get("record_id"),
        "travel_duration_seconds": travel_dur,
        "claim_at": claim_at.isoformat(),
        "claim_schedule_source": src,
        "claimed": False,
        "claimed_at": None,
    }
    state["current_travel"] = ct
    save_state(state)
    return ct


# ============================================================
# 业务动作
# ============================================================
def start_travel():
    log(">>> 触发「派猫猫旅行」：%s %s" % (CONFIG["start_method"], CONFIG["start_path"]))
    code, text = api_call(CONFIG["start_path"], CONFIG["start_method"], CONFIG["start_body"])
    log("旅行接口返回 HTTP %d：%s" % (code, text[:500]))
    try:
        resp = json.loads(text)
    except Exception:
        resp = {"_raw": text}

    # 判断是否已经旅行中（幂等场景）
    already = _is_already_traveling(resp)
    if already:
        log("检测到已有进行中的旅行，跳过触发，使用状态文件继续等待/领取逻辑。")
        return {"already": True, "response": resp}

    if not _is_success(resp, code):
        raise RuntimeError("旅行触发未成功：%s" % text[:300])

    log("旅行触发成功。")
    return {"already": False, "response": resp}


def claim_travel(record_id=None):
    # 真实接口请求体为 {}（2026-08-28 抓包确认：record_id 由服务端按当前会话推导，无需客户端传）
    body = dict(CONFIG["claim_body"])
    log(">>> 领取旅行积分：%s %s body=%s" % (CONFIG["claim_method"], CONFIG["claim_path"], body))
    code, text = api_call(CONFIG["claim_path"], CONFIG["claim_method"], body)
    log("领取接口返回 HTTP %d：%s" % (code, text[:500]))
    try:
        resp = json.loads(text)
    except Exception:
        resp = {"_raw": text}

    # 成功：HTTP 200 且业务 code=0
    if code == 200 and _is_success(resp, code):
        log("积分领取成功。")
        return {"already": False, "response": resp}

    # 幂等：HTTP 400 {"code":400,"msg":"no unclaimed travel"} = 当前无可领旅行（今日已领过）
    if code == 400 and _is_already_claimed(resp):
        log("无可领奖的旅行（可能今日已领取）：%s" % (resp.get("msg") if isinstance(resp, dict) else text[:120]))
        return {"already": True, "response": resp}

    # 其余视为失败
    raise RuntimeError("积分领取未成功（HTTP %d）：%s" % (code, text[:300]))


# 工具：根据常见字段判断成功/幂等状态
# 小主可按实际接口返回微调这些函数

def _is_success(resp, http_code):
    if http_code != 200:
        return False
    if isinstance(resp, dict):
        code = resp.get("code")
        if code is not None and code != 0:
            return False
    return True


def _is_already_traveling(resp):
    """接口返回「已有进行中的旅行」时返回 True，避免误报失败。"""
    if not isinstance(resp, dict):
        return False
    code = resp.get("code")
    msg = str(resp.get("msg", "")).lower()
    # 示例：如果业务码为 10001 或消息含「进行中/已派出/已出发」则视为已旅行
    if code == 10001 or "已" in msg or "进行中" in msg or "already" in msg:
        return True
    return False


def _is_already_claimed(resp):
    """接口返回「今日已领取/已领奖/无可领旅行」时返回 True（幂等判定）。"""
    if not isinstance(resp, dict):
        return False
    code = resp.get("code")
    msg = str(resp.get("msg", "")).lower()
    if code == 10001 or "已领取" in msg or "已领奖" in msg or "already" in msg:
        return True
    # 2026-08-28 实测：今日已领后重复领取 → HTTP 400 {"code":400,"msg":"no unclaimed travel"}
    if "no unclaimed" in msg or "unclaimed travel" in msg or "no travel" in msg:
        return True
    return False


def _is_no_unclaimed_error(e):
    """判断异常是否来自「无可领旅行」（如网络层 400 但未被 claim_travel 吞掉）。"""
    s = str(e).lower()
    return ("no unclaimed" in s) or ("unclaimed travel" in s) or ("已领取" in s) or ("无可领" in s)


# ============================================================
# 主流程
# ============================================================
def wait_until(target_time):
    """精确睡到目标时间，期间每秒打印一次剩余时间。"""
    while True:
        now = datetime.now()
        remaining = (target_time - now).total_seconds()
        if remaining <= 0:
            break
        # 每 60 秒或最后 10 秒打印日志
        if remaining <= 10 or int(remaining) % 60 == 0:
            log("距离领取还有 %s" % _fmt_seconds(int(remaining)))
        time.sleep(min(1, remaining))


def _fmt_seconds(s):
    return "%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def _need_start_today(state):
    """判断今天是否还需要触发一次新旅行。
    返回 True=需要触发；False=今天已触发（无论是否已领）。"""
    ct = state.get("current_travel") or {}
    if not ct:
        return True
    dep = _date_of(ct.get("depart_local"))
    today = datetime.now().date()
    return dep != today


def cmd_run():
    """单次手动完整流程：触发旅行（若今天尚未触发）→ 等待到达+缓冲 → 领取。"""
    ensure_ready()
    state = load_state()

    if _need_start_today(state):
        ct = _begin_travel(state)
        log("旅行触发成功（%s），预计 %s 到达。" % (
            ct.get("location_name"), ct["claim_at"]))
    else:
        ct = state["current_travel"]
        if ct.get("claimed"):
            log("今日旅行已完成并领取（%s），无需重复。" % ct.get("claimed_at"))
            return
        log("今日旅行已在进行，直接进入等待/领取。")

    claim_at = datetime.fromisoformat(ct["claim_at"])
    if datetime.now() < claim_at:
        wait_until(claim_at)
    else:
        log("已过触发时间，直接进入领取。")

    res = claim_travel(record_id=ct.get("record_id"))
    ct["claimed"] = True
    ct["claimed_at"] = datetime.now().isoformat()
    save_state(state)
    if res.get("already"):
        log("🎉 当前无可领旅行（可能已领取），流程结束。")
    else:
        log("🎉 派猫猫旅行自动化流程完成。")


def cmd_start_only():
    """只触发旅行（配合定时任务拆分）。若今天已触发则跳过。"""
    ensure_ready()
    state = load_state()
    if not _need_start_today(state):
        ct = state["current_travel"]
        if ct.get("claimed"):
            log("今日旅行已完成并领取，无需重复触发。")
        else:
            log("今日旅行已触发（%s），无需重复。" % ct.get("depart_local"))
        return
    ct = _begin_travel(state)
    log("旅行触发成功，预计 %s 到达；领取任务将按「到达 + 15min 缓冲」自动触发。" % ct["claim_at"])


def cmd_claim_only():
    """只执行领取（当天模式 / 手动补领）。未到触发时间会自动等待。"""
    ensure_ready()
    state = load_state()
    ct = state.get("current_travel") or {}

    if not ct:
        log("状态文件中没有旅行记录，尝试直接调用领取接口（手动/补领场景）。")
        try:
            res = claim_travel()
            if res.get("already"):
                log("当前无可领旅行（可能已领取）。")
            else:
                log("🎉 积分领取完成。")
        except Exception as e:
            log("❌ 直接领取失败：%s" % e)
        return

    if ct.get("claimed"):
        log("当前旅行已领取（%s），无需重复。" % ct.get("claimed_at"))
        return

    claim_at = datetime.fromisoformat(ct["claim_at"])
    now = datetime.now()
    if now < claim_at:
        remaining = (claim_at - now).total_seconds()
        log("尚未到触发时间（还需 %s），自动等待至 %s 后领取。" % (
            _fmt_seconds(int(remaining)), claim_at.strftime("%H:%M:%S")))
        wait_until(claim_at)
    else:
        log("已过触发时间（实际经过 >= 旅行时长 + 缓冲），直接进入领取。")

    try:
        res = claim_travel(record_id=ct.get("record_id"))
    except Exception as e:
        if _is_no_unclaimed_error(e):
            log("无未领取的旅行（可能已手动领取），标记完成。")
            ct["claimed"] = True
            ct["claimed_at"] = datetime.now().isoformat()
            save_state(state)
        else:
            log("❌ 积分领取失败：%s" % e)
        return
    ct["claimed"] = True
    ct["claimed_at"] = datetime.now().isoformat()
    save_state(state)
    if res.get("already"):
        log("🎉 当前无可领旅行（可能已领取）。")
    else:
        log("🎉 积分领取完成。")


def cmd_daily():
    """隔天领取模式每日任务：先领取「昨日」积分，再开始「当日」旅行。
    首次运行：状态文件为空 → 无昨日积分可领（领取步骤被安全跳过），直接进入「开始今日旅行」，
    不会因『无昨日积分』而报错退出，后续每天均可正常运行。
    """
    ensure_ready()
    state = load_state()
    today = datetime.now().date()
    ct = state.get("current_travel") or {}

    # ---------- 步骤 1：领取昨日（或更早未领）的积分 ----------
    if ct and not ct.get("claimed"):
        dep = _date_of(ct.get("depart_local"))
        if dep and dep < today:
            log("检测到 %s 的旅行尚未领取，先领取昨日积分..." % dep)
            try:
                res = claim_travel(record_id=ct.get("record_id"))
                ct["claimed"] = True
                ct["claimed_at"] = datetime.now().isoformat()
                save_state(state)
                if res.get("already"):
                    log("昨日无可领积分（可能已领），标记完成。")
                else:
                    log("✅ 昨日积分领取成功。")
            except Exception as e:
                if _is_no_unclaimed_error(e):
                    # 服务端判定无未领旅行（含首次运行兜底），标记完成，避免无限重试
                    log("无未领取的旅行（首次运行或已领），标记完成，继续今日旅行。")
                    ct["claimed"] = True
                    ct["claimed_at"] = datetime.now().isoformat()
                    save_state(state)
                else:
                    log("⚠️ 昨日积分领取失败：%s（不影响今日旅行，明日再试）" % e)
        elif dep == today:
            log("今日旅行仍在进行，明日再领取，跳过领取步骤。")
        else:
            log("历史旅行记录无需处理。")
    else:
        log("无历史旅行记录（首次运行），无需领取昨日积分。")

    # ---------- 步骤 2：开始今日旅行 ----------
    ct2 = state.get("current_travel") or {}
    if not _need_start_today(state):
        log("今日旅行已触发（%s），无需重复。" % ct2.get("depart_local"))
        return
    new_ct = _begin_travel(state)
    log("今日旅行已触发（%s），预计 %s 到达；明日由 daily 任务自动领取昨日积分。" % (
        new_ct.get("location_name"), new_ct["claim_at"]))


def cmd_status():
    cfg = load_config()
    log("--- 已选配置 ---")
    log("运行方式：%s" % ("手动" if cfg.get("run_mode") == "manual" else ("自动定时" if cfg.get("run_mode") == "scheduled" else "未配置（请运行 setup）")))
    if cfg.get("run_mode") == "scheduled":
        method_map = {"auto": "到点自动领取", "remind": "到点提醒手动领取", "other": "其他方式"}
        log("定时领取方式：%s" % method_map.get(cfg.get("scheduled_claim_method", "auto"), cfg.get("scheduled_claim_method", "auto")))
        log("领取模式：%s" % ("当天领取" if cfg.get("claim_mode") == "same-day" else "隔天领取"))
        log("定时载体：%s" % ("系统计划任务" if cfg.get("schedule_backend") == "system" else "WorkBuddy 自动化"))
        if cfg.get("schedule_backend") == "workbuddy" and cfg.get("automation_dir"):
            log("自动化配置目录：%s" % cfg["automation_dir"])
        log("触发时间：%02d:%02d" % (cfg.get("trigger_hh", 9), cfg.get("trigger_mm", 0)))

    has_token = True
    try:
        extract_token()
    except Exception as e:
        has_token = False
        log("提取登录态失败：%s（仅显示本地状态）" % e)
    state = load_state()
    ct = state.get("current_travel") or {}
    if ct:
        log("--- 本地旅行记录 ---")
        log("出发时间：%s，目的地：%s" % (ct.get("depart_local", "无"), ct.get("location_name", "无")))
        log("已领奖：%s" % ("是" if ct.get("claimed") else "否"))
        if ct.get("claimed_at"):
            log("领取时间：%s" % ct.get("claimed_at"))
        log("领取触发时间：%s" % ct.get("claim_at", "无"))
    else:
        log("本地暂无旅行记录。")
    if not has_token:
        return
    # 已登录但成长计划未开通 → 自动打开浏览器引导开通
    if not ensure_growth_opened():
        sys.exit(2)
    log("--- 实时旅行状态 ---")
    try:
        code, resp = get_travel_status()
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        log("state: %s" % data.get("state"))
        loc = data.get("location", {}) or {}
        log("目的地：%s（%s 小时）" % (loc.get("name"), data.get("duration_hours")))
        log("奖励积分：%s" % data.get("reward_credit"))
        log("daily_limit_reached：%s" % data.get("daily_limit_reached"))
        arrive = data.get("arrive_at")
        now = data.get("server_now")
        if arrive and now is not None:
            rem = arrive - now
            if rem > 0:
                log("距离到达还有：%s" % _fmt_seconds(int(rem)))
            else:
                log("已到达，可运行 claim-only 领取。")
    except Exception as e:
        log("查询实时状态失败：%s" % e)


# ============================================================
# 安装交互（setup）：选择运行方式 + 领取模式，并创建系统定时任务
# ============================================================
def _parse_time(s):
    s = (s or "").strip()
    if ":" not in s:
        raise ValueError("时间格式应为 HH:MM，例如 09:00")
    hh, mm = s.split(":", 1)
    hh = int(hh); mm = int(mm)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("小时 0-23，分钟 0-59")
    return hh, mm


def _prompt_choice(prompt, choices, default_key=None):
    """交互式单选。choices = [(key, desc), ...]。非交互（无 TTY）时回退到 default_key。"""
    print("")
    print(prompt)
    for k, d in choices:
        mark = " (默认)" if k == default_key else ""
        print("  %s) %s%s" % (k, d, mark))
    if not sys.stdin.isatty():
        print("  （当前为非交互环境，使用默认：%s）" % default_key)
        return default_key
    while True:
        inp = input("请选择 [%s]: " % (default_key or "")).strip()
        if not inp and default_key:
            return default_key
        if inp in [k for k, _ in choices]:
            return inp
        print("  输入无效，请重新选择。")


def _prompt_automation_dir(cfg):
    """使用 WorkBuddy 自动化时，提示用户选择配置/状态文件的存放目录。

    默认把 workbuddy_automation_config.json 放在脚本所在目录，但 skill 目录不便于长期管理
    （升级、迁移或清理时容易误删）。因此主动询问用户希望保存到哪个磁盘或目录，
    等待确认后再继续生成配置。
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_cache = os.path.join(os.path.expanduser("~"), ".workbuddy", "cache", "cat-travel")
    # 环境变量 / 已保存配置优先级最高，方便非交互/自动化安装
    env_dir = os.environ.get("CAT_TRAVEL_AUTOMATION_DIR")
    if env_dir:
        d = os.path.abspath(os.path.expanduser(env_dir))
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass
    current = cfg.get("automation_dir") or default_cache

    print("")
    print("💾 文件存放位置")
    print("   当前 WorkBuddy 自动化配置默认准备保存在脚本所在目录：")
    print("     %s" % script_dir)
    print("   这个目录不便于长期管理（skill 升级/迁移时容易丢失配置），建议更换。")

    if not sys.stdin.isatty():
        print("   （当前为非交互环境，使用默认目录：%s）" % current)
        try:
            os.makedirs(current, exist_ok=True)
        except Exception:
            pass
        return current

    while True:
        print("")
        print("请选择文件存放位置：")
        print("  1) 使用默认缓存目录（推荐，升级 skill 不丢失）：%s" % default_cache)
        print("  2) 继续使用脚本所在目录：%s" % script_dir)
        print("  3) 自定义目录（请输入完整路径，如 D:\\CatTravel 或 /home/xxx/cat-travel）")
        inp = input("请选择 [1]: ").strip()
        if not inp or inp == "1":
            d = default_cache
        elif inp == "2":
            d = script_dir
        elif inp == "3":
            d = input("   请输入完整目录路径：").strip()
            if not d:
                print("   路径为空，请重新选择。")
                continue
            d = os.path.abspath(os.path.expanduser(d))
        else:
            print("   输入无效，请重新选择。")
            continue

        try:
            os.makedirs(d, exist_ok=True)
            # 简单校验目录可写
            test_file = os.path.join(d, ".write_test")
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test_file)
            print("   ✅ 已确认使用目录：%s" % d)
            return d
        except Exception as e:
            print("   无法使用该目录（%s），请重新选择。" % e)


def cmd_setup():
    print("=" * 60)
    print("  派猫猫旅行 · 安装向导")
    print("=" * 60)
    print("本向导帮你一步步选择运行方式，并（可选）创建定时任务。")
    print("随时可重跑本向导修改配置；配置保存在用户缓存目录，不写入 skill 仓库。")
    print("")
    print("💡 建议：先选「单次手动执行」跑通一次、确认能领到积分后，")
    print("   再回来重跑本向导配置定时任务，体验更稳、心里更踏实。")

    cfg = load_config()

    # ---------- ① 运行方式 ----------
    run_mode = os.environ.get("CAT_TRAVEL_RUN_MODE")
    if run_mode not in ("manual", "scheduled"):
        k = _prompt_choice(
            "① 你希望如何运行猫猫旅行？",
            [("1", "单次手动执行（推荐先试：随时自己运行，不创建定时任务）"),
             ("2", "配置为自动定时任务（每天自动跑，需要继续选领取模式 / 载体 / 时间）")],
            default_key="1")
        run_mode = {"1": "manual", "2": "scheduled"}.get(k, "manual")
    cfg["run_mode"] = run_mode

    # ---------- ② 定时领取方式（仅自动定时需要；先生成领取策略，再决定任务结构） ----------
    scheduled_claim_method = os.environ.get("CAT_TRAVEL_SCHEDULED_CLAIM_METHOD")
    if run_mode == "scheduled" and scheduled_claim_method not in ("auto", "remind", "other"):
        k = _prompt_choice(
            "② 每日定时任务如何领取积分？",
            [("1", "到点自动领取（推荐）：到达触发时间后脚本自动判断并领取，无需你动手"),
             ("2", "到点提醒手动领取：仅发送提醒通知，由你手动运行 claim-only / daily 领取"),
             ("3", "其他方式：先跳过领取自动化，后续由你自定义")],
            default_key="1")
        scheduled_claim_method = {"1": "auto", "2": "remind", "3": "other"}.get(k, "auto")
    cfg["scheduled_claim_method"] = scheduled_claim_method or "auto"

    # ---------- ③ 积分领取模式（手动 / 定时都要知道，run / daily 用得到） ----------
    claim_mode = os.environ.get("CAT_TRAVEL_CLAIM_MODE")
    if claim_mode not in ("same-day", "next-day"):
        k = _prompt_choice(
            "③ 积分领取模式？",
            [("1", "当天领取：一次 run 完成「派发 + 等待 + 领积分」（最长 4h+15min 缓冲）"),
             ("2", "隔天领取：用 daily，每天先领「昨日」积分再派「当日」旅行（首次无昨日会自动跳过）")],
            default_key="1")
        claim_mode = {"1": "same-day", "2": "next-day"}.get(k, "same-day")
    cfg["claim_mode"] = claim_mode

    if run_mode == "manual":
        save_config(cfg)
        print("")
        print("✅ 已保存为「手动模式」。之后你可以随时运行：")
        if claim_mode == "same-day":
            print("     python scripts/cat_travel.py run        # 一次完成 派发+等待+领积分")
            print("  或拆成两步：")
            print("     python scripts/cat_travel.py start-only  # 先派发")
            print("     python scripts/cat_travel.py claim-only  # 到了再领")
        else:
            print("     python scripts/cat_travel.py daily       # 先领昨日积分，再派今日旅行")
        print("（想改成自动定时，重跑 `python scripts/cat_travel.py setup` 即可。）")
        return

    # ---------- ④ 定时任务载体 ----------
    backend = os.environ.get("CAT_TRAVEL_SCHEDULE_BACKEND")
    if backend not in ("system", "workbuddy"):
        k = _prompt_choice(
            "④ 定时任务由谁负责调度？",
            [("1", "系统计划任务（Windows 任务计划 / macOS·Linux crontab）——脚本直接创建，电脑睡眠也能跑，更稳"),
             ("2", "WorkBuddy 定时自动化——生成配置后你在 WorkBuddy 里点一下创建；依赖 WorkBuddy 在触发时刻运行（睡眠不跑）")],
            default_key="1")
        backend = {"1": "system", "2": "workbuddy"}.get(k, "system")
    cfg["schedule_backend"] = backend

    # ---------- ⑤ 触发时间（用户自己定） ----------
    trigger = os.environ.get("CAT_TRAVEL_TRIGGER")
    if not trigger and sys.stdin.isatty():
        print("")
        print("⑤ 每日触发时间：你常几点开机 / 在线就填几点（例如常 9 点开电脑填 09:00）。")
        print("   时间完全由你定，没有强制要求；下面只是默认值建议。")
    if not trigger:
        if sys.stdin.isatty():
            trigger = input("   触发时间（HH:MM，默认 09:00）：").strip()
    if not trigger:
        trigger = "09:00"
    try:
        hh, mm = _parse_time(trigger)
    except Exception as e:
        print("时间格式有误（%s），改用默认 09:00。" % e)
        hh, mm = 9, 0
    cfg["trigger_hh"] = hh
    cfg["trigger_mm"] = mm
    save_config(cfg)

    # ---------- ⑥ 执行 ----------
    print("")
    if backend == "system":
        print("正在创建系统定时任务（%s / 触发时间 %02d:%02d）..." % (
            "当天领取" if claim_mode == "same-day" else "隔天领取", hh, mm))
        ok = create_scheduled_tasks(cfg)
        print("")
        if ok:
            print("✅ 系统定时任务已创建。配置已保存到：")
            print("   %s" % CONFIG["config_file"])
        else:
            print("⚠️ 自动创建未完全成功（可能缺少权限或非桌面环境）。")
            print("   你可手动按下面的命令创建；或把报错反馈给作者。")
        _print_manual_schedule_hint(cfg)
    else:
        # WorkBuddy 自动化：先确认文件存放目录，再生成配置
        automation_dir = _prompt_automation_dir(cfg)
        cfg["automation_dir"] = automation_dir
        save_config(cfg)
        print("正在生成 WorkBuddy 定时自动化配置（%s / 触发时间 %02d:%02d）..." % (
            "当天领取" if claim_mode == "same-day" else "隔天领取", hh, mm))
        _emit_workbuddy_automation(cfg)
        print("")
        print("✅ WorkBuddy 自动化配置已生成。配置已保存到：")
        print("   %s" % CONFIG["config_file"])
        print("   自动化 JSON 存放目录：%s" % cfg.get("automation_dir", "脚本所在目录"))

    print("")
    print("💡 最后建议：先手动跑一次确认能领到积分，再放心依赖定时任务。")
    if claim_mode == "same-day":
        print("     python scripts/cat_travel.py run")
    else:
        print("     python scripts/cat_travel.py daily")


def create_scheduled_tasks(cfg):
    """按配置创建系统定时任务。成功返回 True，任一失败返回 False。"""
    if cfg.get("schedule_backend") == "workbuddy":
        # WorkBuddy 载体不在这里建系统任务，由 _emit_workbuddy_automation 处理
        return False
    if os.name == "nt":
        return _win_create_tasks(cfg)
    return _nix_create_tasks(cfg)


def _emit_workbuddy_automation(cfg):
    """生成 WorkBuddy 定时自动化配置（JSON 文件 + 控制台指引）。

    说明：脚本运行在普通 Python 环境，无法直接创建 WorkBuddy 自动化，
    因此输出一份可直接粘贴 / 逐条创建的配置，由用户在 WorkBuddy 内完成创建。
    """
    hh = cfg["trigger_hh"]; mm = cfg["trigger_mm"]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    abs_script = os.path.join(script_dir, "cat_travel.py")
    automation_dir = cfg.get("automation_dir") or script_dir
    claim_method = cfg.get("scheduled_claim_method", "auto")

    def rrule(h, m):
        return "FREQ=DAILY;BYHOUR=%d;BYMINUTE=%d" % (h, m)

    def make(name, h, m, prompt):
        return {
            "name": name,
            "start_hh": h,
            "start_mm": m,
            "scheduleType": "recurring",
            "rrule": rrule(h, m),
            "cwds": [automation_dir],
            "status": "ACTIVE",
            "prompt": prompt,
        }

    if cfg["claim_mode"] == "same-day":
        total = hh * 60 + mm + int(CONFIG["travel_max_hours"]) * 60 + int(CONFIG["claim_buffer_seconds"] // 60)
        ch = (total // 60) % 24
        cm = total % 60
        start_auto = make("派猫猫旅行-触发@%02d:%02d" % (hh, mm), hh, mm,
                          "运行 `python \"%s\" start-only` 派发猫猫旅行并写入到达时间。若提示未开通成长计划，则打开浏览器引导开通；完成后汇报：派发成功 / 已在进行中无需重复 / 令牌失效需刷新登录。" % abs_script)
        if claim_method == "remind":
            autos = [
                start_auto,
                make("派猫猫旅行-领奖提醒@%02d:%02d" % (ch, cm), ch, cm,
                     "提醒小主：猫猫旅行预计已到达，请手动运行 `python \"%s\" claim-only` 领取积分。" % abs_script),
            ]
        elif claim_method == "other":
            autos = [start_auto]
        else:
            autos = [
                start_auto,
                make("派猫猫旅行-领奖@%02d:%02d" % (ch, cm), ch, cm,
                     "运行 `python \"%s\" claim-only` 按旅行时长自动判断到点后领取积分（幂等）。汇报：领取成功得几分 / 无可领（今日已领或还没派发） / 令牌失效需刷新登录。" % abs_script),
            ]
    else:
        if claim_method == "remind":
            autos = [
                make("派猫猫旅行-每日提醒@%02d:%02d" % (hh, mm), hh, mm,
                     "提醒小主：请手动运行 `python \"%s\" daily` 完成昨日积分领取和今日旅行派发。" % abs_script),
            ]
        elif claim_method == "other":
            autos = []
        else:
            autos = [
                make("派猫猫旅行-每日@%02d:%02d" % (hh, mm), hh, mm,
                     "运行 `python \"%s\" daily`：先领昨日积分（无昨日则安全跳过），再派发今日旅行。首次运行无昨日积分属正常。汇报：领取结果 / 已派发 / 令牌失效需刷新登录。" % abs_script),
            ]

    out = os.path.join(automation_dir, "workbuddy_automation_config.json")
    if autos:
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(autos, f, ensure_ascii=False, indent=2)
            log("WorkBuddy 自动化配置已生成：%s" % out)
        except Exception as e:
            log("写入 WB 配置失败：%s" % e)
        print("  ✅ 配置已写入：%s" % out)
        print("")
        print("  在 WorkBuddy 中创建自动化的方法（逐条创建）：")
        for a in autos:
            print("   · 名称：%s" % a["name"])
            print("     时间：每天 %02d:%02d（rrule: %s）" % (a["start_hh"], a["start_mm"], a["rrule"]))
            print("     指令：%s" % a["prompt"])
            print("")
        print("  提示：把上面 JSON 文件内容直接粘进 WorkBuddy 自动化即可创建；")
        print("        或按上面逐条在 WorkBuddy「自动化」里手动创建（名称 / 时间 / 指令照抄）。")
    else:
        print("  ℹ️ 你选择了「其他方式」，未生成领取相关自动化。")
        print("     请稍后自行在 WorkBuddy「自动化」里补充领取任务。")
        print("     派发命令参考：python \"%s\" start-only" % abs_script)
        if cfg["claim_mode"] == "same-day":
            print("     领取命令参考：python \"%s\" claim-only" % abs_script)
        else:
            print("     每日任务参考：python \"%s\" daily" % abs_script)


def _reminder_cmd(message):
    """返回一个可在系统定时任务中使用的提醒命令。

    Windows 用 PowerShell 弹窗；macOS 用 osascript 通知；Linux 优先 notify-send，
    不可用则写入缓存目录的 reminder.log。如系统通知不可用，至少保证有文本记录。
    """
    if os.name == "nt":
        return ('powershell -Command "Add-Type -AssemblyName System.Windows.Forms; '
                '[System.Windows.Forms.MessageBox]::Show(\'%s\', \'猫猫旅行提醒\')"' % message)
    elif sys.platform == "darwin":
        return 'osascript -e \'display notification "%s" with title "猫猫旅行提醒"\'' % message
    else:
        log_path = os.path.join(os.path.expanduser("~"), ".workbuddy", "cache", "cat-travel", "reminder.log")
        return ('(command -v notify-send >/dev/null 2>&1 && notify-send "猫猫旅行提醒" "%s") || '
                'echo "[%s] %s" >> "%s"' % (message, "$(date +%%Y-%%m-%%d\\ %%H:%%M:%%S)", message, log_path))


def _win_create_tasks(cfg):
    """Windows：用 schtasks 创建。先清理旧的 CatTravel-* 任务避免残留。"""
    names = ["CatTravel-Start", "CatTravel-Claim", "CatTravel-Daily", "CatTravelDaily"]
    for n in names:
        subprocess.run(["schtasks", "/Delete", "/TN", n, "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    py = sys.executable
    script = os.path.abspath(__file__)
    cmdline = subprocess.list2cmdline([py, script])
    hh = cfg["trigger_hh"]; mm = cfg["trigger_mm"]
    claim_method = cfg.get("scheduled_claim_method", "auto")
    ok = True
    if cfg["claim_mode"] == "same-day":
        start_time = "%02d:%02d" % (hh, mm)
        total = hh * 60 + mm + int(CONFIG["travel_max_hours"]) * 60 + int(CONFIG["claim_buffer_seconds"] // 60)
        ch = (total // 60) % 24
        cm = total % 60
        claim_time = "%02d:%02d" % (ch, cm)
        ok &= _win_make("CatTravel-Start", start_time, cmdline + " start-only")
        if claim_method == "remind":
            ok &= _win_make("CatTravel-ClaimReminder", claim_time,
                            _reminder_cmd("猫猫旅行已到达，请手动运行 claim-only 领取积分"))
        elif claim_method != "other":
            ok &= _win_make("CatTravel-Claim", claim_time, cmdline + " claim-only")
    else:
        if claim_method == "remind":
            ok &= _win_make("CatTravel-DailyReminder", "%02d:%02d" % (hh, mm),
                            _reminder_cmd("请手动运行 daily 领取昨日积分并派发今日旅行"))
        elif claim_method != "other":
            ok &= _win_make("CatTravel-Daily", "%02d:%02d" % (hh, mm), cmdline + " daily")
    return ok


def _win_make(name, t, cmdline):
    r = subprocess.run(["schtasks", "/Create", "/TN", name, "/TR", cmdline,
                        "/SC", "DAILY", "/ST", t, "/F"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        log("计划任务已创建：%s（%s）" % (name, t))
        print("  ✅ %s  @ %s" % (name, t))
        return True
    log("计划任务创建失败：%s -> %s" % (name, (r.stderr or r.stdout or "").strip()[:200]))
    print("  ❌ %s 创建失败：%s" % (name, (r.stderr or r.stdout or "").strip()[:160]))
    return False


def _nix_create_tasks(cfg):
    """macOS / Linux：用 crontab 创建（先移除旧 cat-travel 区块避免重复）。"""
    try:
        import shlex
    except Exception:
        shlex = None
    py = sys.executable
    script = os.path.abspath(__file__)
    if shlex:
        quoted = "%s %s" % (shlex.quote(py), shlex.quote(script))
    else:
        quoted = "%s %s" % (py, script)
    hh = cfg["trigger_hh"]; mm = cfg["trigger_mm"]
    claim_method = cfg.get("scheduled_claim_method", "auto")
    if cfg["claim_mode"] == "same-day":
        start_line = "%d %d * * * %s start-only" % (mm, hh, quoted)
        total = hh * 60 + mm + int(CONFIG["travel_max_hours"]) * 60 + int(CONFIG["claim_buffer_seconds"] // 60)
        ch = (total // 60) % 24
        cm = total % 60
        if claim_method == "remind":
            claim_line = "%d %d * * * %s" % (cm, ch, _reminder_cmd("猫猫旅行已到达，请手动运行 claim-only 领取积分"))
        elif claim_method == "other":
            claim_line = None
        else:
            claim_line = "%d %d * * * %s claim-only" % (cm, ch, quoted)
        lines = [start_line] + ([claim_line] if claim_line else [])
    else:
        if claim_method == "remind":
            lines = ["%d %d * * * %s" % (mm, hh, _reminder_cmd("请手动运行 daily 领取昨日积分并派发今日旅行"))]
        elif claim_method == "other":
            lines = []
        else:
            lines = ["%d %d * * * %s daily" % (mm, hh, quoted)]

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout.splitlines()
    except Exception:
        existing = []
    filtered = [l for l in existing if "cat-travel" not in l and "CatTravel" not in l]
    block = ["# >>> cat-travel >>>"] + lines + ["# <<< cat-travel <<<"]
    newlines = filtered + block
    r = subprocess.run(["crontab", "-"], input="\n".join(newlines) + "\n",
                       capture_output=True, text=True)
    if r.returncode == 0:
        for ln in lines:
            log("crontab 已写入：%s" % ln)
            print("  ✅ %s" % ln)
        return True
    log("crontab 写入失败：%s" % (r.stderr or "").strip()[:200])
    print("  ❌ crontab 写入失败：%s" % (r.stderr or "").strip()[:160])
    return False


def _print_manual_schedule_hint(cfg):
    """打印手动创建定时任务的参考命令（当自动创建失败时使用）。"""
    hh = cfg["trigger_hh"]; mm = cfg["trigger_mm"]
    claim_method = cfg.get("scheduled_claim_method", "auto")
    print("")
    print("手动创建参考（时间 %02d:%02d）：" % (hh, mm))
    if cfg["claim_mode"] == "same-day":
        total = hh * 60 + mm + int(CONFIG["travel_max_hours"]) * 60 + int(CONFIG["claim_buffer_seconds"] // 60)
        ch = (total // 60) % 24
        cm = total % 60
        print("  当天领取 需至少 1 个任务：")
        print("    · 旅行任务   %02d:%02d  →  python scripts/cat_travel.py start-only" % (hh, mm))
        if claim_method == "auto":
            print("    · 领取任务   %02d:%02d  →  python scripts/cat_travel.py claim-only" % (ch, cm))
        elif claim_method == "remind":
            print("    · 领取提醒   %02d:%02d  →  请手动运行 python scripts/cat_travel.py claim-only" % (ch, cm))
        else:
            print("    · 领取任务：你选择了「其他方式」，请自行补充领取命令。")
    else:
        print("  隔天领取：")
        if claim_method == "auto":
            print("    · 每日任务   %02d:%02d  →  python scripts/cat_travel.py daily" % (hh, mm))
        elif claim_method == "remind":
            print("    · 每日提醒   %02d:%02d  →  请手动运行 python scripts/cat_travel.py daily" % (hh, mm))
        else:
            print("    · 每日任务：你选择了「其他方式」，请自行补充 daily 命令。")


def main():
    usage = ("用法：python cat_travel.py <命令>\n"
             "  setup       交互式安装：选运行方式 + 领取模式，自动建定时任务\n"
             "  run         单次手动完整流程：触发旅行 → 等待 → 领取\n"
             "  start-only  只触发旅行（配合定时任务拆分）\n"
             "  claim-only  只领取（当天模式 / 手动补领，到点自动等待）\n"
             "  daily       隔天模式每日任务：先领昨日积分再开始今日旅行\n"
             "  status      查看旅行状态与已选配置")
    if len(sys.argv) < 2:
        print(usage)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    log("=" * 50)
    log("派猫猫旅行自动化启动，命令：%s" % cmd)

    try:
        if cmd in ("setup", "install"):
            cmd_setup()
        elif cmd in ("run", "start-and-claim"):
            cmd_run()
        elif cmd in ("start-only", "start"):
            cmd_start_only()
        elif cmd in ("claim-only", "claim"):
            cmd_claim_only()
        elif cmd in ("daily", "next-day"):
            cmd_daily()
        elif cmd == "status":
            cmd_status()
        else:
            print(usage)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        log("❌ 流程异常：%s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
