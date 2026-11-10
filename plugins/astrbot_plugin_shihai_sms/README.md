# 识海·信使（astrbot_plugin_shihai_sms）

让机器人"读到"用户的手机短信，把**快递信息**（取件码 / 运单号 / 驿站 / 状态）解析出来，**主动私聊发给用户**。

```
手机短信 → SmsForwarder App（安卓）→ HTTP POST http://电脑IP:6187/sms
        → 本插件解析入库 → 主动私聊上报用户
```

## 电脑端（已就绪）

1. 插件放在 `data\plugins\astrbot_plugin_shihai_sms\`，重启 AstrBot 后自动生效。
2. 在 WebUI（`http://127.0.0.1:6185`）→ 插件管理 → 本插件 → 配置：
   - `owner_qid`：用户 QQ 号（快递报给谁）
   - `platform_id`：平台适配器 ID（WebUI「平台」页里那台适配器的名字，当前模板用的是 `qq`）
   - `token`：随便设一串字母数字，等下手机端也要填同样的（推荐，防别人乱发请求）
3. 重启 AstrBot，日志里出现 `[识海信使] 短信接收服务已启动` 即成功。

## 手机端（安卓）

1. 安装开源应用 **SmsForwarder（短信转发器）**：GitHub `pppscn/SmsForwarder` 的 Releases 下载 APK（应用商店也可能搜到"短信转发器"）。
2. 首页 →「发送通道」→ 添加 → **WebHook**：
   - WebHook 地址：`http://电脑局域网IP:6187/sms`
     （电脑 IP 用 `ipconfig` 查"IPv4 地址"，如 `192.168.1.8`；手机和电脑必须连同一个 WiFi/路由器）
   - 请求方式：POST
   - 消息模板（保持默认即可，关键字段是 `{FROM_NUM}` 和 `{SMS_TXT}`）：
     ```json
     {"from": "{FROM_NUM}", "channel": "sms", "text": "{SMS_TXT}", "timestamp": "{DATE}"}
     ```
   - 额外请求头：`X-Token: 你在插件里设的token`
3. 回首页 →「规则」→ 添加：
   - 匹配模式：全部短信 / 只转发包含"快递|取件|驿站|签收|顺丰|菜鸟|丰巢"关键词的短信（省事且更安静）
   - 转发通道：选刚建的 WebHook
4. 权限：给 App「短信」「通知」权限，关掉电池优化（防止后台被杀）。可点通道上的"测试"发一条假短信验证。

### iOS 说明

iPhone 系统不允许第三方 App 读短信，无免越狱方案。退路：
- 用「快捷指令」手动转发关键短信（体验差）；
- 或只依赖快递 App 的推送（不可行于本插件）。**安卓是唯一顺畅路径。**

## Windows 防火墙（只做一次）

管理员 PowerShell 执行（端口按插件配置里的 `port` 改）：

```
netsh advfirewall firewall add rule name="ShihaiSms 6187" dir=in action=allow protocol=TCP localport=6187
```

## 指令与工具

| 入口 | 说明 |
|---|---|
| `/快递` | 列出最近 8 条快递短信（单号 / 状态 / 取件码） |
| `/短信 [条数]` | 列出最近收到的短信 |
| `/短信测试 <内容>` | 管理员测试解析，如 `/短信测试 【顺丰】您的快递已到菜鸟驿站，取件码:8-1-302` |
| LLM 工具 `get_recent_express` | 问她"有没有快递 / 取件码多少"时她会自动调用 |

## 上报格式示例

```
用户，有快递消息啦（11-08 14:32）
📦 顺丰 · 单号 SF1234567890123
状态：到站待取
取件码：8-1-302
原文：【顺丰速运】您有一个快递已到达菜鸟驿站(大学城店)...
```

## 隐私

短信原文只存本机（`data\plugin_data\shihai_sms\sms_records.json`，插件目录重装也不丢），默认保留 30 天、最多 500 条，不会上传到任何第三方。不想留非快递短信就把配置里 `forward_all` 关掉。
