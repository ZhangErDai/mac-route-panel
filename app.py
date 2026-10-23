from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import hosts as hostsfile
import routing

ROOT = Path(__file__).resolve().parent


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'routes.json'
        self.lock = threading.RLock()
        self.process_lock = (directory / 'instance.lock').open('a+')
        try:
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('项目后台已经在运行，请打开现有网页。')
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                if self.data.get('version') != 1 or not isinstance(self.data.get('records'), dict):
                    raise ValueError('不支持的配置格式')
            except (ValueError, OSError) as e:
                raise RuntimeError(f'本地配置无法读取，未覆盖原文件：{e}') from e
        else:
            self.data = dict(version=1, records={}, pending=None, last_error=None)
            self.save()

    def save(self):
        with self.lock:
            fd, name = tempfile.mkstemp(prefix='.routes-', dir=self.directory)
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(self.data, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(name, self.path)
                dfd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            finally:
                if os.path.exists(name):
                    os.unlink(name)


def admin_apply(plan: dict, directory: Path) -> dict:
    fd, name = tempfile.mkstemp(prefix='operation-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(plan, file)
        if getattr(sys, 'frozen', False):
            command = shlex.join(['/usr/bin/env', 'PYINSTALLER_RESET_ENVIRONMENT=1',
                                  sys.executable, '--admin-helper', name])
        else:
            command = shlex.join([sys.executable, '-I', str(ROOT / 'privileged.py'), name])
        script = 'on run argv\nreturn do shell script (item 1 of argv) with administrator privileges\nend run'
        # No timeout: forcibly killing a helper midway could leave a half-applied operation.
        # The visible UI stays busy until the user authorizes/cancels and the helper completes.
        result = subprocess.run(['/usr/bin/osascript', '-e', script, command], capture_output=True, text=True)
        if result.returncode:
            error = result.stderr.strip()
            if '-128' in error or 'canceled' in error.lower():
                raise routing.RouteError('已取消管理员授权，未执行路由修改。')
            logging.getLogger('route-panel').warning('管理员助手授权失败：%s', error)
            if '-60008' in error or 'Connection Invalid' in error or 'Connection invalid' in error:
                raise routing.RouteError('macOS 未能完成管理员授权。请关闭当前后台，通过本机终端或双击启动.command 启动后重试。')
            raise routing.RouteError('macOS 管理员授权或执行失败：' + (error.splitlines()[-1] if error else '未返回错误说明'))
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise routing.RouteError('管理员助手没有返回可核对的结果，请刷新检查系统路由。')
        if not data.get('ok'):
            raise routing.RouteError(data.get('message', '路由操作失败。'))
        return data
    finally:
        Path(name).unlink(missing_ok=True)


def create_app(port=8765, directory=None, shutdown=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = Store(Path(directory) if directory else ROOT / 'data')
    token = secrets.token_urlsafe(32)
    mutation = threading.Lock()
    plans = {}
    host_plans = {}
    lifecycle = {'stopping': False}
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    def snapshot():
        snap = routing.system()
        with store.lock:
            rows = routing.enriched(snap, copy.deepcopy(store.data['records']))
        return snap, rows

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        host = request.headers.get('host', '')
        if host not in hosts or (request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient')):
            return JSONResponse({'error': '仅允许本机访问。'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error': '不接受跨站请求。'}, status_code=403)
        if request.url.path.startswith('/api/'):
            if not secrets.compare_digest(request.headers.get('x-session-token', ''), token):
                return JSONResponse({'error': '会话失效，请重新打开页面。'}, status_code=403)
            if request.method != 'GET':
                if request.headers.get('origin') != f'http://{host}':
                    return JSONResponse({'error': '不接受外部来源的修改请求。'}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.exception_handler(routing.RouteError)
    async def route_error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=409)

    @app.exception_handler(hostsfile.HostsError)
    async def hosts_error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=409)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger('route-panel').error('后台操作异常', exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse({'error': '后台执行异常，请查看启动终端并刷新检查实际路由。'}, status_code=500)

    @app.get('/', response_class=HTMLResponse)
    def home():
        return (ROOT / 'static/index.html').read_text().replace('__SESSION_TOKEN__', token)

    @app.get('/health')
    def health():
        return dict(application='local-route-panel', version=1, stopping=lifecycle['stopping'])

    @app.get('/api/routes')
    def get_routes():
        snap, rows = snapshot()
        warnings = []
        if store.data.get('pending'):
            warnings.append('上次操作未记录完整结果，以下为当前实际路由；请检查后再操作。')
        if store.data.get('last_error'):
            message = store.data['last_error']
            if '-60008' in message or 'Connection invalid' in message:
                message = '上次 macOS 管理员授权失败。请通过本机终端或双击启动.command 启动后台后重试。'
            warnings.append(message)
        return dict(routes=rows, interfaces=list(snap['interfaces'].values()), warnings=warnings,
                    busy=mutation.locked(), stopping=lifecycle['stopping'], updated=time.time())

    @app.get('/api/hosts')
    def get_hosts():
        text = hostsfile.read()
        managed, external = hostsfile.parse(text)
        return dict(managed=managed,
                    external=[dict(address=e['address'], domains=e['domains'], system=e['system'],
                                   block=e['address'] in (hostsfile.BLOCK, '::'))
                              for e in external],
                    busy=mutation.locked(), stopping=lifecycle['stopping'], updated=time.time())

    async def body(request):
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise routing.RouteError('请求必须为 JSON。')
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 8192:
                raise routing.RouteError('请求过大。')
        try:
            data = json.loads(content)
        except ValueError:
            raise routing.RouteError('JSON 格式不正确。')
        if not isinstance(data, dict):
            raise routing.RouteError('请求格式不正确。')
        return data

    def prepare(data):
        if mutation.locked() or lifecycle['stopping']:
            raise routing.RouteError('后台正在处理其他操作，请稍后再试。')
        snap, rows = snapshot()
        action = data.get('action')
        if action == 'add':
            r = routing.new_route(data, snap)
        else:
            r = next((row for row in rows if row['id'] == data.get('id')), None)
            if not r:
                raise routing.RouteError('路由状态已改变，请刷新。')
        p = routing.plan(action, r, snap)
        pid = secrets.token_urlsafe(24)
        now = time.monotonic()
        for old in list(plans):
            if plans[old]['expires'] < now:
                del plans[old]
        plans[pid] = dict(plan=p, expires=now + 180)
        return dict(id=pid, action=action, route=r, commands=p['commands'], persistent=bool(r.get('service_id')),
                    warning='隧道软件可能自动恢复这条路由，刷新时会显示实际状态。' if r['interface'].startswith('utun') else '')

    @app.post('/api/preview')
    async def preview(request: Request):
        data = await body(request)
        # Snapshot reads are blocking system calls; keep the event loop responsive.
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(prepare, data)

    def apply(pid):
        if not mutation.acquire(blocking=False):
            raise routing.RouteError('另一条路由正在修改，请等待操作完成。')
        try:
            if lifecycle['stopping']:
                raise routing.RouteError('后台正在关闭。')
            saved = plans.pop(pid, None)
            if not saved or saved['expires'] < time.monotonic():
                raise routing.RouteError('操作预览已过期，请重新预览。')
            p = saved['plan']
            r, action = p['route'], p['action']
            snap, rows = snapshot()
            if action == 'add':
                fresh_r = routing.new_route(dict(destination=r['cidr'], mode=r['mode'], gateway=r['gateway'], interface=r['interface']), snap)
                if fresh_r['interface'] != r['interface']:
                    raise routing.RouteError('出口接口已改变，请重新预览。')
            fresh = routing.plan(action, r, snap)
            if fresh['was_live'] != p['was_live'] or fresh['service_before'] != p['service_before']:
                raise routing.RouteError('系统配置已改变，请刷新并重新预览。')
            with store.lock:
                baseline = copy.deepcopy(store.data)
                # Preserve recovery information before opening the admin authorization dialog.
                store.data['records'].setdefault(r['id'], dict(route=r, enabled=action != 'add'))
                store.data['pending'] = dict(action=action, route=r, time=time.time())
                store.save()
            try:
                result = admin_apply(p, store.directory)
            except Exception as e:
                with store.lock:
                    store.data = baseline
                    # Keep a recovery definition even if an administrator command
                    # partly failed and the original system entry is now missing.
                    store.data['records'].setdefault(r['id'], dict(route=r, enabled=bool(r.get('enabled', False))))
                    store.data['last_error'] = str(e)
                    store.save()
                raise
            with store.lock:
                actual = result['route']
                actual.update(source=r.get('source', 'local'), service_id=r.get('service_id'), service_name=r.get('service_name'))
                actual['id'] = routing.route_id(actual)
                store.data['records'].pop(r['id'], None)
                store.data['records'][actual['id']] = dict(route=actual, enabled=action != 'disable')
                store.data['pending'] = None
                store.data['last_error'] = None
                store.save()
            return result
        finally:
            mutation.release()

    @app.post('/api/apply')
    async def apply_route(request: Request):
        data = await body(request)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(apply, str(data.get('id', '')))

    def prepare_hosts(data):
        if mutation.locked() or lifecycle['stopping']:
            raise routing.RouteError('后台正在处理其他操作，请稍后再试。')
        p = hostsfile.plan(data, hostsfile.read(), store.directory / 'hosts-backups')
        pid = secrets.token_urlsafe(24)
        now = time.monotonic()
        for old in list(host_plans):
            if host_plans[old]['expires'] < now:
                del host_plans[old]
        host_plans[pid] = dict(plan=p, expires=now + 180)
        return dict(id=pid, action=p['action'], domain=p['domain'], rule=p['rule'],
                    commands=p['commands'], adopted=p['adopted'],
                    warning='本次会修改 /etc/hosts 并刷新 DNS 缓存，仅影响本机解析。')

    def apply_hosts(pid):
        if not mutation.acquire(blocking=False):
            raise routing.RouteError('另一个操作正在执行，请等待完成。')
        try:
            if lifecycle['stopping']:
                raise routing.RouteError('后台正在关闭。')
            saved = host_plans.pop(pid, None)
            if not saved or saved['expires'] < time.monotonic():
                raise routing.RouteError('操作预览已过期，请重新预览。')
            return admin_apply(saved['plan'], store.directory)
        finally:
            mutation.release()

    @app.post('/api/hosts/preview')
    async def preview_hosts(request: Request):
        data = await body(request)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(prepare_hosts, data)

    @app.post('/api/hosts/apply')
    async def apply_host_rule(request: Request):
        data = await body(request)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(apply_hosts, str(data.get('id', '')))

    @app.post('/api/shutdown')
    async def stop(request: Request):
        await body(request)
        if not mutation.acquire(blocking=False):
            raise routing.RouteError('路由操作尚未完成，暂时不能关闭后台。')
        try:
            if not shutdown:
                raise routing.RouteError('请通过启动.command 或 run.py 启动，以使用关闭后台功能。')
            store.save()
            lifecycle['stopping'] = True
            timer = threading.Timer(.5, shutdown)
            timer.daemon = True
            timer.start()
            return dict(ok=True, message='后台正在关闭，系统路由保持不变。')
        finally:
            mutation.release()

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    app.state.store = store
    return app
