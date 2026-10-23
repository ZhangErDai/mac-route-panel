import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app
import run


class PackagingTests(unittest.TestCase):
    def probe(self, mode):
        requested = []
        token = 'a' * 43
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                requested.append((self.path, self.headers.get('X-Session-Token')))
                status, body = 404, b'{}'
                if self.path == '/health' and mode in ('current', 'stopping'):
                    status = 200
                    body = json.dumps(dict(application='local-route-panel', stopping=mode == 'stopping')).encode()
                elif self.path == '/health' and mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', '/api/routes')
                    self.end_headers()
                    return
                elif self.path == '/':
                    status = 200
                    if mode == 'other':
                        body = b'<title>Another local app</title>'
                    else:
                        body = (f'<title>本机路由管理</title><meta name="session-token" content="{token}">'
                                '<script src="/static/app.js"></script><button id="shutdown"></button>').encode()
                elif self.path == '/api/routes':
                    status = 403 if mode in ('imitation', 'redirect') else 200
                    body = json.dumps(dict(routes=[], interfaces=[], busy=False, stopping=False)).encode()
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            found = run.already_running(server.server_port)
        finally:
            server.shutdown();server.server_close();thread.join(2)
        return found, requested

    def test_legacy_server_requires_page_and_authenticated_api(self):
        found, requested = self.probe('legacy')
        self.assertTrue(found)
        self.assertEqual(requested, [('/health', None), ('/', None), ('/api/routes', 'a' * 43)])

    def test_current_server_uses_health_without_loading_routes(self):
        found, requested = self.probe('current')
        self.assertTrue(found)
        self.assertEqual(requested, [('/health', None)])

    def test_unrelated_or_imitation_server_is_not_reused(self):
        for mode in ('other', 'imitation', 'redirect'):
            with self.subTest(mode=mode):
                self.assertFalse(self.probe(mode)[0])

    def test_stopping_server_is_not_reused(self):
        found, requested = self.probe('stopping')
        self.assertFalse(found)
        self.assertEqual(requested, [('/health', None)])

    def test_frozen_admin_command_reenters_fixed_helper(self):
        with tempfile.TemporaryDirectory(prefix='程序 空格 ') as directory:
            def execute(args, **kwargs):
                command = shlex.split(args[-1])
                self.assertEqual(command[:4], ['/usr/bin/env', 'PYINSTALLER_RESET_ENVIRONMENT=1',
                                               sys.executable, '--admin-helper'])
                self.assertEqual(json.loads(Path(command[4]).read_text())['action'], 'disable')
                return SimpleNamespace(returncode=0, stderr='', stdout='{"ok": true}')
            with patch('app.sys.frozen', True, create=True), patch('app.subprocess.run', side_effect=execute):
                self.assertTrue(app.admin_apply({'action': 'disable'}, Path(directory))['ok'])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_helper_dispatch_does_not_start_web_server_or_probe_port(self):
        with patch('run.FROZEN', True), patch('sys.argv', ['RoutePanel', '--admin-helper', '/tmp/request.json']), \
                patch('privileged.main') as helper, patch('run.already_running') as probe:
            self.assertEqual(run.main(), 0)
            helper.assert_called_once_with('/tmp/request.json')
            probe.assert_not_called()

    def test_repeat_launch_reuses_instance_without_initializing_store(self):
        with patch('sys.argv', ['run.py']), patch('run.already_running', return_value=True), \
                patch('run.webbrowser.open') as browser, patch('app.create_app') as create:
            self.assertEqual(run.main(), 0)
            browser.assert_called_once_with('http://127.0.0.1:8765')
            create.assert_not_called()

    def test_occupied_unrelated_port_has_actionable_error(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            with patch('sys.argv', ['run.py', '--port', str(port)]), \
                    patch('run.already_running', return_value=False):
                with self.assertRaisesRegex(RuntimeError, '关闭后台'):
                    run.main()


if __name__ == '__main__':
    unittest.main()
