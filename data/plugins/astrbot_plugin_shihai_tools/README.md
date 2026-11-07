![Moe Counter](https://count.getloli.com/@astrbot_plugin_uapi?theme=3d-num)

# astrbot_plugin_uapi

📡 封装 100+ 免费 API 的 AstrBot 插件，支持**指令调用**和 **LLM Tool 自动调用**两种方式。

## ✨ 功能特性

- 🔌 **100+ API 封装**：涵盖天气、IP 定位、翻译、热榜、二维码、OCR、Minecraft、B站、GitHub、文本处理、图片处理等
- 🎯 **指令调用**：通过 `/uapi` 指令直接调用任意 API
- 🤖 **LLM Tool**：将常用 API 注册为大模型可调用的 Function Tool，AstrBot 对话中可自动触发
- 🖼️ **媒体支持**：API 返回的图片自动发送为图片消息，音频自动以文件形式发送
- ⚙️ **可视化配置**：在 WebUI 中配置 API Key、启用/禁用 Tool、自定义 Tool 白名单
- 🔍 **搜索与浏览**：支持 API 列表分页、关键词搜索、模糊匹配

## ⚙️ 配置说明

在 AstrBot WebUI 插件管理页面，点击插件右侧的配置按钮：

| 配置项 | 类型 | 说明 | 默认值 |
|--------|------|------|--------|
| `api_key` | string | UAPI API Key（可选，免费API无需填写） | 空 |
| `enable_tools` | bool | 是否注册 LLM Tool | true |
| `tools_whitelist` | text | LLM Tool 白名单，逗号分隔 | 空（注册全部推荐） |
| `request_timeout` | int | 请求超时（秒） | 30 |

> 💡 **关于 API Key**：大部分 API 免费无需 Key。部分高级功能（如 AI 翻译、OCR）需要 Key。在 https://uapis.cn/console/api-keys 免费获取。

## 🤖 LLM Tool 说明

启用 `enable_tools` 后，插件会自动将以下类别的 API 注册为 LLM 可调用的 Function Tool：

- 🌤️ 天气、农历、世界时间
- 🌐 IP 查询、Ping、DNS、WHOIS
- 🌍 文本翻译
- 📊 全网热榜、智能搜索
- 📱 B站、QQ、GitHub 信息查询
- 🖼️ 二维码、必应壁纸
- 📝 MD5、Base64、AES 加解密
- 🎲 一言、答案之书、随机图片
- 🎮 Minecraft、Steam、Epic
- 📦 快递、手机归属地、电影票房

在对话中，LLM 会根据用户意图自动调用合适的 API。例如：

- 用户问"北京今天天气怎么样？" → 自动调用 `misc.weather`
- 用户问"帮我查下 8.8.8.8 是哪里的 IP" → 自动调用 `network.ipinfo`
- 用户问"帮我生成一个二维码链接到 example.com" → 自动调用 `image.qrcode`

### 自定义 LLM Tool 白名单

如果只想启用部分 API，在配置中填写 `tools_whitelist`（逗号分隔的 API 名称）：

```
misc.weather,network.ipinfo,translate.text,image.qrcode,misc.hotboard
```

## 📖 指令用法

### 基本格式

```
/uapi <子命令> [参数...]
```

### 简化调用（推荐）🔥

```
/uapi <API名称> <参数值1> <参数值2> ...
```

参数按 **API 定义中必填在前、可选在后** 的顺序填入，无需写参数名。

### 传统调用（兼容）

```
/uapi <API名称> 参数名=值 ...
```

两种模式可混用，插件会自动判断。

### 查看帮助

```
/uapi
/uapi help
```

### 列出所有 API

```
/uapi list              # 第1页
/uapi list 2            # 第2页
```

### 搜索 API

```
/uapi search 天气       # 搜索与"天气"相关的API
/uapi search bilibili   # 搜索B站相关API
```

### 查看 API 详情（含参数顺序）

```
/uapi misc.weather          # 查看天气API的参数说明与顺序
/uapi translate.text        # 查看翻译API的参数说明与顺序
/uapi image.qrcode          # 查看二维码API的参数说明与顺序
```

## 📋 常用 API 示例（简化调用）

### 🌤️ 天气与生活

```
# 查询天气
/uapi misc.weather 北京

# 查询天气（含扩展信息和预报）
/uapi misc.weather 上海 true true

# 查询世界时间
/uapi misc.worldtime Asia/Shanghai

# 查询农历时间
/uapi misc.lunartime

# 查询节假日
/uapi misc.holiday-calendar 2025-06-01

# 查询历史天气
/uapi misc.weather.history 北京
```

### 🌐 网络工具

```
# IP 归属地查询
/uapi network.ipinfo 8.8.8.8

# Ping 主机
/uapi network.ping github.com

# DNS 解析
/uapi network.dns github.com A

# WHOIS 查询
/uapi network.whois github.com

# URL 可访问性
/uapi network.urlstatus https://example.com

# ICP 备案查询
/uapi network.icp baidu.com

# 端口扫描
/uapi network.portscan example.com 80
```

### 🌍 翻译

```
# 文本翻译（中译英）
/uapi translate.text en 你好世界

# 文本翻译（英译中）
/uapi translate.text zh "Hello World"

# AI 智能翻译
/uapi ai.translate zh "Hello, how are you?"
```

### 📊 热榜与搜索

```
# 微博热搜
/uapi misc.hotboard weibo

# 知乎热榜
/uapi misc.hotboard zhihu

# B站热搜
/uapi misc.hotboard bilibili

# 抖音热搜
/uapi misc.hotboard douyin
```

### 📱 社交媒体

```
# B站视频信息
/uapi social.bilibili.videoinfo BV17x411w79F

# B站用户信息
/uapi social.bilibili.userinfo 456664753

# B站直播间
/uapi social.bilibili.liveroom 6

# QQ 用户信息
/uapi social.qq.userinfo 10001

# QQ 群信息
/uapi social.qq.groupinfo 123456789

# GitHub 仓库
/uapi github.repo owner/repo

# GitHub 用户
/uapi github.user Soulter
```

### 🖼️ 图片工具（自动发送图片）

```
# 生成二维码
/uapi image.qrcode https://example.com 512

# 必应每日壁纸
/uapi image.bing-daily

# 必应壁纸（指定分辨率 4k/1080）
/uapi image.bing-daily 2025-01-01 false 4k image

# 随机二次元图片
/uapi random.image

# 单词发音（自动以文件形式发送音频）
/uapi dictionary.audio hello
```

### 📝 文本处理

```
# MD5 哈希
/uapi text.md5 "hello world"

# Base64 编码
/uapi text.base64.encode "hello"

# Base64 解码
/uapi text.base64.decode "aGVsbG8="
```

### 🎮 游戏相关

```
# Minecraft 服务器状态
/uapi game.minecraft.serverstatus hypixel.net

# Minecraft 玩家信息
/uapi game.minecraft.userinfo Notch

# Steam 用户摘要
/uapi game.steam.summary 76561197960435530

# Epic 免费游戏
/uapi game.epic-free
```

### 🎲 随机与娱乐

```
# 一言（随机）
/uapi saying.random

# 一言（每日）
/uapi saying.random daily

# 答案之书
/uapi answerbook.ask "今天运势如何"

# 随机字符串
/uapi random.string 16

# 随机数
/uapi misc.randomnumber 1 100
```

### 📦 实用工具

```
# 手机归属地
/uapi misc.phoneinfo 13800138000

# 快递查询
/uapi misc.tracking.query SF1234567890

# 域名在微信中的状态
/uapi network.wxdomain example.com

# 时间戳转换
/uapi convert.unixtime 1704067200

# 电影票房
/uapi misc.movie-box-office

# 程序员历史上的今天
/uapi history.programmer.today

# 每日单词
/uapi daily.word

# 单词查询
/uapi dictionary.lookup hello
```

## 📋 传统调用示例（兼容）

如果更喜欢指定参数名，传统 `key=value` 模式仍完全支持：

```
# 查询天气
/uapi misc.weather city=北京

# IP 查询
/uapi network.ipinfo ip=8.8.8.8

# 翻译
/uapi translate.text to_lang=en text=你好世界

# 二维码
/uapi image.qrcode text=https://example.com size=512

# 热榜
/uapi misc.hotboard type=weibo
```

## 🏷️ 完整 API 列表

以下是所有可用 API 的分类概览。使用 `/uapi list` 可在对话中查看完整列表。

### 杂项 (Misc)
`misc.weather` `misc.worldtime` `misc.lunartime` `misc.hotboard` `misc.phoneinfo` `misc.randomnumber` `misc.timestamp` `misc.holiday-calendar` `misc.tracking.query` `misc.tracking.detect` `misc.tracking.carriers` `misc.date-diff` `misc.district` `misc.weather.history` `misc.movie-box-office` `misc.movie-rating-rank`

### 网络 (Network)
`network.ipinfo` `network.dns` `network.ping` `network.portscan` `network.urlstatus` `network.whois` `network.icp` `network.wxdomain`

### 翻译 (Translate)
`translate.text` `ai.translate` `ai.translate.languages` `translate.stream`

### 图片 (Image)
`image.qrcode` `image.bing-daily` `image.bing-daily.history` `image.frombase64` `image.motou` `image.speechless` `image.tobase64` `image.decode` `image.svg` `image.compress` `image.ocr` `image.nsfw` `avatar.gravatar`

### 文本 (Text)
`text.md5` `text.analyze` `text.base64.encode` `text.base64.decode` `text.aes.encrypt` `text.aes.decrypt` `text.aes.encrypt-advanced` `text.aes.decrypt-advanced` `text.markdown-to-html` `text.markdown-to-pdf` `text.convert` `text.md5.verify`

### 社交 (Social)
`social.bilibili.videoinfo` `social.bilibili.userinfo` `social.bilibili.archives` `social.bilibili.replies` `social.bilibili.liveroom` `social.qq.userinfo` `social.qq.groupinfo` `github.repo` `github.user`

### 游戏 (Game)
`game.minecraft.serverstatus` `game.minecraft.userinfo` `game.minecraft.historyid` `game.minecraft.version` `game.minecraft.mods` `game.steam.summary` `game.steam.servers` `game.epic-free`

### 随机 (Random)
`random.image` `random.string` `saying.random` `saying`

### 网页解析 (WebParse)
`webparse.metadata` `webparse.extractimages` `web.tomarkdown.async` `web.tomarkdown.async.{task_id}`

### 水印 (Watermark)
`watermark.embed` `watermark.decode` `watermark.label` `watermark.producer-code`

### 转换 (Convert)
`convert.json` `convert.unixtime`

### 其他
`answerbook.ask` `daily.news-image` `daily.word` `dictionary.lookup` `dictionary.audio` `history.programmer.today` `history.programmer` `status.ratelimit` `status.usage`

## 🛠️ 开发者说明

### 项目结构

```
astrbot_plugin_uapi/
├── metadata.yaml          # 插件元数据
├── _conf_schema.json      # 配置 Schema
├── requirements.txt       # 依赖
├── main.py                # 插件主入口（指令 + LLM Tool）
├── uapi_client.py         # HTTP 异步客户端
├── api_registry.py        # API 注册表（从 OpenAPI 规范自动生成）
└── README.md              # 说明文档
```

### 扩展新 API

`api_registry.py` 从 uapis.cn 的 OpenAPI 规范（`openapi.json`）自动生成。当平台新增 API 时，运行以下命令重新生成：

```bash
python tools/generate_registry.py
```

### 技术栈

- **HTTP 客户端**：`aiohttp`（异步）
- **LLM Tool**：基于 AstrBot `FunctionTool` + Pydantic `@dataclass`
- **配置管理**：AstrBot `AstrBotConfig`

## 📄 许可

AGPL 3.0 License

## 🔗 相关链接

- [UAPI 平台](https://uapis.cn)
- [UAPI API 文档](https://uapis.cn/docs/api-reference)
- [UAPI API Key 获取](https://uapis.cn/console/api-keys)
