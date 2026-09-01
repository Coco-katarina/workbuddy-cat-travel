#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 成长计划 · 派猫猫旅行自动化
=====================================
流程：
  1. 提取本机 WorkBuddy 登录态（decrypt-token.js）
  2. 调用「派猫猫旅行」接口触发旅行
  3. 出发后按「旅行时长 + 15min 缓冲」自动触发领取（旅行时长优先取 depart/status 响应的 arrive_at-depart_at，无则取 duration_hours）
  4. 调用「领取积分」接口完成领奖

使用方式：
  python cat_travel.py run          # 完整流程：触发旅行 → 等待 → 领取
  python cat_travel.py start-only   # 只触发旅行，不等待（配合定时任务拆分）
  python cat_travel.py claim-only   # 只执行领取（用于电脑重启后补领）
  python cat_travel.py status       # 查看当前旅行状态

定时任务建议：
  - 方式 A：用 Windows 任务计划程序 / automation 每天固定时间运行：
      python cat_travel.py run
    脚本按「出发 + 旅行时长 + 15min 缓冲」自动等待到触发点再领取，需保持电脑开机至触发点。
  - 方式 B（适合 WorkBuddy automation，不能长时 sleep）：拆成两个 automation / 计划任务：
    · 每天 09:00 运行 python cat_travel.py start-only（触发并记下旅行时长 / 触发时间）
    · 每天固定或更晚运行 python cat_travel.py claim-only
      claim-only 会用状态文件里的「旅行时长 + 15min」自行判断是否到点，
      未到点则自动等待，到点则立即领取，无需硬编码固定间隔。

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

    # 重试策略
    "max_retries": 3,
    "retry_delay_seconds": 5,

    # 状态文件（用于崩溃恢复 / claim-only）：写入用户级缓存目录，避免污染 skill 目录
    "state_file": os.path.join(os.path.expanduser("~"), ".workbuddy", "cache", "cat-travel", "cat_travel_state.json"),

    # 日志文件
    "log_file": os.path.join(os.path.expanduser("~"), ".workbuddy", "cache", "cat-travel", "cat_travel.log"),
}

# ============================================================
# 运行时常量
# ============================================================
HERE = os.path.dirname(os.path.abspath(__file__))


