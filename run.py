import argparse
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import traceback
import urllib.request
import webbrowser


FROZEN = getattr(sys, 'frozen', False)
APP_DIRECTORY = Path.home() / 'Library/Application Support/LocalRoutePanel'


def already_running(port):
    # Recognize current and pre-packaging servers without changing either one.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(base + '/health', timeout=1) as response:
            data = json.loads(response.read(4096))
        if isinstance(data, dict) and data.get('application') == 'local-route-panel':
            return data.get('stopping') is False
    except (OSError, ValueError):
        pass

    # Older source servers have no /health endpoint. Require both our page
    # markers and a successful session-authenticated, read-only route response.
    try:
        with opener.open(base + '/', timeout=1) as response:
            page = response.read(65536).decode('utf-8')
        if not all(marker in page for marker in ('<title>本机路由管理</title>',
                                                'src="/static/app.js"', 'id="shutdown"')):
            return False
        token = re.search(r'<meta\s+name="session-token"\s+content="([A-Za-z0-9_-]{32,128})"', page)
        if not token:
            return False
        request = urllib.request.Request(base + '/api/routes',
                                         headers={'X-Session-Token': token[1]})
        with opener.open(request, timeout=2) as response:
            data = json.loads(response.read(1048576))
        return (isinstance(data, dict) and isinstance(data.get('routes'), list)
                and isinstance(data.get('interfaces'), list)
                and isinstance(data.get('busy'), bool) and data.get('stopping') is False)
    except (OSError, ValueError):
        return False


def report_error(message):
    if not FROZEN:
        print(message, file=sys.stderr)
        return
    script = ('on run argv\n'
              'display alert "本机路由管理" message (item 1 of argv) as critical\n'
              'end run')
    subprocess.run(['/usr/bin/osascript', '-e', script, message], capture_output=True)


def main():
    # Dispatch before importing FastAPI or initializing user data. The authorized
    # child executes one fixed operation and never starts a root web server.
    if FROZEN and len(sys.argv) > 1 and sys.argv[1] == '--admin-helper':
        from privileged import main as helper
        helper(sys.argv[2] if len(sys.argv) == 3 else '')
        return 0

    parser = argparse.ArgumentParser(description='本机 IPv4 路由管理')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--data-dir', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise RuntimeError('此项目管理 macOS 路由，只能在 Mac 上运行。')
    if os.geteuid() == 0:
        raise RuntimeError('请以普通用户启动；修改路由时会单独请求管理员授权。')
    if not 1024 <= args.port <= 65535:
        raise RuntimeError('端口必须在 1024～65535 之间。')
    if already_running(args.port):
        if not args.no_browser:
            webbrowser.open(f'http://127.0.0.1:{args.port}')
        return 0

    # Reserve the socket before loading configuration, so bind failures are
    # visible from Finder and never alter an existing instance's data.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', args.port))
    except OSError:
        sock.close()
        raise RuntimeError(f'端口 {args.port} 已被占用。请先在原网页点击“关闭后台”，再打开本程序。')

    directory = args.data_dir or (APP_DIRECTORY if FROZEN else None)
    if FROZEN:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_directory = (directory or APP_DIRECTORY) / 'logs'
        log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(log_directory / 'app.log', maxBytes=1_000_000,
                                      backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)

    import uvicorn
    from app import create_app
    server = None

    def stop():
        server.should_exit = True

    app = None
    try:
        app = create_app(port=args.port, directory=directory, shutdown=stop)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port,
                                               workers=1, access_log=False,
                                               loop='asyncio', http='h11', ws='none',
                                               log_config=None if FROZEN else uvicorn.config.LOGGING_CONFIG))
        if not args.no_browser:
            def open_when_ready():
                import time
                for _ in range(100):
                    if server.started:
                        webbrowser.open(f'http://127.0.0.1:{args.port}')
                        return
                    if server.should_exit:
                        return
                    time.sleep(.1)
            threading.Thread(target=open_when_ready, daemon=True).start()
        if not FROZEN:
            print(f'本机路由管理：http://127.0.0.1:{args.port}', flush=True)
            print('点击网页右上角“关闭后台”或在终端按 Ctrl+C 停止。', flush=True)
        server.run(sockets=[sock])
    finally:
        sock.close()
        if app is not None:
            app.state.store.process_lock.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        logging.getLogger('route-panel').error('启动失败', exc_info=True)
        if not FROZEN:
            traceback.print_exc()
        report_error(str(error))
        raise SystemExit(1)
