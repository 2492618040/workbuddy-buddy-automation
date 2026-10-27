# WorkBuddy「Buddy 加油站」GitHub Actions 每日自动化

每天定时在 **GitHub 云端 runner** 上自动完成：

1. **Buddy 加油站每日签到**（领通用积分）
2. **派猫猫旅行**——先领取已到家的旅行积分，再判断是否可派新一趟（当天已派过则跳过）
3. **结果推送**——签到结果与猫猫状态**分节展示**，支持飞书 / 微信（WxPusher / pushplus）

无需本地常开设备。接口契约全部经桌面端安装包 + Web 端页面反向核实并真实实测
（见 [docs/api-notes.md](docs/api-notes.md)），不使用网上流传的旧接口名。

## 仓库结构

```
├── .github/workflows/buddy-daily.yml   # 每日定时任务（北京时间 06:00，可手动触发）
├── scripts/
│   ├── buddy_daily.py                  # 主脚本：签到 + 派猫 + 推送（单次运行，零第三方依赖）
│   └── refresh_token.py                # 本机令牌刷新：读取登录态 → 注入 GitHub Secret
├── docs/api-notes.md                   # 接口契约与实测返回（脱敏）
└── README.md
```

## 部署步骤（约 10 分钟）

### 1. 创建仓库并推送

```bash
git init
git add .
git commit -m "feat: Buddy 每日自动化（签到+派猫+推送）"
git branch -M main
git remote add origin https://github.com/<你的账号>/<仓库名>.git
git push -u origin main
```

### 2. 注入凭证 Secret

**方式 A（推荐，自动）**：本机安装 [gh CLI](https://cli.github.com/) 并 `gh auth login` 后，
在**本机普通终端**（cmd / PowerShell / Terminal，勿经 AI 沙箱——沙箱会杀死读取登录文件的进程）运行：

```bash
python3 scripts/refresh_token.py --repo <你的账号>/<仓库名>
```

自动写入 `WB_ACCESS_TOKEN` 与 `WB_USER_ID` 两个 Secret，令牌不回显、不落盘。

**方式 B（网页手动）**：不加 gh 时运行 `python3 scripts/refresh_token.py --repo <账号>/<仓库> --show`，
按提示到 `https://github.com/<账号>/<仓库>/settings/secrets/actions` 新建 Secret：

| Name | 值 | 必填 |
|------|----|------|
| `WB_ACCESS_TOKEN` | 登录态 accessToken（约 28 天有效） | ✅ |
| `WB_USER_ID` | 账号 uid | ✅ |
| `WB_ENTERPRISE_ID` / `WB_DOMAIN` | 企业账号才需要 | ➖ |
| `FEISHU_WEBHOOK` | 飞书自定义机器人 webhook | 推送三选一 |
| `WXPUSHER_TOKEN` + `WXPUSHER_UID` | WxPusher 微信推送（免费） | 推送三选一 |
| `PUSHPLUS_TOKEN` | pushplus（未实名发不出，见 FAQ） | 推送三选一 |
| `TRAVEL_LOCATION_ID` | 指定旅行地点 id（默认随机） | ➖ |
| `ADOPT_BUDDY` | **默认 `1`（自动领养第一只猫猫并自动签协议）；设为 `0` 关闭** | ➖ |

> 令牌过期（接口 401）后重跑 `refresh_token.py` 即可，无需改代码。

### 3. 验收运行

仓库页面 → **Actions** → 「Buddy 每日自动化」→ **Run workflow** 手动触发一次。
成功后下载产物 `buddy-run-summary`（脱敏摘要，含各接口原始返回）。

## 推送通道配置

### 飞书（免费，推荐）
群设置 → 添加「自定义机器人」→ 复制 webhook 填 `FEISHU_WEBHOOK`。
签到结果与猫猫状态在卡片中**分节展示**。

### 微信 · WxPusher（免费）
1. 微信扫码登录 <https://wxpusher.zjiecode.com/admin/>（自动注册）
2. 创建应用 → 得到 `AT_` 开头的 appToken → 填 `WXPUSHER_TOKEN`
3. 关注公众号「wxpusher」→ 我的 → 我的UID → 填 `WXPUSHER_UID`

### 微信 · pushplus
<https://pushplus.plus> 微信扫码登录 → 个人中心复制用户token → 填 `PUSHPLUS_TOKEN`。
⚠️ 未实名账号会返回 `905 账户未进行实名认证` 无法发送（实名收费）。

## 本地手动运行（调试用）

```bash
export WB_ACCESS_TOKEN="..."        # bash；PowerShell 用 $env:WB_ACCESS_TOKEN="..."
export WB_USER_ID="..."
python3 scripts/buddy_daily.py      # 解释器路径用 command -v python3 动态获取
```

## 行为规则

- **幂等**：今日已签则跳过；当天已派猫则跳过；可一天内多次运行。
- **猫猫失败不影响签到**：签到成功即整体成功（退出码 0）；签到失败退出码 1，
  Actions 标记红色，猫猫部分照常尝试并在推送中单独说明。
- **安全**：令牌只走环境变量/Secrets；日志与摘要中敏感字段一律脱敏（`abcd***wxyz`）；
  仓库 `.gitignore` 已排除一切本地配置文件。
- **执行时序**：先签到 → 查剩余积分 → 派猫（claim → 判状态 → depart）→ 推送 → 写摘要。

## FAQ

**Q：本地电脑休眠/关机时还能跑吗？**
A：**能，完全不受影响。** 任务运行在 GitHub 云端 runner 上，与本地电脑电源状态无关——
关机、休眠、断网都照跑。这正是选云端方案的原因（本地自动化在机器睡着时会延迟补跑、关机则直接错过）。

**Q：定时任务会不会用着用着被停掉？**
A：GitHub 会对 60 天无仓库活动的仓库自动停用 schedule 工作流。本仓库已内置**保活提交**
（每次运行写日期戳并推送），只要任务在跑仓库就有活动，定时任务永不失联。

**Q：Actions 有免费额度吗？**
A：公开仓库完全免费；私有仓库每月 2000 分钟免费额度，本任务每次运行约 1 分钟
（每天 1 次 ≈ 30 分钟/月），远低于限额。

**Q：Actions 日志里报 401？**
A：令牌过期（约 28 天）。本机重跑 `scripts/refresh_token.py --repo <账号>/<仓库>`。

**Q：猫猫部分显示"尚未领养猫猫"？**
A：自动领养默认开启（`ADOPT_BUDDY=1`）：首次运行会自动签署协议并领养第一只猫猫。
如曾设为 `0` 关闭，改回 `1` 或到成长中心页面手动领养即可。

**Q：派猫显示"旅行中"不派新？**
A：正常。猫旅行 1–4 小时，回来后先自动领奖再派下一趟；每日限派一次。

**Q：推送收不到？**
A：WxPusher 检查是否关注公众号；pushplus 检查实名状态（905）；飞书检查机器人是否被移除。

**Q：定时没跑？**
A：GitHub schedule 有 10–30 分钟延迟属正常；cron 为 UTC 时间（北京时间 06:00 = `0 22 * * *`）。
