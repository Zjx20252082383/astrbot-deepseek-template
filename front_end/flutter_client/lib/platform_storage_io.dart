import 'dart:io';

Future<String?> webViewDataDirectory() async {
  final root = Platform.environment['LOCALAPPDATA'];
  if (root == null) throw StateError('无法定位当前用户的应用数据目录');
  final directory = Directory(
    '$root${Platform.pathSeparator}ShihaiClient${Platform.pathSeparator}WebView2',
  );
  await directory.create(recursive: true);
  return directory.path;
}
