#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy「Buddy 加油站」云端每日自动化（GitHub Actions 版）。

单次运行依序完成：
  1. 每日签到（Buddy 加油站领积分）
  2. 派猫猫旅行（先领取已到家的旅行积分，再判断是否可派新一趟；当天已派过则跳过）
  3. 将「签到结果」与「猫猫状态」分节推送到 飞书 / 微信(WxPusher) / 微信(pushplus)

设计约束（对应云端场景）：
  - 零第三方依赖（仅 Python 标准库），GitHub ubuntu-latest runner 直接可跑；
  - 凭证一律从环境变量读取（Actions 由 secrets 注入），不明文写入仓库、不入日志；
  - 日志与摘要中所有敏感字段（accessToken 等）一律脱敏；
  - 猫猫部分任何失败都不影响签到结论：签到成功即整体成功（退出码 0）。

接口契约：全部经 WorkBuddy 桌面端 5.5.6 安装包（app.asar）与 Web 端
GrowthCenter 页面 chunk 反向核实，并用真实账号逐一实测（见 docs/api-notes.md）。
注意：网上流传的 /billing/meter/checkin-status 等旧接口名不适用，
实际桌面端点均带 /v2 前缀。
"""
import argparse
import base64
import datetime
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.1.1"
USER_AGENT = f"workbuddy-buddy-automation/{VERSION} (+https://github.com/)"

# 北京时间（UTC+8）。GitHub runner 系统时区是 UTC，直接用本地时间会把标题
# 打成"前一天 22:xx"（北京时间 06:xx = UTC 前一天 22:xx），故一律固定时区。
BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))

# ---------------------------------------------------------------- 接口定义 --
# 端点：桌面端 prod endpoint（app.asar 中 DEV_ENV_ENDPOINTS.prod 实测确认）
ENDPOINT = os.environ.get("WB_ENDPOINT", "https://copilot.tencent.com").rstrip("/")

CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"   # 查询签到状态
CHECKIN_CLAIM_PATH = "/v2/billing/meter/daily-checkin"              # 执行签到
RESOURCE_SUMMARY_PATH = "/billing/meter/get-user-resource-summary"  # 剩余积分

BUDDY_INFO_PATH = "/activity/growth/buddy/info"          # 猫猫信息
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"   # 旅行地点配置
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"   # 旅行状态
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"   # 派猫出发 {location_id}
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"     # 领到达奖励
BUDDY_FIRST_PATH = "/activity/growth/buddy/first"             # 领养第一只猫猫
BUDDY_AGREEMENT_GET_PATH = "/activity/growth/buddy/agreement"  # 查询协议签署状态
BUDDY_AGREEMENT_SIGN_PATH = "/activity/growth/buddy/agreement"  # 签署协议 {agree:true}

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
WXPUSHER_URL = "https://wxpusher.zjiecode.com/api/send/message"
PUSHPLUS_URL = "https://www.pushplus.plus/send"

# 需要脱敏的字段名（大小写不敏感）
SENSITIVE_KEYS = {"accessToken", "access_token", "refreshToken", "refresh_token",
                  "idToken", "id_token", "token", "appToken", "secret"}


# ------------------------------------------------------------------ 工具函数 --
def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def mask(text):
    """日志脱敏：超长串只保留首尾各 4 位。"""
    if not isinstance(text, str):
        return text
    if len(text) <= 12:
        return "***"
    return text[:4] + "***" + text[-4:]


def token_expiry(token):
    """从 JWT 解析到期时间。返回 (剩余天数, 到期时间字符串)；解析不出返回 (None, None)。

    WorkBuddy 的登录令牌是标准 JWT（iss = Keycloak realm），payload 里带 exp，
    因此可以精确算出还剩几天，在到期前主动提醒，而不是等它失效了才知道。
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None, None
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return None, None
        days = (exp - time.time()) / 86400.0
        expire_at = (datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
                     + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
        return days, expire_at
    except Exception:  # noqa: BLE001
        return None, None


def sanitize(obj):
    """递归脱敏任意 JSON 结构中的敏感字段。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SENSITIVE_KEYS and isinstance(v, str) and v:
                out[k] = mask(v)
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    return obj


class ApiError(Exception):
    def __init__(self, message, http_code=None, biz_code=None):
        super().__init__(message)
        self.http_code = http_code
        self.biz_code = biz_code


def build_opener():
    handlers = []
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def http_json(opener, method, path, body=None, headers=None, timeout=20, retries=3):
    """发起请求并解析 JSON。返回 (http_status, parsed_json)。失败抛 ApiError。"""
    url = ENDPOINT + path
    data = json.dumps(body if body is not None else {}).encode("utf-8")
    hdrs = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                raise ApiError(f"响应不是合法 JSON: {raw[:200]}")
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            msg = (parsed or {}).get("msg") if isinstance(parsed, dict) else None
            raise ApiError(msg or f"HTTP {e.code}", http_code=e.code,
                           biz_code=(parsed or {}).get("code") if isinstance(parsed, dict) else None)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries:
                log(f"网络错误（第 {attempt}/{retries} 次重试）: {e}")
                time.sleep(2 * attempt)
    raise ApiError(f"网络错误（重试 {retries} 次后仍失败）: {last}")


def biz_ok(resp):
    """后端 envelope 是否成功（code == 0）。"""
    return isinstance(resp, dict) and resp.get("code") == 0


def bj_now():
    """北京时间（runner 无论在哪个时区，推送标题的日期一律以北京时间为准）。"""
    return datetime.datetime.now(BJ_TZ)


def now_str():
    return bj_now().strftime("%Y-%m-%d %H:%M")


def bj_date(offset_days=0):
    """北京时区的日期字符串（offset_days=-1 即昨天），供签到历史/状态文件使用。"""
    return (bj_now() + datetime.timedelta(days=offset_days)).strftime("%Y-%m-%d")


# --------------------------------------------------- 昨日收获（状态文件） --
# 猫猫积分没有历史查询接口，因此每次运行把当日收获写入仓库内 state/daily.json
# （经 GitHub Contents API，用运行自带的 GITHUB_TOKEN，本地模式自动跳过），
# 次日运行读出即为"昨天的积分"。任何失败都只记日志，绝不影响主流程。
GITHUB_API = "https://api.github.com"
STATE_FILE_PATH = os.environ.get("STATE_FILE_PATH", "state/daily.json")
STATE_KEEP_DAYS = 14


def state_target():
    """返回 (repo, branch, token)；本地模式（缺 GITHUB_TOKEN/GITHUB_REPOSITORY）返回 None。"""
    repo = os.environ.get("STATE_REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN") or ""
    branch = os.environ.get("STATE_BRANCH") or os.environ.get("GITHUB_REF_NAME") or "main"
    if repo and token:
        return repo, branch, token
    return None


def state_load():
    """读取状态文件。返回 (days dict, sha 或 None)；读取失败返回 ({}, None)。"""
    tgt = state_target()
    if not tgt:
        return {}, None
    repo, branch, token = tgt
    url = f"{GITHUB_API}/repos/{repo}/contents/{STATE_FILE_PATH}?ref={branch}"
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    })
    try:
        with build_opener().open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        raw = data.get("content") or ""
        if data.get("encoding") == "base64":
            days = json.loads(base64.b64decode(raw).decode("utf-8"))
        else:
            days = json.loads(raw)
        if not isinstance(days, dict):
            return {}, None
        return days, data.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}, None   # 首次运行，还没有状态文件
        log(f"状态文件读取失败 HTTP {e.code}（不影响主流程）")
        return {}, None
    except Exception as e:  # noqa: BLE001
        log(f"状态文件读取失败（不影响主流程）: {e}")
        return {}, None


def state_record(day_date, checkin_credit, cat_credit, remaining=None):
    """把当日收获写入状态文件（best-effort）。
    remaining = 当日签到后查询到的剩余积分（即当日结束时的余额）；
    次日运行时读出来即「昨日剩余积分」。"""
    tgt = state_target()
    if not tgt:
        return
    repo, branch, token = tgt
    days, sha = state_load()
    rec = days.setdefault("days", {})
    # remaining 为签到后查询到的当前余额，次日运行时读出来即"昨日剩余积分"。
    rec[day_date] = {"checkin_credit": checkin_credit or 0,
                     "cat_credit": cat_credit or 0,
                     "remaining": remaining}
    days["days"] = dict(sorted(rec.items())[-STATE_KEEP_DAYS:])   # 只留最近 N 天
    days["updated_at"] = bj_now().isoformat()
    body = {
        "message": f"chore: state {day_date} (auto)",
        "branch": branch,
        "content": base64.b64encode(
            json.dumps(days, ensure_ascii=False).encode("utf-8")).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    url = f"{GITHUB_API}/repos/{repo}/contents/{STATE_FILE_PATH}"
    req = urllib.request.Request(url, method="PUT", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }, data=json.dumps(body).encode("utf-8"))
    try:
        with build_opener().open(req, timeout=20) as resp:
            resp.read()
        log(f"状态文件已写入: {day_date} 签到{checkin_credit or 0} 猫猫{cat_credit or 0} 剩余{remaining}")
    except Exception as e:  # noqa: BLE001
        log(f"状态文件写入失败（不影响主流程）: {e}")


# ---------------------------------------------------------------- 业务：签到 --
def do_checkin(opener, headers):
    """签到：先查状态，未签则领。返回结果 dict。"""
    result = {"action": None, "success": False, "streak_days": None,
              "today_credit": None, "yesterday_credit": None, "message": "",
              "raw_status": None, "raw_claim": None}
    _, status = http_json(opener, "POST", CHECKIN_STATUS_PATH, {}, headers)
    if not biz_ok(status):
        raise ApiError(f"查询签到状态失败: code={status.get('code')} msg={status.get('msg')}",
                       biz_code=status.get("code"))
    sdata = status.get("data") or {}
    result["raw_status"] = sanitize(sdata)
    today = bool(sdata.get("today_checked_in"))
    result["streak_days"] = sdata.get("streak_days")
    result["today_credit"] = sdata.get("today_credit")
    # 昨天是否签过：接口自带历史签到日期表，昨天在其中即昨天已领
    if bj_date(-1) in (sdata.get("checkin_dates") or []):
        result["yesterday_credit"] = sdata.get("daily_credit")
    if today:
        result["action"] = "skip"
        result["success"] = True
        result["message"] = "今日已签到（幂等跳过）"
        log(f"签到状态: 今日已签, 连续 {result['streak_days']} 天")
        return result
    _, claim = http_json(opener, "POST", CHECKIN_CLAIM_PATH, {}, headers)
    result["raw_claim"] = sanitize(claim)
    if not biz_ok(claim):
        # 已签也可能返回非 0，二次确认
        _, status2 = http_json(opener, "POST", CHECKIN_STATUS_PATH, {}, headers)
        if biz_ok(status2) and (status2.get("data") or {}).get("today_checked_in"):
            result["action"] = "skip"
            result["success"] = True
            result["message"] = "服务端确认今日已签到（二次确认）"
            return result
        raise ApiError(f"签到失败: code={claim.get('code')} msg={claim.get('msg')}",
                       biz_code=claim.get("code"))
    cdata = claim.get("data") or {}
    result["action"] = "claim"
    result["success"] = True
    result["today_credit"] = cdata.get("credit", result["today_credit"])
    result["streak_days"] = cdata.get("streak_days", result["streak_days"])
    result["message"] = f"签到成功，获得 {result['today_credit']} 积分"
    log(f"签到成功: +{result['today_credit']} 积分, 连续 {result['streak_days']} 天")
    return result


def fetch_remaining(opener, headers):
    """查询剩余积分（best-effort）。返回 (摘要字符串, 原始脱敏 data)。"""
    try:
        _, resp = http_json(opener, "POST", RESOURCE_SUMMARY_PATH, {}, headers)
        if not biz_ok(resp):
            return None, None
        pkgs = ((resp.get("data") or {}).get("packages")
                or (resp.get("data") or {}).get("Packages") or [])
        parts = []
        for idx, p in enumerate(pkgs):
            if not isinstance(p, dict):
                continue
            # 字段名以真实响应为准：PackageCode / subscriptionPackageCode / code
            code = (p.get("PackageCode") or p.get("subscriptionPackageCode")
                    or p.get("code") or "")
            # TCACA_ 开头是内部套餐编码，无可读性，按顺序给友好名
            if not code or code.startswith("TCACA_"):
                code = "总结分" if idx == 0 else "附加额度"
            remain = p.get("CycleRemainCapacity") or p.get("cycleRemain")
            total = p.get("CycleTotalCapacity") or p.get("cycleTotal")
            if remain is None and total is None:
                continue
            parts.append(f"{code} {remain}/{total}" if total is not None else f"{code} {remain}")
        return ("；".join(parts) if parts else None), sanitize(resp.get("data"))
    except Exception as e:  # noqa: BLE001
        log(f"剩余积分查询失败（不影响主流程）: {e}")
        return None, None


# ---------------------------------------------------------------- 业务：派猫 --
def get_buddy(opener, headers):
    """查询当前猫猫。返回 (buddy_dict 或 None, 原始脱敏 data)。"""
    _, resp = http_json(opener, "GET", BUDDY_INFO_PATH, None, headers)
    if not biz_ok(resp):
        return None, sanitize(resp.get("data"))
    return (resp.get("data") or {}).get("buddy"), sanitize(resp.get("data"))


def travel_status(opener, headers):
    _, resp = http_json(opener, "GET", TRAVEL_STATUS_PATH, None, headers)
    if not biz_ok(resp):
        raise ApiError(f"查询旅行状态失败: code={resp.get('code')} msg={resp.get('msg')}")
    return resp.get("data") or {}


def travel_locations(opener, headers):
    _, resp = http_json(opener, "GET", TRAVEL_CONFIG_PATH, None, headers)
    if not biz_ok(resp):
        raise ApiError(f"查询旅行配置失败: code={resp.get('code')} msg={resp.get('msg')}")
    return (resp.get("data") or {}).get("locations") or []


def do_cat_flow(opener, headers, adopt=False, location_id=None):
    """派猫流程（对主人规格的实现）：
    1. 领取已到家的旅行积分（state == arrived 时；'no unclaimed travel' 视为无可领）；
    2. 判断是否可派新一趟：idle 且当日未派过 → depart；已派过 → 跳过；
    3. 任何失败只记录，不向上抛（由调用方决定整体结论）。
    """
    out = {
        "success": True,               # 猫猫部分是否按预期走完（无异常）
        "buddy_name": None,
        "claim": None,                 # {"reward_credit": n, "letter": "..."}
        "depart": None,                # {"location": "咖啡馆", "location_id": 1}
        "state": None,
        "daily_limit_reached": None,
        "notes": [],
        "error": None,
        "raw": [],
    }

    def note(msg):
        out["notes"].append(msg)
        log(f"[派猫] {msg}")

    # 0) 猫猫信息
    try:
        buddy, raw_info = get_buddy(opener, headers)
        out["raw"].append({"api": "buddy/info", "resp": raw_info})
    except ApiError as e:
        out["success"] = False
        out["error"] = f"查询猫猫信息失败: {e}"
        note(out["error"])
        return out

    if not buddy:
        # 自动领养（默认开启；ADOPT_BUDDY=0 关闭）。与 Web 端流程一致：
        # 先签协议（agreement agree=true），再领养第一只猫猫（buddy/first）。
        if str(adopt) != "0":
            try:
                _, agr = http_json(opener, "GET", BUDDY_AGREEMENT_GET_PATH, None, headers)
                out["raw"].append({"api": "buddy/agreement", "resp": sanitize(agr)})
                agreed = bool((agr.get("data") or {}).get("agreed")
                              or (agr.get("data") or {}).get("has_agreed"))
                if not agreed:
                    _, sign = http_json(opener, "POST", BUDDY_AGREEMENT_SIGN_PATH,
                                        {"agree": True}, headers)
                    out["raw"].append({"api": "buddy/agreement#sign", "resp": sanitize(sign)})
                    if biz_ok(sign):
                        note("已签署猫猫协议")
                    else:
                        note(f"协议签署失败: {sign.get('msg')}")
                _, resp = http_json(opener, "POST", BUDDY_FIRST_PATH, {}, headers)
                out["raw"].append({"api": "buddy/first", "resp": sanitize(resp)})
                if biz_ok(resp):
                    note("已自动领养第一只猫猫")
                    buddy, _ = get_buddy(opener, headers)
                else:
                    note(f"自动领养失败: {resp.get('msg')}")
            except ApiError as e:
                note(f"自动领养失败: {e}")
        if not buddy:
            out["success"] = True  # 未领养不是错误，是待办
            note("尚未领养猫猫（已开启 ADOPT_BUDDY 自动领养；或到成长中心页面手动领养）")
            return out

    out["buddy_name"] = buddy.get("name") or buddy.get("nickname") or "猫猫"

    # 1) 先领取已到家的旅行积分
    try:
        st = travel_status(opener, headers)
        out["state"] = st.get("state")
        out["daily_limit_reached"] = bool(st.get("daily_limit_reached"))
        out["raw"].append({"api": "travel/status", "resp": sanitize(st)})
        if st.get("state") == "arrived":
            try:
                _, claim = http_json(opener, "POST", TRAVEL_CLAIM_PATH, {}, headers)
                out["raw"].append({"api": "travel/claim", "resp": sanitize(claim)})
                if biz_ok(claim):
                    cdata = claim.get("data") or {}
                    out["claim"] = {"reward_credit": cdata.get("reward_credit"),
                                    "letter": cdata.get("letter")}
                    note(f"猫猫已到家，领取旅行积分 {cdata.get('reward_credit')} 分")
                else:
                    msg = str(claim.get("msg") or "")
                    if "no unclaimed" in msg or "not arrived" in msg:
                        note("没有可领取的旅行积分")
                    else:
                        raise ApiError(msg or f"code={claim.get('code')}",
                                       biz_code=claim.get("code"))
            except ApiError as e:
                out["success"] = False
                out["error"] = f"领取旅行积分失败: {e}"
                note(out["error"])
        elif st.get("state") == "traveling":
            loc = (st.get("location") or {}).get("name") or "外地"
            note(f"猫猫正在「{loc}」旅行中，暂不需要操作")
        else:
            note("猫猫在家（idle）")
    except ApiError as e:
        out["success"] = False
        out["error"] = f"查询旅行状态失败: {e}"
        note(out["error"])
        return out

    # 2) 再判断是否可派新一趟（claim 后状态可能从 arrived 变 idle，需重新查）
    try:
        st2 = travel_status(opener, headers)
        out["raw"].append({"api": "travel/status#2", "resp": sanitize(st2)})
        state = st2.get("state")
        daily_limit = bool(st2.get("daily_limit_reached"))
        out["state"] = state
        out["daily_limit_reached"] = daily_limit

        if state == "traveling":
            loc = (st2.get("location") or {}).get("name") or "外地"
            note(f"猫猫旅行中（{loc}），今日不再派新")
            return out
        if daily_limit:
            note("今天已经派过猫猫了，跳过（每日限一次）")
            return out
        if state != "idle":
            note(f"当前状态 {state}，不派新")
            return out

        locations = travel_locations(opener, headers)
        out["raw"].append({"api": "travel/config",
                           "resp": {"locations": sanitize(locations)}})
        if not locations:
            note("没有可派的旅行地点")
            return out

        # 选点：优先指定 TRAVEL_LOCATION_ID，否则随机
        chosen = None
        if location_id:
            for loc in locations:
                if str(loc.get("id")) == str(location_id):
                    chosen = loc
                    break
            if chosen is None:
                note(f"指定地点 id={location_id} 不存在，改为随机")
        if chosen is None:
            chosen = random.choice(locations)

        _, depart = http_json(opener, "POST", TRAVEL_DEPART_PATH,
                              {"location_id": chosen.get("id")}, headers)
        out["raw"].append({"api": "travel/depart", "resp": sanitize(depart)})
        if biz_ok(depart):
            out["depart"] = {"location": chosen.get("name"),
                             "location_id": chosen.get("id")}
            note(f"已派猫猫前往「{chosen.get('name')}」旅行")
        else:
            msg = str(depart.get("msg") or "")
            http_code = depart.get("http_code")
            if "daily limit" in msg or "already traveling" in msg:
                note("猫猫今天累啦，明天再来（服务端确认已派过/旅行中）")
            elif "no active buddy" in msg:
                note("没有可派出的猫猫（未领养）")
            elif "location not available" in msg:
                # 换个地点再试一次
                alt = next((l for l in locations if l is not chosen), None)
                if alt is not None:
                    _, depart2 = http_json(opener, "POST", TRAVEL_DEPART_PATH,
                                           {"location_id": alt.get("id")}, headers)
                    out["raw"].append({"api": "travel/depart#2", "resp": sanitize(depart2)})
                    if biz_ok(depart2):
                        out["depart"] = {"location": alt.get("name"),
                                         "location_id": alt.get("id")}
                        note(f"已改派猫猫前往「{alt.get('name')}」旅行")
                    else:
                        out["success"] = False
                        out["error"] = f"派猫失败: {depart2.get('msg')}"
                        note(out["error"])
                else:
                    out["success"] = False
                    out["error"] = f"派猫失败: {msg}"
                    note(out["error"])
            else:
                out["success"] = False
                out["error"] = f"派猫失败: {msg or http_code}"
                note(out["error"])
    except ApiError as e:
        out["success"] = False
        out["error"] = f"派猫流程异常: {e}"
        note(out["error"])
    return out


# ---------------------------------------------------------------- 推送通道 --
def push_feishu(webhook, title, yesterday_lines, checkin_lines, cat_lines):
    """飞书自定义机器人：interactive 卡片，昨日收获/签到/猫猫分节展示。"""
    def section(text):
        return {"tag": "div", "text": {"tag": "lark_md", "content": text}}
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "turquoise",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                section("**📊 昨日收获**\n" + "\n".join(yesterday_lines)),
                {"tag": "hr"},
                section("**📅 签到结果**\n" + "\n".join(checkin_lines)),
                {"tag": "hr"},
                section("**🐱 猫猫状态**\n" + "\n".join(cat_lines)),
            ],
        },
    }
    body = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = build_opener()
    with opener.open(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "replace")
    data = json.loads(raw)
    if data.get("code") not in (0, None) and data.get("StatusCode") != 0:
        raise ApiError(f"飞书推送失败: {raw[:200]}")
    return True


def push_wxpusher(app_token, uids, title, html):
    if isinstance(uids, str):
        uid_list = [u.strip() for u in uids.replace("，", ",").replace("；", ";")
                    .replace(";", ",").split(",") if u.strip()]
    else:
        uid_list = [str(u).strip() for u in uids if str(u).strip()]
    if not uid_list:
        raise ApiError("WxPusher 未配置接收 UID")
    body = json.dumps({
        "appToken": app_token,
        "content": html,
        "summary": (title or "")[:100],
        "uids": uid_list,
        "contentType": 2,   # HTML
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WXPUSHER_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = build_opener()
    with opener.open(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "replace")
    data = json.loads(raw)
    if data.get("code") != 1000:
        raise ApiError(f"WxPusher 推送失败 code={data.get('code')} msg={data.get('msg')}")
    return True


def push_pushplus(token, title, html):
    body = json.dumps({
        "token": token, "title": title, "content": html, "template": "html",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(PUSHPLUS_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = build_opener()
    with opener.open(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "replace")
    data = json.loads(raw)
    if data.get("code") not in (200, 0):
        raise ApiError(f"pushplus 推送失败 code={data.get('code')} msg={data.get('msg')}")
    return True


def build_report(checkin, remaining, cat, yesterday=None, y_remaining=None):
    """生成 (标题, 昨日行, 签到行, 猫猫行, html)。
    yesterday=(昨日签到积分, 昨日猫猫积分)；y_remaining=昨日结束时的剩余积分（余额）。"""
    title = f"Buddy加油站 · {now_str()}"
    # ---- 昨日收获 ----
    y_ck, y_cat = (yesterday if isinstance(yesterday, tuple) else (None, None))
    yl = []
    if y_ck is not None:
        yl.append(f"📅 签到：+{y_ck} 积分")
    if y_cat is not None:
        yl.append(f"🐱 猫猫：+{y_cat} 积分")
    if y_ck is not None and y_cat is not None:
        yl.append(f"🧮 合计：+{(y_ck or 0) + (y_cat or 0)} 积分")
    if y_remaining is not None:
        yl.append(f"💰 昨日剩余积分：{y_remaining}")
    if not yl:
        yl.append("（暂无昨日记录，明天起自动补齐）")

    if checkin["action"] == "claim":
        ck = [f"✅ 签到成功：+{checkin['today_credit']} 积分",
              f"🔗 连续签到：{checkin['streak_days']} 天"]
    elif checkin["action"] == "skip":
        ck = ["♻️ 今日已签到（跳过，不重复领取）",
              f"🔗 连续签到：{checkin['streak_days']} 天"]
    else:
        ck = [f"❌ 签到失败：{checkin.get('message') or '未知错误'}"]
    if remaining:
        ck.append(f"💰 剩余积分：{remaining}")

    catl = []
    if cat.get("buddy_name"):
        catl.append(f"🐾 猫猫：{cat['buddy_name']}")
    if cat.get("claim"):
        catl.append(f"🎁 旅行归来领取：+{cat['claim']['reward_credit']} 积分")
    if cat.get("depart"):
        catl.append(f"✈️ 已派新一趟：「{cat['depart']['location']}」")
    if cat.get("state") == "traveling" and not cat.get("depart"):
        catl.append("🧳 状态：旅行中")
    # 备注行去重：已被结构化行表达的信息不再重复
    dup_markers = []
    if cat.get("claim"):
        dup_markers.append("旅行积分")
    if cat.get("depart"):
        dup_markers += ["已派猫猫前往", "已改派猫猫前往"]
    if cat.get("state") == "traveling" and not cat.get("depart"):
        dup_markers.append("旅行中")
    for n in cat.get("notes") or []:
        if any(mk in n for mk in dup_markers):
            continue
        catl.append(f"• {n}")
    if not catl:
        catl.append("（无猫猫状态信息）")

    html = (
        f"<h3>{title}</h3>"
        f"<h4>📊 昨日收获</h4><p>{'<br/>'.join(yl)}</p>"
        f"<h4>📅 签到结果</h4><p>{'<br/>'.join(ck)}</p>"
        f"<h4>🐱 猫猫状态</h4><p>{'<br/>'.join(catl)}</p>"
    )
    return title, yl, ck, catl, html


# -------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser(description="WorkBuddy Buddy加油站：签到+派猫+推送")
    ap.add_argument("--dry-run", action="store_true",
                    help="只查询不执行签到/派猫/推送")
    ap.add_argument("--config",
                    help="配置文件路径（JSON，本地运行时用；字段与 Secret 同名小写）")
    ap.add_argument("--out", default="artifacts/summary.json",
                    help="脱敏运行摘要输出路径（供 upload-artifact）")
    args = ap.parse_args()

    # 凭证解析优先级：环境变量（GitHub Secrets） > --config 配置文件（本地运行）
    file_cfg = {}
    if args.config and os.path.exists(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        except Exception as e:  # noqa: BLE001
            log(f"读取配置失败: {e}")
    elif args.config:
        log(f"配置文件不存在: {args.config}")

    token = os.environ.get("WB_ACCESS_TOKEN") or file_cfg.get("access_token") or ""
    user_id = os.environ.get("WB_USER_ID") or file_cfg.get("user_id") or ""
    ent = os.environ.get("WB_ENTERPRISE_ID") or file_cfg.get("enterprise_id") or ""
    dom = os.environ.get("WB_DOMAIN") or file_cfg.get("domain") or ""
    location_id = (os.environ.get("TRAVEL_LOCATION_ID")
                   or file_cfg.get("travel_location_id") or None)
    adopt = os.environ.get("ADOPT_BUDDY") or str(file_cfg.get("adopt_buddy", "1"))
    feishu = os.environ.get("FEISHU_WEBHOOK") or file_cfg.get("feishu_webhook") or ""
    wx_token = os.environ.get("WXPUSHER_TOKEN") or file_cfg.get("wxpusher_token") or ""
    wx_uids = os.environ.get("WXPUSHER_UID") or file_cfg.get("wxpusher_uid") or ""
    pp_token = os.environ.get("PUSHPLUS_TOKEN") or file_cfg.get("pushplus_token") or ""

    if not token:
        log("错误: 缺少 WB_ACCESS_TOKEN（GitHub Secrets 注入或 --config 配置文件）")
        return 2
    if not user_id:
        log("警告: 缺少 WB_USER_ID，部分接口可能鉴权失败")

    headers = {"Authorization": f"Bearer {token}"}
    if user_id:
        headers["X-User-Id"] = user_id
    if ent:
        headers["X-Enterprise-Id"] = ent
        headers["X-Tenant-Id"] = ent
    if dom:
        headers["X-Domain"] = dom

    log(f"端点: {ENDPOINT} | 账号: {user_id or '(未提供)'} | 令牌: {mask(token)}")

    # 令牌剩余有效期（JWT 自带 exp，可精确算出还剩几天）
    days_left, expire_at = token_expiry(token)
    if days_left is not None:
        log(f"令牌到期: {expire_at}（剩余 {days_left:.1f} 天）")
    opener = build_opener()

    summary = {"time": bj_now().isoformat(), "version": VERSION,
               "endpoint": ENDPOINT, "user_id": user_id, "checkin": None,
               "remaining": None, "cat": None, "push": [], "overall": None,
               "token_days_left": (round(days_left, 1) if days_left is not None else None),
               "token_expire_at": expire_at}

    # ---- 1) 签到（决定整体成败） ----
    # 令牌有效期约 28 天，过期后接口返回 401/403。若不显式识别，
    # 云端会每天"静默失败"，主人误以为仍在领取 —— 故单独标记并强提示。
    checkin_error = None
    token_expired = False
    checkin = {"action": "error", "success": False, "message": ""}
    try:
        checkin = do_checkin(opener, headers)
    except ApiError as e:
        checkin_error = e
        checkin["message"] = str(e)
        if getattr(e, "http_code", None) in (401, 403):
            token_expired = True
            checkin["message"] += "（登录令牌已失效，请更新 WB_ACCESS_TOKEN）"
        log(f"签到失败: {checkin['message']}")
    except Exception as e:  # noqa: BLE001
        checkin_error = e
        checkin["message"] = f"未预期错误: {e}"
        log(f"签到未预期错误: {e}")
    summary["checkin"] = sanitize(checkin)
    summary["token_expired"] = token_expired
    if token_expired:
        log("登录令牌已失效：本次签到未生效，请尽快更新 WB_ACCESS_TOKEN（有效期约 28 天）")

    # ---- 2) 剩余积分（best-effort） ----
    remaining = None
    if checkin["success"]:
        remaining, raw_res = fetch_remaining(opener, headers)
        summary["remaining"] = remaining
        summary["remaining_raw"] = raw_res
        if remaining:
            log(f"剩余积分: {remaining}")

    # ---- 3) 派猫（独立容错，不影响签到结论） ----
    cat = None
    if checkin["success"] and not args.dry_run:
        try:
            cat = do_cat_flow(opener, headers,
                              adopt=adopt, location_id=location_id)
        except Exception as e:  # noqa: BLE001
            cat = {"success": False, "notes": [], "error": f"派猫流程未预期错误: {e}"}
            log(f"派猫流程未预期错误: {e}")
        summary["cat"] = sanitize(cat)
    elif args.dry_run:
        cat = {"success": True, "notes": ["dry-run：跳过派猫"], "error": None}
        summary["cat"] = cat

    # ---- 4) 推送（飞书 / 微信） ----
    if not args.dry_run:
        # 昨日收获：签到部分优先用接口自带历史（checkin_dates），
        # 猫猫部分只能来自上次运行写入的状态文件（无历史接口）。
        y_checkin = checkin.get("yesterday_credit")
        y_cat = None
        y_remaining = None
        if checkin["success"]:
            state_days, _sha = state_load()
            y_rec = (state_days.get("days") or {}).get(bj_date(-1)) or {}
            if y_checkin is None:
                y_checkin = y_rec.get("checkin_credit")
            y_cat = y_rec.get("cat_credit")
            y_remaining = y_rec.get("remaining")
            if y_checkin is not None or y_cat is not None or y_remaining is not None:
                log(f"昨日收获: 签到 {y_checkin} | 猫猫 {y_cat} | 剩余 {y_remaining}")

        title, yl, ck, catl, html = build_report(checkin, remaining, cat or {},
                                                 yesterday=(y_checkin, y_cat),
                                                 y_remaining=y_remaining)
        # 到期前预警：令牌约 28 天一换，提前 3 天开始每天提醒，避免断签
        if days_left is not None and days_left <= 3 and not token_expired:
            left_txt = ("今天" if days_left < 1 else "%.0f 天后" % days_left)
            title = "⚠️ 令牌%s到期 " % left_txt + title
            ck = ("WorkBuddy 登录令牌将于 %s 到期（剩余 %.1f 天）。\n"
                  "到期后云端会停止领取，请及时更新 Secret：WB_ACCESS_TOKEN。\n\n"
                  % (expire_at, days_left)) + ck
        if token_expired:
            # 标题前置告警，保证手机推送首屏就能看到"需要人工介入"
            title = "⚠️ 令牌失效·需更新 " + title
            ck = ("登录令牌已失效（有效期约 28 天），今日积分未领取。\n"
                  "请在本机重新获取令牌后更新仓库 Secret：WB_ACCESS_TOKEN。\n\n") + ck
        jobs = []
        if feishu:
            jobs.append(("飞书", lambda: push_feishu(feishu, title, yl, ck, catl)))
        if wx_token and wx_uids:
            jobs.append(("微信(WxPusher)", lambda: push_wxpusher(wx_token, wx_uids, title, html)))
        if pp_token:
            jobs.append(("微信(pushplus)", lambda: push_pushplus(pp_token, title, html)))
        for name, fn in jobs:
            try:
                fn()
                summary["push"].append({"channel": name, "success": True})
                log(f"推送成功: {name}")
            except Exception as e:  # noqa: BLE001
                summary["push"].append({"channel": name, "success": False,
                                        "error": str(e)})
                log(f"推送失败({name}): {e}")
        if not jobs:
            log("未配置任何推送通道（FEISHU_WEBHOOK / WXPUSHER_TOKEN+UID / PUSHPLUS_TOKEN）")

        # ---- 写状态文件（供明天推送"昨日收获"；本地模式自动跳过） ----
        if checkin["success"]:
            cat_credit = ((cat or {}).get("claim") or {}).get("reward_credit")
            state_record(bj_date(0),
                         checkin.get("today_credit") or 0,
                         cat_credit or 0,
                         remaining)

    # ---- 5) 摘要落盘（脱敏） ----
    summary["overall"] = "success" if checkin["success"] else "failed"
    try:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(summary_fix(summary), f, ensure_ascii=False, indent=2)
        log(f"脱敏摘要已写入: {args.out}")
    except Exception as e:  # noqa: BLE001
        log(f"摘要写入失败: {e}")

    # ---- 6) 退出码：签到成功即 0 ----
    if checkin_error is not None:
        return 1
    return 0


def summary_fix(s):
    return sanitize(s)


if __name__ == "__main__":
    sys.exit(main())
