import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hosts


BASE = '''##
# Host Database
##
127.0.0.1\tlocalhost
255.255.255.255\tbroadcasthost
::1             localhost
192.0.2.10      intranet.example
'''


class ParseTests(unittest.TestCase):
    def test_reads_managed_block_and_keeps_external_entries(self):
        text = hosts.render(BASE, [dict(domain='dl.pstmn.io', address='0.0.0.0', kind='block')])
        managed, external = hosts.parse(text)
        self.assertEqual(managed, [dict(domain='dl.pstmn.io', address='0.0.0.0',
                                        kind='block', protected=False)])
        self.assertIn(dict(domain='dl.pstmn.io', address='0.0.0.0', kind='block', protected=False),
                      managed)
        entry = next(e for e in external if e['address'] == '192.0.2.10')
        self.assertEqual(entry['domains'], ['intranet.example'])
        self.assertEqual(entry['raw'], '192.0.2.10      intranet.example')
        self.assertFalse(entry['system'])
        self.assertEqual(len(entry['id']), 12)

    def test_system_entries_are_marked_read_only(self):
        _, external = hosts.parse(BASE)
        flagged = {e['address']: e['system'] for e in external}
        self.assertTrue(flagged['127.0.0.1'])
        self.assertTrue(flagged['::1'])
        self.assertTrue(flagged['255.255.255.255'])
        self.assertFalse(flagged['192.0.2.10'])
        for address in ('127.0.0.1', '::1', '255.255.255.255', 'fe80::1'):
            self.assertTrue(hosts.system_address(address), address)
        for address in ('0.0.0.0', '::', '192.0.2.10'):
            self.assertFalse(hosts.system_address(address), address)
        for address in ('0.0.0.0',):
            self.assertFalse(hosts.protected(address), address)
        for address in ('127.0.0.1', '127.0.0.2', '169.254.1.1', '224.0.0.1',
                        '255.255.255.255', '::1'):
            self.assertTrue(hosts.protected(address), address)

    def test_blocking_entry_outside_managed_block_is_not_system(self):
        text = BASE + '0.0.0.0 dl.pstmn.io\n'
        _, external = hosts.parse(text)
        entry = external[-1]
        self.assertFalse(entry['system'], '用户自己写的屏蔽条目不应被标记为系统条目')
        self.assertEqual(entry['address'], '0.0.0.0')
        self.assertEqual(entry['domains'], ['dl.pstmn.io'])

    def test_protected_managed_entry_cannot_be_changed(self):
        text = BASE + hosts.BEGIN + '\n127.0.0.1 keepme.internal\n' + hosts.END + '\n'
        managed, _ = hosts.parse(text)
        self.assertTrue(managed[0]['protected'])
        for data in (dict(action='remove', domain='keepme.internal'),
                     dict(action='block', domain='keepme.internal'),
                     dict(action='pin', domain='keepme.internal', address='192.0.2.9')):
            with self.assertRaises(hosts.HostsError):
                hosts.plan(data, text)

    def test_unbalanced_markers_are_refused(self):
        for text in (hosts.BEGIN + '\n0.0.0.0 a.example\n', '0.0.0.0 a.example\n' + hosts.END + '\n'):
            with self.assertRaises(hosts.HostsError):
                hosts.parse(text)

    def test_domain_and_address_validation(self):
        for bad in ('', 'exa mple.com', '-bad.com', 'http://x.com', 'a' * 300, 'localhost'):
            with self.assertRaises(hosts.HostsError):
                hosts.name(bad)
        for bad in ('', '0.0.0.0', '127.0.0.1', '224.0.0.1', 'not-an-ip'):
            with self.assertRaises(hosts.HostsError):
                hosts.fixed(bad)
        self.assertEqual(hosts.name('DL.Pstmn.IO.'), 'dl.pstmn.io')
        self.assertEqual(hosts.fixed('192.0.2.30'), '192.0.2.30')


