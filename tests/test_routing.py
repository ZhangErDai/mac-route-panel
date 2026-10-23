import copy
import unittest
from unittest.mock import patch

import routing

TABLE = '''Routing tables
Internet:
Destination Gateway Flags Netif Expire
default 198.18.0.1 UGScg en0
198.18.0/22 link#12 UCS en0 !
198.18.4 198.18.8.254 UGSc en5
10.20/16 198.18.8.254 UGSc en5
100.64/10 link#19 UCS utun1
192.168.1 link#19 UCS utun1
192.168.1.87 link#19 UHWIi utun1
198.18.0.25 aa:bb:cc:dd:ee:ff UHLWI en0
127 127.0.0.1 UCS lo0
'''
IFCONFIG = '''en0: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 198.18.0.212 netmask 0xfffffc00 broadcast 198.18.3.255
    status: active
en5: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 198.18.8.52 netmask 0xfffffc00 broadcast 198.18.11.255
    status: active
utun1: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
    inet 100.64.0.1 --> 100.64.0.1 netmask 0xffffffff
'''


def fixture():
    return dict(routes=routing.parse_routes(TABLE), interfaces=routing.parse_interfaces(IFCONFIG),
                services={'svc':dict(id='svc', name='USB LAN', interface='en5', routes=[
                    ['198.18.4.0','255.255.255.0','198.18.8.254'],
                    ['10.20.0.0','255.255.0.0','198.18.8.254']])})