def _find_node():
    import shutil
    return shutil.which("node")


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
# 状态持久化
# ============================================================
def load_state():
    try:
        with open(CONFIG["state_file"], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(CONFIG["state_file"], "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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


def persist_travel_schedule(state, data, started_local=None):
    """把本次旅行的时长 / 触发时间写入状态文件，供 claim-only / 崩溃恢复复用。"""
    claim_at, travel_dur, src = resolve_claim_schedule(data, started_local)
    state["depart_at"] = data.get("depart_at")
    state["arrive_at"] = data.get("arrive_at")
    state["travel_duration_seconds"] = travel_dur
    state["claim_at"] = claim_at.isoformat()
    state["claim_schedule_source"] = src
    loc = data.get("location", {}) or {}
    state["location_id"] = loc.get("id") if isinstance(loc, dict) else None
    state["location_name"] = loc.get("name") if isinstance(loc, dict) else None
    state["reward_credit"] = data.get("reward_credit", 0)
    save_state(state)
    return claim_at, travel_dur, src


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


def cmd_run():
    extract_token()
    state = load_state()

    # 步骤 1：触发旅行（或复用 / 接管已有进行中的旅行）
    if state.get("travel_started_at") and not state.get("claimed"):
        # 已有未完成的旅行记录，查询实时状态继续
        log("发现未完成的旅行记录（%s），查询实时状态继续。" % state["travel_started_at"])
        try:
            _, resp = get_travel_status()
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
        except Exception:
            data = (state.get("start_response") or {}).get("data", {})
    else:
        try:
            _, resp = get_travel_status()
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
            if data.get("state") == "traveling":
                log("检测到当前已在旅行中，直接接管状态。")
                state["travel_started_at"] = datetime.now().isoformat()
                state["start_response"] = resp
                state["claimed"] = False
            else:
                result = start_travel()
                state["travel_started_at"] = datetime.now().isoformat()
                state["start_response"] = result.get("response")
                state["claimed"] = False
        except Exception:
            result = start_travel()
            state["travel_started_at"] = datetime.now().isoformat()
            state["start_response"] = result.get("response")
            state["claimed"] = False
        data = (state.get("start_response") or {}).get("data", {})

    # 步骤 2：解析「领取触发时间」= 出发 + 旅行时长 + 缓冲(15min)
    # 旅行时长数据源见 resolve_claim_schedule（优先 depart/status 的 arrive_at - depart_at）。
    started_local = None
    try:
        started_local = datetime.fromisoformat(state["travel_started_at"])
    except Exception:
        pass
    claim_at, travel_dur, src = persist_travel_schedule(state, data, started_local)
    log("领取触发计划：旅行时长=%s，缓冲=%dmin，触发时间=%s（来源：%s）" % (
        _fmt_seconds(travel_dur), CONFIG.get("claim_buffer_seconds", 900) // 60,
        claim_at.strftime("%Y-%m-%d %H:%M:%S"), src))

    if datetime.now() < claim_at:
        wait_until(claim_at)
    else:
        log("已过触发时间，直接进入领取。")

    # 领取前再查一次 status，顺带刷新 record_id
    try:
        _, resp = get_travel_status()
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        record_id = data.get("record_id") or state.get("record_id")
    except Exception:
        record_id = state.get("record_id")
    if record_id:
        state["record_id"] = record_id
        save_state(state)

    # 步骤 3：领取
    claim_result = claim_travel(record_id=record_id)
    state["claimed"] = True
    state["claimed_at"] = datetime.now().isoformat()
    state["claim_response"] = claim_result.get("response")
    save_state(state)
    log("🎉 派猫猫旅行自动化流程完成。")


def cmd_start_only():
    extract_token()
    state = load_state()
    if state.get("travel_started_at") and not state.get("claimed"):
        log("已有进行中的旅行记录，跳过重复触发。")
        return
    # 先查 status，若已在旅行中则直接接管（例如今天已手动派发）
    try:
        _, resp = get_travel_status()
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        if data.get("state") == "traveling":
            log("检测到当前已在旅行中，直接记录状态（record_id=%s）。" % data.get("record_id"))
            state["travel_started_at"] = datetime.now().isoformat()
            state["start_response"] = resp
            state["claimed"] = False
            claim_at, _, _ = persist_travel_schedule(state, data, datetime.now())
            log("已记录旅行时长 / 触发时间：%s。" % claim_at.strftime("%Y-%m-%d %H:%M:%S"))
            return
    except Exception as e:
        log("查询 status 失败，尝试直接触发：%s" % e)
    result = start_travel()
    state["travel_started_at"] = datetime.now().isoformat()
    state["start_response"] = result.get("response")
    state["claimed"] = False
    data = (result.get("response") or {}).get("data", {})
    claim_at, _, _ = persist_travel_schedule(state, data, datetime.now())
    log("旅行触发成功，将在 %s 由 claim-only 自动触发领取。" % claim_at.strftime("%Y-%m-%d %H:%M:%S"))


def cmd_claim_only():
    extract_token()
    state = load_state()
    record_id = state.get("record_id")
    if not state.get("travel_started_at"):
        log("状态文件中没有旅行记录，将直接调用领取接口（record_id=%s）。" % record_id)

    # 解析触发计划：优先用状态文件里的 depart_at/arrive_at，否则查实时 status
    data = {}
    try:
        _, resp = get_travel_status()
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
    except Exception:
        pass
    merged = dict(data)
    if state.get("depart_at"):
        merged["depart_at"] = state["depart_at"]
    if state.get("arrive_at"):
        merged["arrive_at"] = state["arrive_at"]
    if state.get("start_response"):
        sd = (state["start_response"] or {}).get("data", {})
        if sd.get("depart_at") and not merged.get("depart_at"):
            merged["depart_at"] = sd["depart_at"]
        if sd.get("arrive_at") and not merged.get("arrive_at"):
            merged["arrive_at"] = sd["arrive_at"]

    started_local = None
    try:
        started_local = datetime.fromisoformat(state["travel_started_at"])
    except Exception:
        pass

    claim_at, travel_dur, src = resolve_claim_schedule(merged, started_local)
    log("领取触发计划：旅行时长=%s，缓冲=%dmin，触发时间=%s（来源：%s）" % (
        _fmt_seconds(travel_dur), CONFIG.get("claim_buffer_seconds", 900) // 60,
        claim_at.strftime("%Y-%m-%d %H:%M:%S"), src))

    # 触发判断：实际经过时间 < 旅行时长 + 缓冲 → 自动等待；否则领取
    now = datetime.now()
    if now < claim_at:
        remaining = (claim_at - now).total_seconds()
        log("尚未到触发时间（还需 %s），自动等待至 %s 后领取。" % (
            _fmt_seconds(int(remaining)), claim_at.strftime("%H:%M:%S")))
        wait_until(claim_at)
    else:
        log("已过触发时间（实际经过 >= 旅行时长 + 缓冲），直接进入领取。")

    claim_result = claim_travel(record_id=record_id)
    state["claimed"] = True
    state["claimed_at"] = datetime.now().isoformat()
    state["claim_response"] = claim_result.get("response")
    save_state(state)
    log("🎉 积分领取完成。")


def cmd_status():
    has_token = True
    try:
        extract_token()
    except Exception as e:
        has_token = False
        log("提取登录态失败：%s（仅显示本地状态）" % e)
    state = load_state()
    if state:
        log("本地记录 - 旅行启动：%s，已领奖：%s，record_id：%s" % (
            state.get("travel_started_at", "无"),
            "是" if state.get("claimed") else "否",
            state.get("record_id", "无")))
    if not has_token:
        return
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


def main():
    usage = "用法：python cat_travel.py run | claim-only | status"
    if len(sys.argv) < 2:
        print(usage)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    log("=" * 50)
    log("派猫猫旅行自动化启动，命令：%s" % cmd)

    try:
        if cmd in ("run", "start-and-claim"):
            cmd_run()
        elif cmd in ("start-only", "start"):
            cmd_start_only()
        elif cmd in ("claim-only", "claim"):
            cmd_claim_only()
        elif cmd == "status":
            cmd_status()
        else:
            print(usage)
            sys.exit(1)
    except Exception as e:
        log("❌ 流程异常：%s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