class RenderTests(unittest.TestCase):
    def test_only_the_managed_block_changes(self):
        text = hosts.render(BASE, [dict(domain='a.example', address='0.0.0.0', kind='block')])
        updated = hosts.render(text, [dict(domain='b.example', address='192.0.2.5', kind='pin')])
        before = [line for line in text.splitlines() if line and not line.startswith('# >>>')]
        after = [line for line in updated.splitlines() if line and not line.startswith('# >>>')]
        outside = lambda lines: [l for l in lines if 'a.example' not in l and 'b.example' not in l
                                 and l not in (hosts.BEGIN, hosts.END)]
        self.assertEqual(outside(before), outside(after))
        self.assertIn('192.0.2.5 b.example', updated)

    def test_block_is_appended_once_and_is_idempotent(self):
        once = hosts.render(BASE, [dict(domain='a.example', address='0.0.0.0', kind='block')])
        twice = hosts.render(once, [dict(domain='a.example', address='0.0.0.0', kind='block')])
        self.assertEqual(once, twice)
        self.assertEqual(once.count(hosts.BEGIN), 1)

    def test_empty_rule_list_keeps_markers(self):
        text = hosts.render(BASE, [])
        self.assertIn(hosts.BEGIN, text)
        self.assertIn(hosts.END, text)
        self.assertEqual(hosts.parse(text)[0], [])


class PlanTests(unittest.TestCase):
    def plan(self, text, **data):
        return hosts.plan(dict(data), text, None)

    def test_block_and_remove(self):
        p = self.plan(BASE, action='block', domain='dl.pstmn.io')
        self.assertEqual(p['rule'], dict(domain='dl.pstmn.io', address='0.0.0.0', kind='block'))
        self.assertFalse(p['adopted'])
        self.assertIn('0.0.0.0 dl.pstmn.io', p['text_after'])
        q = self.plan(p['text_after'], action='remove', domain='dl.pstmn.io')
        self.assertEqual(q['after'], [])

    def test_remove_unknown_domain_is_refused(self):
        with self.assertRaises(hosts.HostsError):
            self.plan(BASE, action='remove', domain='missing.example')

    def test_conflicting_external_entry_is_refused(self):
        with self.assertRaises(hosts.HostsError):
            self.plan(BASE, action='pin', domain='intranet.example', address='192.0.2.99')

    def test_exact_external_entry_is_adopted(self):
        text = BASE.replace('192.0.2.10      intranet.example', '0.0.0.0 intranet.example')
        p = self.plan(text, action='block', domain='intranet.example')
        self.assertTrue(p['adopted'])
        self.assertNotIn('\n0.0.0.0 intranet.example', p['text_after'].split(hosts.BEGIN)[0])
        self.assertIn('迁移原有行', p['commands'][0])

    def test_partial_external_match_is_not_adopted(self):
        text = BASE.replace('192.0.2.10      intranet.example', '0.0.0.0 intranet.example other.example')
        with self.assertRaises(hosts.HostsError):
            self.plan(text, action='block', domain='intranet.example')


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'hosts'
        self.path.write_text(BASE)
        self.path.chmod(0o644)
        self.flushed = 0

    def tearDown(self):
        self.directory.cleanup()

    def flush(self):
        self.flushed += 1

    def run_plan(self, **data):
        p = hosts.plan(dict(data), self.path.read_text(), Path(self.directory.name) / 'backups')
        return hosts.execute(p, path=self.path, flush=self.flush)

    def test_writes_rules_and_flushes_cache(self):
        result = self.run_plan(action='block', domain='dl.pstmn.io')
        self.assertTrue(result['ok'])
        self.assertTrue(result['changed'])
        self.assertEqual(self.flushed, 1)
        managed, external = hosts.parse(self.path.read_text())
        self.assertEqual([r['domain'] for r in managed], ['dl.pstmn.io'])
        self.assertTrue(any(e['domains'] == ['localhost'] for e in external))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
        backups = list((Path(self.directory.name) / 'backups').glob('hosts-*.txt'))
        self.assertEqual(len(backups), 1)
        self.assertIn('intranet.example', backups[0].read_text())

    def test_repeat_apply_is_a_no_op(self):
        self.run_plan(action='block', domain='dl.pstmn.io')
        result = self.run_plan(action='block', domain='dl.pstmn.io')
        self.assertFalse(result['changed'])
        self.assertEqual(self.flushed, 1)

    def test_file_changed_after_preview_is_refused(self):
        p = hosts.plan(dict(action='block', domain='dl.pstmn.io'), self.path.read_text(), None)
        with self.path.open('a') as file:
            file.write('192.0.2.77 changed.example\n')
        before = self.path.read_text()
        with self.assertRaises(hosts.HostsError):
            hosts.execute(p, path=self.path, flush=self.flush)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.flushed, 0)

    def test_write_failure_restores_original(self):
        p = hosts.plan(dict(action='block', domain='dl.pstmn.io'), self.path.read_text(), None)
        with patch('hosts._write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                hosts.execute(p, path=self.path, flush=self.flush)
        self.assertNotIn('dl.pstmn.io', self.path.read_text())

    def test_unexpected_rewrite_is_detected_and_reverted(self):
        p = hosts.plan(dict(action='block', domain='dl.pstmn.io'), self.path.read_text(), None)
        real = hosts._write
        calls = []
        def tampered(text, path=hosts.HOSTS):
            calls.append(1)
            real(text + '203.0.113.9 injected.example\n' if len(calls) == 1 else text, path)
        with patch('hosts._write', side_effect=tampered):
            with self.assertRaises(hosts.HostsError):
                hosts.execute(p, path=self.path, flush=self.flush)
        self.assertNotIn('injected.example', self.path.read_text())
        self.assertNotIn('dl.pstmn.io', self.path.read_text())

    def test_preview_that_would_drop_an_external_line_is_refused(self):
        p = hosts.plan(dict(action='block', domain='dl.pstmn.io'), self.path.read_text(), None)
        before = self.path.read_text()
        p['text_after'] = p['text_after'].replace('192.0.2.10      intranet.example\n', '')
        with self.assertRaises(hosts.HostsError):
            hosts.execute(p, path=self.path, flush=self.flush)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.flushed, 0)

    def test_helper_dispatch_routes_hosts_plans(self):
        import privileged
        with patch('privileged.hosts.execute', return_value=dict(ok=True)) as run:
            self.assertTrue(privileged.execute(dict(kind='hosts'))['ok'])
            run.assert_called_once_with(dict(kind='hosts'))
        with patch('privileged.execute_route', return_value=dict(ok=True)) as run:
            self.assertTrue(privileged.execute(dict(action='disable'))['ok'])
            run.assert_called_once_with(dict(action='disable'))


class LineEditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'hosts'
        self.path.write_text(BASE)
        self.flushed = 0

    def tearDown(self):
        self.directory.cleanup()

    def flush(self):
        self.flushed += 1

    def entry(self, text=None):
        return next(e for e in hosts.parse(text or self.path.read_text())[1]
                    if e['address'] == '192.0.2.10')

    def run_plan(self, **data):
        p = hosts.plan(dict(data), self.path.read_text(), Path(self.directory.name) / 'backups')
        return p, hosts.execute(p, path=self.path, flush=self.flush)

    def test_update_changes_only_the_target_line(self):
        entry = self.entry()
        plan, result = self.run_plan(action='line-update', entry=entry['id'], address='192.0.2.44')
        self.assertTrue(result['changed'])
        self.assertEqual(plan['line_after'], '192.0.2.44      intranet.example')
        text = self.path.read_text()
        self.assertIn('192.0.2.44      intranet.example', text)
        self.assertIn('127.0.0.1\tlocalhost', text)
        self.assertEqual(self.flushed, 1)

    def test_delete_removes_only_that_line(self):
        entry = self.entry()
        self.run_plan(action='line-delete', entry=entry['id'])
        text = self.path.read_text()
        self.assertNotIn('intranet.example', text)
        self.assertIn('127.0.0.1\tlocalhost', text)
        self.assertIn('::1             localhost', text)

    def test_system_entries_are_never_editable(self):
        for address in ('127.0.0.1', '::1', '255.255.255.255'):
            entry = next(e for e in hosts.parse(BASE)[1] if e['address'] == address)
            for action in ('line-update', 'line-delete'):
                with self.assertRaises(hosts.HostsError):
                    hosts.plan(dict(action=action, entry=entry['id'], address='192.0.2.9'), BASE)

    def test_unknown_entry_id_is_refused(self):
        with self.assertRaises(hosts.HostsError):
            hosts.plan(dict(action='line-delete', entry='doesnotexist'), BASE)

    def test_multi_hostname_line_changes_together_and_warns(self):
        text = BASE + '192.0.2.50 a.example b.example\n'
        entry = next(e for e in hosts.parse(text)[1] if e['address'] == '192.0.2.50')
        plan = hosts.plan(dict(action='line-update', entry=entry['id'], address='192.0.2.51'), text)
        self.assertIn('b.example', plan['warning'])
        self.assertEqual(plan['line_after'], '192.0.2.51 a.example b.example')

    def test_editing_without_managed_block_does_not_create_one(self):
        entry = self.entry()
        plan = hosts.plan(dict(action='line-update', entry=entry['id'], address='192.0.2.44'), BASE)
        self.assertNotIn(hosts.BEGIN, plan['text_after'])


if __name__ == '__main__':
    unittest.main()
