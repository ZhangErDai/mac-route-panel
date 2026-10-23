"""macOS IPv4 route parsing and narrowly scoped route operations (stdlib only)."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import plistlib
import re
import shlex
import subprocess
import time
from pathlib import Path

PREFERENCES = Path('/Library/Preferences/SystemConfiguration/preferences.plist')


class RouteError(Exception):
    pass


def run(args: list[str], timeout=15) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RouteError(f'系统命令无法完成：{e}') from e
    if p.returncode:
        raise RouteError((p.stderr or p.stdout).strip() or '系统命令执行失败')
    return p.stdout.strip()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def network(value: str) -> str:
    try:
        n = ipaddress.IPv4Network(value.strip(), strict=False)
    except (ValueError, AttributeError):
        raise RouteError('目标必须是 IPv4 地址或网段，例如 198.18.4.136 或 198.18.4.0/24。')
    if n.prefixlen == 0 or n.is_loopback or n.is_link_local or n.is_multicast or n.network_address.is_reserved:
        raise RouteError('默认、回环、链路本地、组播和保留网段不支持通过此页面修改。')
    return str(n)


def gateway(value: str) -> str:
    try:
        ip = ipaddress.IPv4Address(value.strip())
    except (ValueError, AttributeError):
        raise RouteError('网关必须是合法的 IPv4 地址。')
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_reserved:
        raise RouteError('网关必须是可用的单播 IPv4 地址。')
    return str(ip)


def destination(raw: str, flags: str) -> str:
    if raw == 'default':
        return '0.0.0.0/0'
    address, _, bits = raw.partition('/')
    parts = address.split('.')
    prefix = int(bits) if bits else (32 if 'H' in flags else len(parts) * 8)
    return str(ipaddress.IPv4Network('.'.join(parts + ['0'] * (4 - len(parts))) + f'/{prefix}', strict=False))


def route_id(r: dict) -> str:
    return digest([r['cidr'], r['mode'], r.get('gateway', ''), r['interface'], bool(r.get('scope'))])[:24]


def parse_routes(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] in ('Routing', 'Internet:', 'Destination'):
            continue
        raw, gw, flags, iface = parts[:4]
        try:
            cidr = destination(raw, flags)
        except ValueError:
            continue
        # These are ARP/neighbour entries or cloned per-host caches, not configured rules.
        if any(flag in flags for flag in ('L', 'W', 'l')):
            continue
        n = ipaddress.IPv4Network(cidr)
        mode = 'gateway' if 'G' in flags else 'interface'
        editable = (n.prefixlen != 0 and not n.is_loopback and not n.is_link_local
                    and not n.is_multicast and not n.network_address.is_reserved
                    and 'S' in flags and 'R' not in flags and 'B' not in flags
                    and (mode == 'gateway' or iface.startswith('utun')))
        r = dict(cidr=cidr, gateway=gw if mode == 'gateway' else '', interface=iface,
                 mode=mode, scope='I' in flags, flags=flags, editable=editable)
        r['id'] = route_id(r)
        rows.append(r)
    return rows


def parse_interfaces(text: str) -> dict:
    result, current = {}, None
    for line in text.splitlines():
        m = re.match(r'^([a-zA-Z][\w]*):.*flags=\w+<([^>]+)>', line)
        if m:
            current = m[1]
            result[current] = dict(name=current, up='UP' in m[2].split(','), addresses=[], networks=[])
        if current:
            if 'status: inactive' in line:
                result[current]['up'] = False
            m = re.search(r'\binet ([\d.]+).*netmask (0x[0-9a-fA-F]+|[\d.]+)', line)
            if m:
                mask = str(ipaddress.IPv4Address(int(m[2], 16))) if m[2].startswith('0x') else m[2]
                result[current]['addresses'].append(m[1])
                result[current]['networks'].append(str(ipaddress.IPv4Network(f'{m[1]}/{mask}', strict=False)))
    return result


def services(path: Path = PREFERENCES) -> dict:
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as e:
        raise RouteError(f'无法读取网络服务配置：{e}') from e
    result = {}
    for sid, svc in data.get('NetworkServices', {}).items():
        entries = []
        for item in svc.get('IPv4', {}).get('AdditionalRoutes', []):
            try:
                n = ipaddress.IPv4Network(f"{item['DestinationAddress']}/{item['SubnetMask']}", strict=False)
                g = str(ipaddress.IPv4Address(item['GatewayAddress']))
                entries.append([str(n.network_address), str(n.netmask), g])
            except (KeyError, ValueError):
                raise RouteError('网卡额外路由配置中有无法识别的条目，为避免覆盖，已停止读取。')
        result[sid] = dict(id=sid, name=svc.get('UserDefinedName', sid),
                           interface=svc.get('Interface', {}).get('DeviceName', ''), routes=entries)
    return result


def system() -> dict:
    return dict(routes=parse_routes(run(['/usr/sbin/netstat', '-rn', '-f', 'inet'])),
                interfaces=parse_interfaces(run(['/sbin/ifconfig'])), services=services())


def triple(r: dict) -> list[str]:
    n = ipaddress.IPv4Network(r['cidr'])
    return [str(n.network_address), str(n.netmask), r['gateway']]


def direct_interface(gw: str, interfaces: dict, routes=None) -> str:
    ip = ipaddress.IPv4Address(gw)
    matches = []
    for name, entry in interfaces.items():
        if not entry['up'] or name == 'lo0':
            continue
        if gw in entry['addresses']:
            raise RouteError('不能把本机自身地址设为转发网关。')
        for cidr in entry['networks']:
            n = ipaddress.IPv4Network(cidr)
            if ip in n:
                matches.append((n.prefixlen, name))
    # Point-to-point tunnels may have only a /32 interface address, while their
    # directly attached prefixes are expressed in the kernel routing table.
    for r in routes or []:
        if r['mode'] == 'interface' and r['cidr'] != '0.0.0.0/0' and interfaces.get(r['interface'], {}).get('up'):
            n = ipaddress.IPv4Network(r['cidr'])
            if ip in n:
                matches.append((n.prefixlen, r['interface']))
    if not matches:
        raise RouteError('网关不在当前可用的直连网络中。若要通过隧道转发，请选择“通过接口”。')
    best = max(x[0] for x in matches)
    names = {name for bits, name in matches if bits == best}
    if len(names) != 1:
        raise RouteError('网关同时匹配多个接口，无法确定出口；请先调整网卡配置。')
    return names.pop()


def new_route(payload: dict, snap: dict) -> dict:
    cidr = network(payload.get('destination', ''))
    mode = payload.get('mode')
    if mode == 'gateway':
        gw = gateway(payload.get('gateway', ''))
        iface = direct_interface(gw, snap['interfaces'], snap['routes'])
    elif mode == 'interface':
        iface = payload.get('interface', '')
        if iface not in snap['interfaces'] or not iface.startswith('utun'):
            raise RouteError('接口转发目前仅支持当前存在的 utun 隧道接口。')
        if not snap['interfaces'][iface]['up']:
            raise RouteError('所选接口当前不可用。')
        gw = ''
    else:
        raise RouteError('请选择通过网关或通过接口。')
    if any(r['cidr'] == cidr and not r['scope'] for r in snap['routes']):
        raise RouteError('系统已存在相同目标的路由，请在列表中管理原规则。')
    if any(ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(cidr)
           for i in snap['interfaces'].values() for ip in i['addresses']) and ipaddress.IPv4Network(cidr).prefixlen == 32:
        raise RouteError('不能为本机自身地址添加转发路由。')
    r = dict(cidr=cidr, mode=mode, gateway=gw, interface=iface, scope=False,
             editable=True, source='local', flags='UGS' if mode == 'gateway' else 'US')
    r['id'] = route_id(r)
    return r


def route_command(action: str, r: dict) -> list[str]:
    if action not in ('add', 'delete'):
        raise RouteError('不支持的路由命令。')
    cidr = network(r['cidr'])
    iface = r['interface']
    if not re.fullmatch(r'(?:en|utun|feth|bridge)\d+', iface):
        raise RouteError('接口名称不合法。')
    n = ipaddress.IPv4Network(cidr)
    args = ['/sbin/route', '-n', action, '-host' if n.prefixlen == 32 else '-net']
    if r.get('scope'):
        args.extend(['-ifscope', iface])
    args.append(str(n.network_address) if n.prefixlen == 32 else cidr)
    if r['mode'] == 'gateway':
        args.append(gateway(r['gateway']))
    elif r['mode'] == 'interface' and iface.startswith('utun'):
        args.extend(['-interface', iface])
    else:
        raise RouteError('不能修改物理接口直连路由。')
    return args


def service_command(svc: dict, entries: list[list[str]]) -> list[str]:
    args = ['/usr/sbin/networksetup', '-setadditionalroutes', svc['name']]
    for item in entries:
        if len(item) != 3:
            raise RouteError('保存的路由格式不正确。')
        ipaddress.IPv4Network(f'{item[0]}/{item[1]}')
        ipaddress.IPv4Address(item[2])
        args.extend(item)
    return args


def current_route(r: dict, snap: dict, new=False):
    return next((item for item in snap['routes'] if
                 (item['cidr'] == r['cidr'] and item['mode'] == r['mode']
                  and item['gateway'] == r['gateway'] and item['scope'] == r['scope']
                  and (new or item['interface'] == r['interface']))), None)


def enriched(snap: dict, records: dict) -> list[dict]:
    rows = {r['id']: dict(r, live=True, enabled=True, source='tunnel' if r['interface'].startswith('utun') else 'runtime',
                         status='active', service_id=None) for r in snap['routes']}
    for svc in snap['services'].values():
        for addr, mask, gw in svc['routes']:
            r = dict(cidr=str(ipaddress.IPv4Network(f'{addr}/{mask}')), gateway=gw,
                     interface=svc['interface'], mode='gateway', scope=False, editable=True,
                     flags='UGS', service_id=svc['id'], service_name=svc['name'], source='saved')
            r['id'] = route_id(r)
            # Persistent additional routes normally have no interface scope.
            if r['id'] not in rows:
                rows[r['id']] = dict(r, live=False, enabled=True, status='missing')
            else:
                rows[r['id']].update(service_id=svc['id'], service_name=svc['name'], source='saved')
            try:
                network(r['cidr'])
            except RouteError:
                rows[r['id']]['editable'] = False
    for rid, record in records.items():
        r = record['route']
        if rid in rows:
            if not record['enabled']:
                rows[rid]['status'] = 'restored'
            if rows[rid]['source'] == 'runtime':
                rows[rid]['source'] = 'local'
        else:
            rows[rid] = dict(r, live=False, enabled=record['enabled'],
                             status='missing' if record['enabled'] else 'disabled')
    return sorted(rows.values(), key=lambda r: (not r['editable'], int(ipaddress.IPv4Network(r['cidr']).network_address), -ipaddress.IPv4Network(r['cidr']).prefixlen, r['interface']))


def plan(action: str, r: dict, snap: dict) -> dict:
    if action not in ('add', 'enable', 'disable') or not r.get('editable'):
        raise RouteError('这条路由只读，不能修改。')
    route_command('add', r)  # Validate all arguments, including restored JSON records.
    svc = snap['services'].get(r.get('service_id'))
    if r.get('service_id') and not svc:
        raise RouteError('原网络服务已不存在，不能自动恢复。')
    before = [list(t) for t in svc['routes']] if svc else None
    after = None
    commands = []
    live = current_route(r, snap, new=action == 'add')
    if svc:
        after = [list(t) for t in before]
        item = triple(r)
        if action == 'disable':
            after = [t for t in after if t != item]
        elif item not in after:
            after.append(item)
        if before != after:
            commands.append(service_command(svc, after))
    if action == 'disable':
        if live:
            commands.append(route_command('delete', live))
    elif not live:
        commands.append(route_command('add', r))
    return dict(action=action, route=r, was_live=bool(live), service_before=before,
                service_after=after, commands=[shlex.join(cmd) for cmd in commands])


def execute(p: dict) -> dict:
    """Called only by the elevated helper. Recheck state after the auth dialog."""
    r, action = p['route'], p['action']
    snap = system()
    if action == 'add':
        validated = new_route(dict(destination=r['cidr'], mode=r['mode'], gateway=r['gateway'], interface=r['interface']), snap)
        if validated['interface'] != r['interface']:
            raise RouteError('预计出口已改变，请刷新后重试。')
    fresh = plan(action, r, snap)
    if fresh['was_live'] != p['was_live'] or fresh['service_before'] != p['service_before']:
        raise RouteError('授权期间路由或网卡配置已改变，本次操作未执行，请刷新重试。')
    live_before = current_route(r, snap)
    if live_before and not live_before['editable']:
        raise RouteError('系统已将这条路由标记为只读，操作已取消。')
    if action != 'disable' and not live_before and not r.get('service_id'):
        if r['mode'] == 'gateway':
            direct_interface(r['gateway'], snap['interfaces'], snap['routes'])
        elif not snap['interfaces'].get(r['interface'], {}).get('up'):
            raise RouteError('隧道接口已不可用。')
    svc = snap['services'].get(r.get('service_id'))
    config_changed = False
    try:
        if svc and fresh['service_before'] != fresh['service_after']:
            config_changed = True
            run(service_command(svc, fresh['service_after']), timeout=30)
            if services().get(svc['id'], {}).get('routes') != fresh['service_after']:
                raise RouteError('网卡配置写入后核对不一致。')
        now = system()
        current = current_route(r, now, new=action == 'add')
        if action == 'disable' and current:
            try:
                run(route_command('delete', current))
            except RouteError:
                if current_route(r, system()):
                    raise
        elif action != 'disable' and not current:
            iface_up = now['interfaces'].get(r['interface'], {}).get('up', False)
            if svc and not iface_up:
                return dict(ok=True, route=r, live=False, message='配置已开启；接口未连接，当前未生效。')
            try:
                run(route_command('add', r))
            except RouteError:
                # networksetup may have installed it between the snapshot and add.
                if not current_route(r, system(), new=action == 'add'):
                    raise
        time.sleep(.15)
        actual = current_route(r, system(), new=action == 'add')
        if (action == 'disable' and actual) or (action != 'disable' and not actual):
            raise RouteError('系统实际路由与操作结果不一致，可能被其他软件重新配置。')
        return dict(ok=True, route=dict(r, **(actual or {})), live=bool(actual), message='路由已关闭。' if action == 'disable' else '路由已开启。')
    except Exception as error:
        rollback = []
        if config_changed:
            try:
                current_cfg = services().get(svc['id'], {}).get('routes')
                if current_cfg == fresh['service_after']:
                    run(service_command(svc, fresh['service_before']), timeout=30)
                    if services().get(svc['id'], {}).get('routes') != fresh['service_before']:
                        raise RouteError('原网卡配置恢复后核对不一致')
                elif current_cfg != fresh['service_before']:
                    raise RouteError('其他程序已更改网卡配置，未覆盖其变更')
            except Exception as e:
                rollback.append(str(e))
        try:
            now = system()
            current = current_route(r, now, new=action == 'add')
            if live_before and not current:
                run(route_command('add', live_before))
            elif not live_before and current:
                run(route_command('delete', current))
            if bool(current_route(r, system(), new=action == 'add')) != bool(live_before):
                raise RouteError('运行时路由恢复后核对不一致')
        except Exception as e:
            rollback.append(str(e))
        suffix = '；恢复未完成：' + '；'.join(rollback) if rollback else '；已核对并恢复操作前状态。'
        raise RouteError(str(error) + suffix) from error
