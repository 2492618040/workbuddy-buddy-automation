# 接口契约（全部经真实账号实测核实）

> 核实日期：2026-10-23 ｜ 客户端：WorkBuddy 桌面端 5.5.6（`@genie/workbuddy-desktop`）
> 核实方式：桌面端安装包 `app.asar` 反向提取 + Web 端「成长中心」页面 JS chunk 提取
> （`assets.workbuddy.cn/web/usercenter/.../growthSpace-*.js`、`GrowthCenterPage-*.js`），
> 再用真实 Bearer 令牌逐一实测。
>
> ⚠️ 网上流传的旧接口名（如 `/billing/meter/checkin-status`、无 `/v2` 前缀的
> `daily-checkin`）**不适用**，本文所有路径均以实际请求结果为准。

## 0. 通用

- **Base URL**：`https://copilot.tencent.com`（桌面端 prod endpoint，app.asar 内
  `DEV_ENV_ENDPOINTS.prod`；可用 `WB_ENDPOINT` 覆盖）
- **鉴权头**：
  - `Authorization: Bearer <accessToken>`
  - `X-User-Id: <uid>`（账号 uid，缺失时部分接口鉴权失败）
  - `X-Enterprise-Id` / `X-Tenant-Id` / `X-Domain`（按账号可选）
- **响应包裹**：`{"code":0,"msg":"OK","requestId":"...","data":{...}}`，`code==0` 为成功
- **User-Agent**：任意（实测无图灵设备令牌 `X-Device-Token` 亦可调通）

## 1. 签到（Buddy 加油站）

| 接口 | 方法 | 说明 |
|------|------|------|
| `/v2/billing/meter/checkin-activity-status` | POST | 查询签到状态 |
| `/v2/billing/meter/daily-checkin` | POST | 执行签到 |
| `/billing/meter/get-user-resource-summary` | POST | 剩余积分（best-effort） |

### 1.1 checkin-activity-status 实测返回（节选，已脱敏）

```json
{
  "code": 0, "msg": "OK", "requestId": "2c2a2848-****",
  "data": {
    "active": true,
    "today_checked_in": false,
    "streak_days": 16,
    "daily_credit": 100,
    "today_credit": 100,
    "is_streak_day": false,
    "next_streak_day": 0,
    "streak_bonus_days": 0,
    "streak_bonus_credit": 0,
    "checkin_dates": ["2026-10-22", "2026-10-21", "..."],
    "week_checkin_days": 4,
    "total_credits": 1700,
    "start_time": "2026-09-30 00:00:00",
    "end_time": "2026-11-30 23:59:59",
    "theme_name": "Buddy加油站",
    "season": 10,
    "activity_name": "Buddy应用"
  }
}
```

### 1.2 daily-checkin 实测返回（节选）

```json
{"code": 0, "msg": "OK", "data": {"credit": 100, "streak_days": 17, "is_streak_day": false}}
```

## 2. 派猫猫旅行（成长中心 GrowthCenter）

| 接口 | 方法 | 说明 |
|------|------|------|
| `/activity/growth/buddy/info` | GET | 猫猫信息（未领养时 `data.buddy` 为 `null`） |
| `/activity/growth/buddy/travel/config` | GET | 旅行地点列表 `data.locations[]` |
| `/activity/growth/buddy/travel/status` | GET | 旅行状态 |
| `/activity/growth/buddy/travel/depart` | POST | 派猫出发，body `{"location_id": <id>}` |
| `/activity/growth/buddy/travel/claim` | POST | 领取已到家旅行积分 |
| `/activity/growth/buddy/first` | POST | 领养第一只猫猫（opt-in，`ADOPT_BUDDY=1`） |

### 2.1 travel/status 实测返回（关键字段）

```json
{
  "code": 0, "msg": "OK",
  "data": {
    "state": "idle",            // idle=在家 | traveling=旅行中 | arrived=已到家可领奖
    "buddy_id": 0,
    "record_id": 0,
    "location": null,           // traveling 时为 {"id":1,"name":"咖啡馆",...}
    "depart_at": 0,
    "arrive_at": 0,
    "server_now": 1792724344,
    "letter": null,
    "use_deeplink": "",
    "daily_limit_reached": false,  // 今天是否已派过
    "duration_hours": 0,
    "reward_credit": 0
  }
}
```

### 2.2 travel/config 实测返回（节选）

```json
{
  "code": 0, "msg": "OK",
  "data": {
    "locations": [
      {
        "id": 1, "code": "coffee", "name": "咖啡馆",
        "description": "阳光、音乐和手冲咖啡的香气，是放空的好地方。",
        "duration_hours_min": 1, "duration_hours_max": 4,
        "reward_credit_min": 5, "reward_credit_max": 10,
        "sort": 1
      }
    ]
  }
}
```

### 2.3 travel/claim 实测返回

- 无可领（正常分支，非错误）：HTTP 403 `{"code":403,"msg":"no unclaimed travel"}`
- 领取成功：`{"code":0,"data":{"reward_credit":10,"letter":"...","use_deeplink":"..."}}`

### 2.4 travel/depart 错误分支（与 Web 端逻辑逐条对齐）

| 服务端返回 | 含义 | 脚本处理 |
|-----------|------|----------|
| HTTP 429 或 msg 含 `daily limit` | 今天已派过 | 正常跳过 |
| msg 含 `already traveling` | 旅行中 | 刷新状态后跳过 |
| msg 含 `no active buddy` | 未领养猫猫 | 记录待办（可 `ADOPT_BUDDY=1` 自动领养） |
| msg 含 `location not available` | 地点不可用 | 自动换地点重试一次 |
| `code==0` | 出发成功 | `data.state` 变 `traveling` |

## 3. 状态机（与 Web 端 GrowthCenterPage 一致）

```
idle（在家） --depart--> traveling（旅行中） --时长到--> arrived（已到家）
arrived --claim--> idle（同时发放 reward_credit）
daily_limit_reached = true 时，idle 状态不再允许 depart（每日限一次）
```

## 4. 推送通道

| 通道 | 端点 | 关键参数 | 备注 |
|------|------|----------|------|
| 飞书机器人 | 仓库 secret `FEISHU_WEBHOOK` | `msg_type=interactive` 卡片 | 免费；签到/猫猫分节展示 |
| WxPusher（微信） | `POST /api/send/message` | `appToken`/`uids`/`contentType=2` | 免费，成功码 `1000` |
| pushplus（微信） | `POST /send` | `token`/`content`/`template=html` | **未实名账号返回 905 无法发送** |
