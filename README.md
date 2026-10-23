# WorkBuddy「Buddy 加油站」云端自动签到 + 微信推送

一套已实跑成功的自动化：**每天北京时间 06:00 自动签到、派猫猫旅行并领取积分，结果推送到微信**。

跑在 GitHub Actions 的云端机器上，所以**电脑关机、休眠、不开客户端都照常执行**，不依赖本机电源与网络。

---

## 一、换一台电脑，三步跑起来

### 第 1 步：装 Python 3.8 以上
到 python.org 下载安装，安装时**勾选 Add to PATH**。命令行执行 `python --version` 有版本号即 OK。

### 第 2 步：准备 4 个值

| 值 | 从哪来 | 说明 |
|---|---|---|
| GitHub 令牌 PAT | github.com → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → Generate new token，勾选 **repo** 和 **workflow** | 只在部署那一刻使用，用来建仓库、推代码、写密钥。**过期不影响每天签到** |
| WorkBuddy 令牌 | 本机 `~/.workbuddy/skills/workbuddy-checkin/config.json` 里的 `access_token` | 签到真正用的凭据，**约 28 天过期** |
| WorkBuddy 用户 ID | 同上文件的 `user_id` | 一串 UUID |
| WxPusher appToken + UID（可选） | wxpusher.zjiecode.com 扫码建应用拿 `AT_` 开头 appToken；微信搜公众号「WxPusher」→ 我的 → 我的 UID 拿 `UID_` 开头 | 免费、不用实名。不填也能跑，只是收不到微信 |

> Windows 上 `~` 指 `C:\Users\你的用户名`。

### 第 3 步：双击 `一键部署.bat`

按提示粘贴上面的值，脚本会自动完成：校验令牌 → 建仓库（不存在就自动建，默认私有）→ 推代码 → 加密写入密钥 → **立刻真实运行一次并等到出结论**。

看到 `✅ 云端实跑成功` 即完成，微信会收到一条带签到结果的消息。

> 网络受限环境（公司内网只放行 api.github.com、打不开 github.com 网页）也能成功：
> 推送走的是 GitHub API，不需要 git，也不需要打开 github.com。

---

## 二、跑完之后

- **每天 06:00（北京时间）**自动执行，无需任何干预。
- 想手动跑一次：仓库 → Actions → `Buddy 每日自动化` → `Run workflow`。
- 本机想验证配置对不对（不部署）：填好 `config.json` 后双击 `本地试跑.bat`。

---

## 三、文件结构

```
buddy-automation-pack/
├── 一键部署.bat           # 双击开始：交互式部署向导
├── 本地试跑.bat           # 双击：本机跑一次，验证配置
├── config.example.json    # 配置模板（复制成 config.json 后填值）
├── .github/workflows/
│   └── buddy-daily.yml    # 云端定时任务（每天 06:00 + 支持手动触发）
├── scripts/
│   ├── buddy_daily.py     # 主程序：签到 + 派猫 + 推送（零第三方依赖）
│   └── setup.py           # 一键部署向导（只用标准库）
└── docs/api-notes.md      # 接口契约实测记录
```

---

## 四、安全说明

- 所有令牌、用户 ID 只写入 **GitHub Secrets**（加密存储），**不会出现在代码里**，也不会被打印到日志（日志只显示前后 4 位与长度）。
- 运行摘要上传为 Actions 产物前会做**递归脱敏**，令牌、Bearer、UID 等敏感键一律掩码。
- 仓库默认**私有**。PAT 仅部署时使用，之后每天的定时任务不依赖它。
- 本地 `config.json` 含明文令牌，已在 `.gitignore` 中排除，不会被上传。

---

## 五、两个必须知道的时间点

1. **WorkBuddy 令牌约 28 天过期**。剩余 ≤3 天时，推送标题会自动变成
   `⚠️ 令牌 N 天后到期 —— Buddy加油站`，正文写明更换方法。

   收到提醒后（或任何时候想换），在本机**双击 `更新令牌.bat`**：它会自动从
   WorkBuddy 桌面端登录态里取出**最新令牌**，加密写进云端 Secret，并推一条微信确认。
   全程 10 秒，不用开网页、不用复制粘贴。
   （该脚本需要 GitHub 令牌：第一次会问，之后存在本地 `config.json` 里。）

   想立刻验证新令牌生效：`python scripts/update_secret.py --run`（更新后自动跑一次云端）。
2. **GitHub 会对 60 天无活动的仓库停用定时任务**。本工作流每次运行会写一个日期戳提交（保活），定时任务不会失联。

---

## 六、常见问题

**Q：微信没收到消息？**
A：① 先确认有没有**扫码关注**你自己在 WxPusher 建的那个应用（没关注收不到，这是最常见原因）；② 消息落在公众号「WxPusher」的会话里，不是服务通知，建议关掉该公众号的免打扰；③ 有几分钟延迟属正常。

**Q：部署时提示缺少 pynacl？**
A：脚本会自动尝试安装。若仍失败，会打印需要在网页填写的清单与直达链接，手动填一次即可，之后不影响运行。

**Q：换电脑后要重新部署吗？**
A：不用。云端已经在跑，与本机无关。只有要**改代码或更新令牌**时才需要再跑一次部署（需要一个新的 PAT）。

**Q：能改执行时间吗？**
A：改 `.github/workflows/buddy-daily.yml` 里的 cron。GitHub 用 UTC，北京时间减 8 小时：06:00 北京 = `0 22 * * *`。
