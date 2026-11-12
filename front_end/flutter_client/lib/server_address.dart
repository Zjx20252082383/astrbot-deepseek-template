/// Normalize a console address without silently accepting non-web schemes or secrets.
String normalizeServerAddress(String input) {
  var value = input.trim();
  if (value.isEmpty) throw const FormatException('请输入服务地址');
  if (!value.contains('://')) value = 'http://$value';
  final uri = Uri.tryParse(value);
  if (uri == null ||
      !['http', 'https'].contains(uri.scheme) ||
      uri.host.isEmpty ||
      RegExp(r'\s').hasMatch(value)) {
    throw const FormatException('请输入有效的 HTTP 或 HTTPS 地址');
  }
  if (uri.userInfo.isNotEmpty || uri.hasQuery || uri.hasFragment) {
    throw const FormatException('请填写服务入口地址，不要包含账号、令牌参数或 #');
  }
  try {
    if (uri.port < 1 || uri.port > 65535) throw const FormatException();
  } on FormatException {
    throw const FormatException('端口应在 1–65535 之间');
  }
  return uri.toString();
}
