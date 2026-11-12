import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'platform_storage_stub.dart'
    if (dart.library.io) 'platform_storage_io.dart';
import 'server_address.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const ShihaiApp());
}

class ShihaiApp extends StatelessWidget {
  const ShihaiApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
    title: '识海控制台',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF5266BC)),
      scaffoldBackgroundColor: const Color(0xFFF4F6FD),
      useMaterial3: true,
      inputDecorationTheme: const InputDecorationTheme(
        border: OutlineInputBorder(),
      ),
    ),
    home: const ConnectionPage(),
  );
}

class ConnectionPage extends StatefulWidget {
  const ConnectionPage({super.key});
  @override
  State<ConnectionPage> createState() => _ConnectionPageState();
}

class _ConnectionPageState extends State<ConnectionPage> {
  final _address = TextEditingController(text: 'http://127.0.0.1:6191');
  final _form = GlobalKey<FormState>();
  bool _busy = true;
  String? _notice;

  @override
  void initState() {
    super.initState();
    _restore();
  }

  Future<void> _restore() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final saved = prefs.getString('server_address');
      if (mounted && saved != null) _address.text = saved;
    } catch (_) {
      if (mounted) _notice = '无法读取上次的地址，可以手动输入。';
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _open() async {
    if (!_form.currentState!.validate()) return;
    final address = normalizeServerAddress(_address.text);
    FocusScope.of(context).unfocus();
    setState(() {
      _busy = true;
      _notice = null;
    });
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('server_address', address);
    } catch (_) {
      if (mounted) setState(() => _notice = '地址未能保存，本次仍可打开。');
    }
    if (!mounted) return;
    setState(() => _busy = false);
    await Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => ConsolePage(address: address)),
    );
  }

  @override
  void dispose() {
    _address.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('识海控制台')),
    body: SafeArea(
      child: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 580),
            child: Card(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Form(
                  key: _form,
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const Icon(
                        Icons.dashboard_outlined,
                        size: 48,
                        color: Color(0xFF5266BC),
                      ),
                      const SizedBox(height: 16),
                      Text(
                        '连接你的识海',
                        style: Theme.of(context).textTheme.headlineSmall,
                      ),
                      const SizedBox(height: 8),
                      const Text('填写已启动的 Web 控制台地址。电脑上的后端服务需要保持运行。'),
                      const SizedBox(height: 24),
                      TextFormField(
                        controller: _address,
                        enabled: !_busy,
                        keyboardType: TextInputType.url,
                        autocorrect: false,
                        enableSuggestions: false,
                        decoration: const InputDecoration(
                          labelText: '服务地址',
                          hintText: 'http://192.168.1.10:6191',
                        ),
                        validator: (value) {
                          try {
                            normalizeServerAddress(value ?? '');
                            return null;
                          } on FormatException catch (error) {
                            return error.message;
                          }
                        },
                        onFieldSubmitted: (_) {
                          if (!_busy) _open();
                        },
                      ),
                      const SizedBox(height: 16),
                      FilledButton.icon(
                        onPressed: _busy ? null : _open,
                        icon: const Icon(Icons.open_in_new),
                        label: Text(_busy ? '准备中…' : '打开控制台'),
                      ),
                      if (_notice != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 12),
                          child: Text(_notice!),
                        ),
                      const SizedBox(height: 20),
                      const Text(
                        '本机：127.0.0.1:6191\n手机：同一 Wi-Fi 下电脑的局域网 IP 和端口\n远程：已配置的 HTTPS 公网地址',
                      ),
                      const SizedBox(height: 12),
                      const Text(
                        '手机上的 127.0.0.1 指手机自身。远程服务地址请使用 HTTPS；不要把模型密钥填在这里。',
                        style: TextStyle(fontSize: 12, color: Colors.black54),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

class ConsolePage extends StatefulWidget {
  const ConsolePage({super.key, required this.address});
  final String address;
  @override
  State<ConsolePage> createState() => _ConsolePageState();
}

class _ConsolePageState extends State<ConsolePage> {
  InAppWebViewController? _controller;
  WebViewEnvironment? _environment;
  bool _ready = false;
  double _progress = 0;
  String? _error;
  Timer? _timer;
  int _generation = 0;

  @override
  void initState() {
    super.initState();
    _prepare();
  }

  Future<void> _prepare() async {
    try {
      if (!kIsWeb && defaultTargetPlatform == TargetPlatform.windows) {
        final version = await WebViewEnvironment.getAvailableVersion();
        if (version == null) {
          throw StateError('未找到 Microsoft Edge WebView2 Runtime，请安装后重新打开客户端。');
        }
        final folder = await webViewDataDirectory();
        _environment = await WebViewEnvironment.create(
          settings: WebViewEnvironmentSettings(userDataFolder: folder),
        );
      }
      if (!mounted) return;
      setState(() => _ready = true);
      _watchLoad();
    } catch (error) {
      if (mounted) setState(() => _error = '网页组件初始化失败：$error');
    }
  }

  void _watchLoad() {
    _timer?.cancel();
    _timer = Timer(const Duration(seconds: 30), () {
      if (mounted) setState(() => _error = '页面长时间未完成加载。请确认后端已启动、地址正确且网络可达。');
    });
  }

  void _retry() {
    _controller = null;
    setState(() {
      _error = null;
      _progress = 0;
      _generation++;
    });
    if (!_ready) {
      _prepare();
    } else {
      _watchLoad();
    }
  }

  Future<void> _back() async {
    try {
      if (!kIsWeb && _controller != null && await _controller!.canGoBack()) {
        await _controller!.goBack();
        return;
      }
    } catch (_) {
      /* Returning to the connection page remains available. */
    }
    if (mounted) Navigator.of(context).pop();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: false,
    onPopInvokedWithResult: (didPop, result) {
      if (!didPop) _back();
    },
    child: Scaffold(
      appBar: AppBar(
        automaticallyImplyLeading: false,
        leading: IconButton(
          onPressed: _back,
          tooltip: '返回',
          icon: const Icon(Icons.arrow_back),
        ),
        title: const Text('识海控制台'),
        actions: [
          IconButton(
            onPressed: _retry,
            tooltip: '重新加载',
            icon: const Icon(Icons.refresh),
          ),
          IconButton(
            onPressed: () => Navigator.of(context).pop(),
            tooltip: '更换地址',
            icon: const Icon(Icons.settings_outlined),
          ),
        ],
      ),
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  widget.address,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ),
            if (_error == null && _progress < 1)
              LinearProgressIndicator(value: _progress == 0 ? null : _progress),
            Expanded(
              child: _error != null
                  ? Center(
                      child: SingleChildScrollView(
                        padding: const EdgeInsets.all(24),
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            const Icon(Icons.cloud_off_outlined, size: 48),
                            const SizedBox(height: 16),
                            Text(_error!, textAlign: TextAlign.center),
                            if (kIsWeb)
                              const Padding(
                                padding: EdgeInsets.only(top: 12),
                                child: Text(
                                  '浏览器端还需允许 iframe 嵌入；HTTPS 页面不能嵌入 HTTP 服务。',
                                  textAlign: TextAlign.center,
                                ),
                              ),
                            const SizedBox(height: 16),
                            FilledButton(
                              onPressed: _retry,
                              child: const Text('重试'),
                            ),
                            TextButton(
                              onPressed: () => Navigator.of(context).pop(),
                              child: const Text('更换地址'),
                            ),
                          ],
                        ),
                      ),
                    )
                  : !_ready
                  ? const Center(child: CircularProgressIndicator())
                  : InAppWebView(
                      key: ValueKey(_generation),
                      webViewEnvironment: _environment,
                      initialUrlRequest: URLRequest(
                        url: WebUri(widget.address),
                      ),
                      initialSettings: InAppWebViewSettings(
                        javaScriptEnabled: true,
                        useShouldOverrideUrlLoading: true,
                      ),
                      onWebViewCreated: (controller) =>
                          _controller = controller,
                      shouldOverrideUrlLoading: (controller, action) async {
                        final scheme = action.request.url?.scheme;
                        return scheme == 'http' ||
                                scheme == 'https' ||
                                scheme == 'about'
                            ? NavigationActionPolicy.ALLOW
                            : NavigationActionPolicy.CANCEL;
                      },
                      onLoadStart: (controller, url) {
                        if (!mounted) return;
                        setState(() {
                          _progress = 0;
                        });
                        _watchLoad();
                      },
                      onProgressChanged: (controller, progress) {
                        if (mounted) setState(() => _progress = progress / 100);
                      },
                      onLoadStop: (controller, url) {
                        _timer?.cancel();
                        if (mounted) setState(() => _progress = 1);
                      },
                      onReceivedError: (controller, request, error) {
                        if (request.isForMainFrame != true) return;
                        _timer?.cancel();
                        if (mounted) {
                          setState(
                            () => _error =
                                '无法打开控制台：${error.description}\n请检查服务地址和电脑后端。',
                          );
                        }
                      },
                      onReceivedHttpError: (controller, request, response) {
                        if (request.isForMainFrame != true) return;
                        _timer?.cancel();
                        if (mounted) {
                          setState(
                            () => _error =
                                '服务返回 HTTP ${response.statusCode}。请检查地址或访问权限。',
                          );
                        }
                      },
                    ),
            ),
          ],
        ),
      ),
    ),
  );
}
