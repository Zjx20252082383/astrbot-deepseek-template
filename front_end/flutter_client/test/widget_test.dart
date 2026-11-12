import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:shihai_client/main.dart';
import 'package:shihai_client/server_address.dart';

void main() {
  test(
    'normalize address and reject invalid or credential-bearing addresses',
    () {
      expect(
        normalizeServerAddress(' 192.168.1.10:6191 '),
        'http://192.168.1.10:6191',
      );
      expect(
        normalizeServerAddress('https://console.example.com/app/'),
        'https://console.example.com/app/',
      );
      for (final address in [
        '',
        'file:///tmp/a',
        'javascript:alert(1)',
        'http://a:99999',
        'http://user:pass@a',
        'http://a/?token=secret',
        'http://a b',
      ]) {
        expect(() => normalizeServerAddress(address), throwsFormatException);
      }
    },
  );
  testWidgets('saved address is restored and invalid input is rejected', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({
      'server_address': 'http://192.168.1.20:6191',
    });
    await tester.pumpWidget(const ShihaiApp());
    await tester.pumpAndSettle();
    expect(find.text('http://192.168.1.20:6191'), findsOneWidget);
    await tester.enterText(find.byType(TextFormField), 'file:///tmp/a');
    await tester.tap(find.text('打开控制台'));
    await tester.pumpAndSettle();
    expect(find.text('请输入有效的 HTTP 或 HTTPS 地址'), findsOneWidget);
  });
}
