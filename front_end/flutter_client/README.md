# 识海 Flutter 客户端

复用现有 `front_end/serve.py` 和 Web 控制台。Flutter 负责服务地址保存、内嵌网页、加载进度、30 秒超时提示、重试、返回和更换地址。不包含后端、模型密钥或 QQ 实现。

## 开发运行

在 PowerShell 切换到原项目的 `front_end/flutter_client` 后执行：

```powershell
flutter pub get
dart format lib test
flutter analyze
flutter test
flutter run -d chrome --web-port 6200
```

网页端先用于快速验证地址页面。首次打开后，点击“打开控制台”才会创建网页视图。

## 后端和连接地址

另开 PowerShell，在仓库根目录运行已有服务：

```powershell
$env:SHIHAI_HOST = '127.0.0.1'
.\.venv\Scripts\python.exe front_end\serve.py
```

先在浏览器验证 `http://127.0.0.1:6191`。机器人真实状态还需要 AstrBot 和对应插件正常运行；网页打开不代表机器人已连接。

- 同一电脑：`http://127.0.0.1:6191`。
- 安卓手机：使用电脑局域网 IP（例如 `http://192.168.1.10:6191`），手机和电脑同一 Wi-Fi，后端须监听可达接口且防火墙允许相应端口。手机上的 127.0.0.1 是手机自己。
- 远程：先配置服务端认证，再配置 cpolar/ngrok 的 HTTPS 地址；电脑与后端保持运行。不应直接公开当前未审查的本地服务。
- 地址必须为 HTTP/HTTPS，不接受 URL 内账号、查询参数或 fragment，避免保存含令牌链接；服务自身的登录页面由网页处理。

## 多端差异

Android / Windows 使用原生 WebView。浏览器版使用 iframe，受目标站点 CSP、X-Frame-Options、混合内容限制；跨域网页不能保证支持所有返回/加载错误事件。浏览器版返回按钮回到地址页，重新加载通过重建视图实现。iframe 的加载完成不代表后端接口健康。

Android Manifest 已为局域网 HTTP 开启联网权限和 cleartext；生产远程部署应优先 HTTPS。没有跳过 TLS 证书检查。Android 17 等较新系统的局域网权限策略如影响访问，需要根据真机错误另行适配。

Windows 要安装 Edge WebView2 Runtime，并确保 NuGet CLI 在 PATH；新系统通常已带 WebView2，实际以运行检查为准。WebView2 数据保存在当前用户 LOCALAPPDATA/ShihaiClient/WebView2（可能在 C 盘），不在 SDK 目录。

```powershell
flutter run -d windows
flutter build apk --debug
```

调试 APK：`build/app/outputs/flutter-apk/app-debug.apk`。正式发行前配置独立签名，当前 Android 模板 release 仍用调试签名。

## 网络与磁盘

开发机器使用 PUB_CACHE / GRADLE_USER_HOME 指向 D 盘。Flutter 命令行代理与 Gradle JVM 代理不同；如果 Maven 下载超时，可在本机 `GRADLE_USER_HOME/gradle.properties` 中配置 HTTP/HTTPS 代理，不能把个人代理配置提交到项目。

```properties
systemProp.http.proxyHost=127.0.0.1
systemProp.http.proxyPort=7900
systemProp.https.proxyHost=127.0.0.1
systemProp.https.proxyPort=7900
systemProp.http.nonProxyHosts=localhost|127.*|[::1]
```

首次 Android 构建可能根据 Flutter 模板要求下载其他 SDK/NDK/构建工具版本；不固定覆盖模板的编译 SDK 版本。若 Gradle 报 Java 版本不兼容，按实际日志选择兼容 JDK，不修改本机其他项目的 Java 设置。

## 校验状态

- Android：已成功构建调试 APK，并在安卓真机通过局域网连接电脑后端；已验证控制台加载、插件配置访问、返回后重新进入。
- Web：已在 Chrome 验证连接页面和内嵌控制台。
- 时间曲线：真机可显示曲线；心智参数的计算和主动消息策略由现有后端插件负责。
- Windows：已包含平台工程，尚未完成 Windows 客户端运行验证。
- 远程访问：尚未配置内网穿透；目前验证范围为本机及局域网。

### Android 构建兼容性

WebView 依赖已调整为 flutter_inappwebview 6.2.0-beta.3，以解决本次构建遇到的 AGP 兼容问题，具体依赖以 pubspec.yaml 和 pubspec.lock 为准。

android/gradle.properties 中保留 kotlin.incremental=false，用于规避 Windows 上工程与依赖缓存位于不同盘符时遇到的 Kotlin 增量缓存错误，可能增加后续编译时间。

提交前执行 flutter analyze 和 flutter test，确认当前最终版本的检查结果。

## 提交

仅提交客户端源文件和文档，遵循原 Flutter `.gitignore`；不要使用仓库根目录 `git add .`，不要提交密钥、虚拟环境、聊天数据、`build/`、`.dart_tool/`、`android/local.properties`。本包不修改任何现有后端文件。
