import copy
import http.client
import json
import re
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn
import app
import routing
from test_routing import fixture


class AuthorizationTests(unittest.TestCase):
    def test_denied_environment_has_short_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            denied=SimpleNamespace(returncode=1,stdout='',stderr='noise\n19:81: execution error: 授权失败。 (-60008)')
            with patch('app.subprocess.run',return_value=denied),self.assertLogs('route-panel',level='WARNING'):
                with self.assertRaisesRegex(routing.RouteError,'启动.command'):
                    app.admin_apply({'action':'disable'},Path(directory))
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_success_uses_one_quoted_command_and_cleans_request(self):
        with tempfile.TemporaryDirectory(prefix='路由 管理 ') as directory:
            def execute(args,**kwargs):
                self.assertEqual(args[0],'/usr/bin/osascript')
                import shlex
                command=shlex.split(args[-1])
                self.assertEqual(command[1],'-I')
                request=Path(command[-1])
                self.assertEqual(json.loads(request.read_text())['action'],'disable')
                return SimpleNamespace(returncode=0,stderr='',stdout=json.dumps({'ok':True,'message':'成功'}))
            with patch('app.subprocess.run',side_effect=execute):
                self.assertTrue(app.admin_apply({'action':'disable'},Path(directory))['ok'])
            self.assertEqual(list(Path(directory).iterdir()),[])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state=fixture()
        cls.system_patch=patch('routing.system',side_effect=lambda:copy.deepcopy(cls.state))
        cls.system_patch.start()
        cls.directory=tempfile.TemporaryDirectory()
        cls.sock=socket.socket();cls.sock.bind(('127.0.0.1',0));cls.port=cls.sock.getsockname()[1]
        def shutdown(): cls.server.should_exit=True
        cls.webapp=app.create_app(cls.port,Path(cls.directory.name),shutdown)
        cls.server=uvicorn.Server(uvicorn.Config(cls.webapp,log_level='error',access_log=False,timeout_graceful_shutdown=2))
        cls.thread=threading.Thread(target=lambda:cls.server.run(sockets=[cls.sock]),daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started: break
            time.sleep(.02)
        if not cls.server.started: raise RuntimeError('测试服务器未能启动')
        conn=http.client.HTTPConnection('127.0.0.1',cls.port,timeout=5)
        conn.request('GET','/')
        page=conn.getresponse().read().decode();conn.close()
        cls.token=re.search(r'name="session-token" content="([^"]+)"',page)[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit=True;cls.thread.join(5)
        cls.webapp.state.store.process_lock.close()
        cls.system_patch.stop();cls.directory.cleanup()

    def setUp(self):
        type(self).state=fixture()
        self.webapp.state.store.data=dict(version=1,records={},pending=None,last_error=None)
        self.webapp.state.store.save()

    def request(self,path,data=None,headers=None):
        h={'X-Session-Token':self.token,'Origin':f'http://127.0.0.1:{self.port}'}
        if headers: h.update(headers)
        body=None
        if data is not None:
            h['Content-Type']='application/json';body=json.dumps(data)
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        conn.request('GET' if data is None else 'POST',path,body,h)
        response=conn.getresponse();status=response.status;result=json.loads(response.read());conn.close()
        return status,result

    def test_routes_are_real_snapshot_and_neighbours_excluded(self):
        code,data=self.request('/api/routes')
        self.assertEqual(code,200)
        self.assertEqual(sum(r['source']=='saved' for r in data['routes']),2)
        self.assertNotIn('192.168.1.87/32',[r['cidr'] for r in data['routes']])

    def test_cross_origin_and_missing_token_are_rejected(self):
        for headers in ({'Origin':'http://evil.example'},{'X-Session-Token':''},{'Host':'evil.example'}):
            code,data=self.request('/api/preview',{'action':'add'},headers)
            self.assertEqual(code,403)

    def test_preview_does_not_execute_and_injection_is_rejected(self):
        with patch('app.admin_apply') as admin:
            code,data=self.request('/api/preview',dict(action='add',destination='198.18.4.136',mode='gateway',gateway='198.18.0.204'))
            self.assertEqual(code,200);self.assertIn('-host 198.18.4.136 198.18.0.204',data['commands'][0]);admin.assert_not_called()
            code,data=self.request('/api/preview',dict(action='add',destination='198.18.4.136;id',mode='gateway',gateway='198.18.0.204'))
            self.assertEqual(code,409)

    def test_mocked_disable_and_enable_preserve_recovery_record(self):
        row=next(r for r in routing.enriched(self.state,{}) if r['cidr']=='192.168.1.0/24')
        def admin(p,directory):
            self.assertTrue(self.webapp.state.store.data['pending'])
            self.assertIn(row['id'],self.webapp.state.store.data['records'])
            if p['action']=='disable': type(self).state['routes']=[r for r in self.state['routes'] if r['id']!=row['id']]
            else: type(self).state['routes'].append(copy.deepcopy(row))
            return dict(ok=True,route=p['route'],live=p['action']!='disable',message='模拟操作已完成')
        with patch('app.admin_apply',side_effect=admin):
            for action in ('disable','enable'):
                code,p=self.request('/api/preview',dict(action=action,id=row['id']));self.assertEqual(code,200)
                code,result=self.request('/api/apply',dict(id=p['id']));self.assertEqual(code,200)
                code,data=self.request('/api/routes')
                current=next(r for r in data['routes'] if r['id']==row['id'])
                self.assertEqual(current['enabled'],action=='enable')
        saved=json.loads((Path(self.directory.name)/'routes.json').read_text())
        self.assertIsNone(saved['pending'])
        self.assertIn(row['id'],saved['records'])

    def test_authorization_cancel_keeps_route_on(self):
        row=next(r for r in routing.enriched(self.state,{}) if r['cidr']=='192.168.1.0/24')
        code,p=self.request('/api/preview',dict(action='disable',id=row['id']))
        with patch('app.admin_apply',side_effect=routing.RouteError('已取消管理员授权')):
            code,result=self.request('/api/apply',dict(id=p['id']))
        self.assertEqual(code,409)
        code,data=self.request('/api/routes')
        self.assertTrue(next(r for r in data['routes'] if r['id']==row['id'])['enabled'])

    def test_shutdown_is_blocked_during_admin_operation(self):
        row=next(r for r in routing.enriched(self.state,{}) if r['cidr']=='192.168.1.0/24')
        code,p=self.request('/api/preview',dict(action='disable',id=row['id']))
        entered=threading.Event();release=threading.Event();result=[]
        def waiting_admin(plan,directory):
            entered.set();release.wait(4)
            raise routing.RouteError('模拟取消授权')
        with patch('app.admin_apply',side_effect=waiting_admin):
            worker=threading.Thread(target=lambda:result.append(self.request('/api/apply',dict(id=p['id']))))
            worker.start()
            self.assertTrue(entered.wait(2))
            try:
                code,data=self.request('/api/shutdown',{})
                self.assertEqual(code,409)
                self.assertIn('尚未完成',data['error'])
                self.assertFalse(self.server.should_exit)
            finally:
                release.set();worker.join(4)
            self.assertEqual(result[0][0],409)

    def test_hosts_snapshot_lists_managed_and_external_entries(self):
        text = '127.0.0.1 localhost\n0.0.0.0 example.test\n'
        with patch('hosts.read',return_value=text):
            code,data=self.request('/api/hosts')
        self.assertEqual(code,200)
        self.assertEqual(data['managed'],[])
        localhost=next(e for e in data['external'] if e['domains']==['localhost'])
        blocked=next(e for e in data['external'] if e['domains']==['example.test'])
        self.assertTrue(localhost['system'])
        self.assertFalse(blocked['system'])
        self.assertTrue(blocked['block'])

    def test_hosts_preview_and_apply_go_through_one_admin_helper(self):
        text = '127.0.0.1 localhost\n'
        with patch('hosts.read',return_value=text):
            code,plan=self.request('/api/hosts/preview',dict(action='block',domain='dl.pstmn.io'))
            self.assertEqual(code,200)
            self.assertEqual(plan['rule']['address'],'0.0.0.0')
            self.assertIn('0.0.0.0 dl.pstmn.io',plan['commands'])
            with patch('app.admin_apply',return_value=dict(ok=True,message='模拟完成')) as admin:
                code,result=self.request('/api/hosts/apply',dict(id=plan['id']))
            self.assertEqual(code,200)
            self.assertEqual(result['message'],'模拟完成')
            self.assertEqual(admin.call_args[0][0]['kind'],'hosts')
            code,error=self.request('/api/hosts/apply',dict(id=plan['id']))
            self.assertEqual(code,409)

    def test_z_shutdown_returns_then_exits_and_releases_port(self):
        before=copy.deepcopy(self.state)
        code,data=self.request('/api/shutdown',{})
        self.assertEqual(code,200);self.assertTrue(data['ok'])
        self.thread.join(5);self.assertFalse(self.thread.is_alive())
        self.assertEqual(before,self.state)
        sock=socket.socket();self.assertNotEqual(sock.connect_ex(('127.0.0.1',self.port)),0);sock.close()


if __name__=='__main__': unittest.main()