class RoutingTests(unittest.TestCase):
    def test_abbreviated_networks_and_cache_exclusion(self):
        rows = routing.parse_routes(TABLE)
        self.assertIn('198.18.4.0/24', [r['cidr'] for r in rows])
        self.assertIn('100.64.0.0/10', [r['cidr'] for r in rows])
        self.assertNotIn('192.168.1.87/32', [r['cidr'] for r in rows])
        self.assertNotIn('198.18.0.25/32', [r['cidr'] for r in rows])
        self.assertFalse(next(r for r in rows if r['cidr']=='0.0.0.0/0')['editable'])

    def test_host_route_and_injection_validation(self):
        r = routing.new_route(dict(destination='198.18.4.136',mode='gateway',gateway='198.18.0.204'), fixture())
        self.assertEqual(r['interface'],'en0')
        self.assertEqual(routing.route_command('add',r), ['/sbin/route','-n','add','-host','198.18.4.136','198.18.0.204'])
        for payload in ('198.18.4.136; touch /tmp/no', '0.0.0.0/0', '127.0.0.1', '224.0.0.1'):
            with self.assertRaises(routing.RouteError): routing.network(payload)
        with self.assertRaises(routing.RouteError): routing.gateway('1.2.3.4; id')
        with self.assertRaises(routing.RouteError): routing.route_command('flush',r)
        r['interface']='en0; id'
        with self.assertRaises(routing.RouteError): routing.route_command('add',r)

    def test_gateway_can_be_reached_by_tunnel_prefix(self):
        r = routing.new_route(dict(destination='192.168.32.0/24',mode='gateway',gateway='100.64.0.3'),fixture())
        self.assertEqual(r['interface'],'utun1')

    def test_reject_duplicate_and_own_gateway(self):
        for data in (dict(destination='198.18.4.0/24',mode='gateway',gateway='198.18.0.204'),
                     dict(destination='198.18.4.136',mode='gateway',gateway='198.18.0.212')):
            with self.assertRaises(routing.RouteError): routing.new_route(data,fixture())

    def test_saved_config_missing_is_not_disabled(self):
        s=fixture();s['routes']=[r for r in s['routes'] if r['cidr']!='198.18.4.0/24']
        row=next(r for r in routing.enriched(s,{}) if r['cidr']=='198.18.4.0/24')
        self.assertTrue(row['enabled']);self.assertFalse(row['live']);self.assertEqual(row['status'],'missing')

    def test_disabled_route_is_preserved_and_restoration_detected(self):
        s=fixture();r=next(r for r in routing.enriched(s,{}) if r['cidr']=='192.168.1.0/24')
        records={r['id']:dict(route=r,enabled=False)}
        self.assertEqual(next(x for x in routing.enriched(s,records) if x['id']==r['id'])['status'],'restored')
        s['routes']=[x for x in s['routes'] if x['id']!=r['id']]
        closed=next(x for x in routing.enriched(s,records) if x['id']==r['id'])
        self.assertFalse(closed['enabled']);self.assertEqual(closed['status'],'disabled')

    def test_scoped_route_command(self):
        r=routing.new_route(dict(destination='198.18.4.136',mode='gateway',gateway='198.18.0.204'),fixture())
        r['scope']=True
        self.assertEqual(routing.route_command('delete',r)[4:7],['-ifscope','en0','198.18.4.136'])

    def execute_fake(self, action='disable', fail_delete=False, disconnected=False, add_race=False):
        state=fixture()
        r=next(r for r in routing.enriched(state,{}) if r['cidr']=='198.18.4.0/24')
        if action=='enable':
            state['routes']=[x for x in state['routes'] if x['id']!=r['id']]
            state['services']['svc']['routes']=state['services']['svc']['routes'][1:]
        if disconnected: state['interfaces']['en5']['up']=False
        p=routing.plan(action,r,state)
        original=copy.deepcopy(state)
        commands=[]
        def fake_run(args,timeout=15):
            commands.append(args)
            if args[0].endswith('networksetup'):
                state['services']['svc']['routes']=[args[i:i+3] for i in range(3,len(args),3)]
            elif args[2]=='delete':
                if fail_delete: raise routing.RouteError('模拟删除失败')
                state['routes']=[x for x in state['routes'] if x['id']!=r['id']]
            elif args[2]=='add':
                state['routes'].append(copy.deepcopy(r))
                if add_race: raise routing.RouteError('File exists')
            return ''
        with patch('routing.system',side_effect=lambda:copy.deepcopy(state)),patch('routing.services',side_effect=lambda:copy.deepcopy(state['services'])),patch('routing.run',side_effect=fake_run),patch('routing.time.sleep'):
            if fail_delete:
                with self.assertRaisesRegex(routing.RouteError,'已核对并恢复'):
                    routing.execute(p)
                self.assertEqual(state,original)
                return
            result=routing.execute(p)
        self.assertTrue(result['ok'])
        self.assertIn(['10.20.0.0','255.255.0.0','198.18.8.254'],state['services']['svc']['routes'])
        return state,result,commands

    def test_saved_disable_preserves_other_routes(self):
        state,result,commands=self.execute_fake()
        self.assertEqual(len(state['services']['svc']['routes']),1)
        self.assertFalse(result['live'])
        self.assertEqual(commands[0][0],'/usr/sbin/networksetup')

    def test_saved_reenable(self):
        state,result,commands=self.execute_fake('enable')
        self.assertEqual(len(state['services']['svc']['routes']),2)
        self.assertTrue(result['live'])

    def test_network_service_add_race_is_verified(self):
        state,result,commands=self.execute_fake('enable',add_race=True)
        self.assertTrue(result['live'])
        self.assertEqual(len(state['services']['svc']['routes']),2)

    def test_enable_disconnected_interface_keeps_config_without_false_live(self):
        state,result,commands=self.execute_fake('enable',disconnected=True)
        self.assertFalse(result['live'])
        self.assertIn('接口未连接',result['message'])
        self.assertFalse(any(c[0]=='/sbin/route' for c in commands))

    def test_partial_failure_restores_original_config(self):
        self.execute_fake(fail_delete=True)

    def test_stale_authorization_does_not_write(self):
        s=fixture();r=next(r for r in routing.enriched(s,{}) if r['cidr']=='198.18.4.0/24')
        p=routing.plan('disable',r,s)
        s['services']['svc']['routes'].append(['10.2.0.0','255.255.0.0','198.18.8.254'])
        with patch('routing.system',return_value=s),patch('routing.run') as command:
            with self.assertRaisesRegex(routing.RouteError,'已改变'): routing.execute(p)
            command.assert_not_called()


if __name__=='__main__': unittest.main()
