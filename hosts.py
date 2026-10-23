"""Managed /etc/hosts rules: domain blocking (0.0.0.0) and fixed-IP pinning.

Only the marked block is ever written. Every other byte of /etc/hosts stays as it
was, so unrelated system entries and hand-written lines cannot be damaged here.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

HOSTS = Path('/etc/hosts')
BEGIN = '# >>> route-panel managed >>>'
END = '# <<< route-panel managed <<<'
MAX_RULES = 200
DOMAIN = re.compile(r'^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?'
                    r'(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$')
BLOCK = '0.0.0.0'
RESERVED = {'localhost', 'localhost.localdomain', 'broadcasthost',
            'ip6-localhost', 'ip6-loopback', 'ip6-allnodes', 'ip6-allrouters'}


class HostsError(Exception):
    pass


def protected(address: str) -> bool:
    """True for addresses this tool must never manage (loopback, link-local, ...)."""
    if address == BLOCK:
        return False
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return True  # IPv6 or anything unexpected inside the managed block
    return ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def system_address(address: str) -> bool:
    """True for well-known system entries shown read-only in the UI.

    ``0.0.0.0``/``::`` are deliberately excluded: outside the managed block they
    are usually someone's own blocking rule, not something the system owns.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_unspecified:
        return False
    if ip.version == 6:
        return ip.is_loopback or ip.is_link_local or ip.is_multicast
    return ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved



def read(path=HOSTS) -> str:
    try:
        return Path(path).read_text()
    except OSError as e:
        raise HostsError(f'无法读取 {path}：{e}') from e


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def name(value) -> str:
    text = str(value or '').strip().lower().rstrip('.')
    if text in RESERVED or text.endswith('.localhost'):
        raise HostsError('系统保留的主机名（如 localhost、broadcasthost）不能在这里修改。')
    if not DOMAIN.match(text):
        raise HostsError('域名格式不正确，只能包含字母、数字、点和连字符，例如 test.com。')
    return text


def fixed(value) -> str:
    try:
        ip = ipaddress.IPv4Address(str(value or '').strip())
    except ValueError:
        raise HostsError('固定 IP 必须是合法的 IPv4 地址。')
    if ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        raise HostsError('固定 IP 必须是可用的单播地址；屏蔽请选择“屏蔽”动作。')
    return str(ip)


def parse(text: str):
    """Return (managed rules, external entries). Raises on an asymmetrical block."""
    managed, external = [], []
    inside = False
    opened = closed = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == BEGIN:
            if opened:
                raise HostsError('受管块标记重复，请先手工检查 /etc/hosts。')
            opened = inside = True
            continue
        if stripped == END:
            if not inside:
                raise HostsError('受管块结束标记没有对应的开始标记，请先手工检查 /etc/hosts。')
            closed, inside = True, False
            continue
        if inside:
            if not stripped or stripped.startswith('#'):
                continue
            parts = stripped.split('#')[0].split()
            if len(parts) != 2:
                raise HostsError('受管块内有无法识别的条目，为避免误改已停止读取。')
            address, domain = parts
            managed.append(dict(domain=domain.lower(),
                                address=address,
                                kind='block' if address == BLOCK else 'pin',
                                protected=protected(address)))
            continue
        if stripped and not stripped.startswith('#'):
            parts = stripped.split('#')[0].split()
            if len(parts) >= 2:
                external.append(dict(address=parts[0], domains=parts[1:], raw=line,
                                     system=system_address(parts[0]),
                                     id=entry_id(line)))
    if opened != closed:
        raise HostsError('受管块标记不完整，请先手工检查 /etc/hosts。')
    if len(managed) > MAX_RULES:
        raise HostsError(f'受管条目超过 {MAX_RULES} 条，请先清理。')
    return managed, external


def render(text: str, rules: list[dict]) -> str:
    """Rewrite only the managed block; keep every other line byte for byte."""
    block = [BEGIN]
    for rule in sorted(rules, key=lambda r: (r['domain'], r['address'])):
        block.append(f"{rule['address']} {rule['domain']}")
    block.append(END)

    lines = text.splitlines(keepends=True)
    out, index, replaced = [], 0, False
    while index < len(lines):
        if lines[index].strip() == BEGIN:
            replaced = True
            while index < len(lines) and lines[index].strip() != END:
                index += 1
            index += 1
            out.extend(line + '\n' for line in block)
            continue
        out.append(lines[index])
        index += 1
    if not replaced:
        if out and not out[-1].endswith('\n'):
            out[-1] += '\n'
        if out and out[-1].strip():
            out.append('\n')
        out.extend(line + '\n' for line in block)
    return ''.join(out)


def merge(rules: list[dict], data: dict) -> list[dict]:
    action = data.get('action')
    if action not in ('block', 'pin', 'remove'):
        raise HostsError('不支持的操作。')
    target = name(data.get('domain'))
    existing = next((r for r in rules if r['domain'] == target), None)
    if existing and existing.get('protected'):
        raise HostsError('该域名在受管块中对应回环、链路本地或广播地址，属于系统条目，页面不会修改它。请手工检查 /etc/hosts。')
    if action == 'remove':
        remaining = [r for r in rules if r['domain'] != target]
        if len(remaining) == len(rules):
            raise HostsError('这条域名不在受管块中，可能已被手工修改，请刷新。')
        return remaining

    address = BLOCK if action == 'block' else fixed(data.get('address'))
    entry = dict(domain=target, address=address, kind='block' if address == BLOCK else 'pin')
    rules = [r for r in rules if r['domain'] != target] + [entry]
    if len(rules) > MAX_RULES:
        raise HostsError(f'受管条目不能超过 {MAX_RULES} 条。')
    return rules


def conflicts(rule: dict, external: list[dict]) -> list[dict]:
    target = rule['domain'].lower()
    return [e for e in external if target in [d.lower() for d in e['domains']]]


def adoptable(rule: dict, external: list[dict]) -> bool:
    """True when exactly one external line maps this domain and nothing else."""
    hits = conflicts(rule, external)
    return (len(hits) == 1 and [d.lower() for d in hits[0]['domains']] == [rule['domain']]
            and hits[0]['address'] == rule['address'])


def drop_external(text: str, entry: dict) -> str:
    """Remove one exact external line, used only by an explicit adopt operation."""
    lines, out, removed = text.splitlines(keepends=True), [], False
    for line in lines:
        if not removed and line.rstrip('\n') == entry['raw']:
            removed = True
            continue
        out.append(line)
    if not removed:
        raise HostsError('原有条目在授权期间被修改，本次操作已取消，请刷新。')
    return ''.join(out)


def entry_id(raw: str) -> str:
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _replace_address(raw: str, address: str) -> str:
    match = re.match(r'^(\s*)\S+(.*)$', raw)
    if not match:
        raise HostsError('这一行格式无法识别，请手工处理。')
    return f'{match[1]}{address}{match[2]}'


def _drop_line(text: str, raw: str) -> str:
    lines, out, removed = text.splitlines(keepends=True), [], False
    for line in lines:
        if not removed and line.rstrip('\n') == raw:
            removed = True
            continue
        out.append(line)
    if not removed:
        raise HostsError('这条记录已变化，请刷新后重试。')
    return ''.join(out)


def _flush() -> None:
    for command in (['/usr/bin/dscacheutil', '-flushcache'],
                    ['/usr/bin/killall', '-HUP', 'mDNSResponder']):
        try:
            subprocess.run(command, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _write(text: str, path=HOSTS) -> None:
    target = Path(path)
    stat = target.stat()
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix='.hosts-')
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(tmp, stat.st_mode & 0o7777)
        try:
            os.chown(tmp, stat.st_uid, stat.st_gid)
        except OSError:
            pass
        os.replace(tmp, target)
        dfd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _backup(text: str, directory) -> None:
    if not directory:
        return
    folder = Path(directory)
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        (folder / f'hosts-{stamp}.txt').write_text(text)
        for old in sorted(folder.glob('hosts-*.txt'))[:-5]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def plan(data: dict, text: str, backup_dir=None) -> dict:
    rules, external = parse(text)
    action = data.get('action')
    if action in ('line-update', 'line-delete'):
        return _plan_line(action, data, text, rules, external, backup_dir)
    target = name(data.get('domain'))
    if action not in ('block', 'pin', 'remove'):
        raise HostsError('不支持的操作。')

    adopting = None
    if action != 'remove':
        address = BLOCK if action == 'block' else fixed(data.get('address'))
        rule = dict(domain=target, address=address, kind='block' if address == BLOCK else 'pin')
        hits = conflicts(rule, external)
        if hits:
            if not adoptable(rule, external):
                raise HostsError('该域名已在 /etc/hosts 的其他位置存在，请先手工处理那一条，避免冲突。')
            adopting = hits[0]

    after = merge(rules, data)
    source = drop_external(text, adopting) if adopting else text
    new_text = render(source, after)
    commands = []
    if adopting:
        commands.append(f"迁移原有行：{adopting['raw'].strip()}")
    commands.append(f'写入 {BEGIN} … {END} 受管块：')
    commands.extend(f"{r['address']} {r['domain']}" for r in sorted(after, key=lambda r: r['domain']))
    if not after:
        commands.append('（受管块为空，仅保留标记）')
    commands.append('/usr/bin/dscacheutil -flushcache')
    commands.append('/usr/bin/killall -HUP mDNSResponder')

    return dict(kind='hosts', action=action, domain=target,
                rule=None if action == 'remove' else next(r for r in after if r['domain'] == target),
                before=[dict(r) for r in rules], after=after, digest=digest(text),
                external_before=external, external_after=parse(new_text)[1],
                text_before=text, text_after=new_text, backup_dir=str(backup_dir or ''),
                commands=commands, adopted=bool(adopting))


def _shape(rules) -> list[tuple]:
    """Comparable form of a rule list, ignoring derived flags."""
    return [(r['domain'], r['address']) for r in rules]


def _with_block(text: str, rules: list) -> str:
    """Keep files without a managed block untouched when only a plain line changes."""
    return render(text, rules) if rules or BEGIN in text else text


def _plan_line(action: str, data: dict, text: str, rules: list, external: list, backup_dir) -> dict:
    """Edit or delete one existing /etc/hosts line that is not in the managed block."""
    ref = str(data.get('entry') or '')
    entry = next((e for e in external if e['id'] == ref), None)
    if not entry:
        raise HostsError('这条记录已变化，请刷新后重试。')
    if entry['system']:
        raise HostsError('这是系统维护的条目（回环、链路本地、广播等），不能在这里修改或删除。')

    if action == 'line-update':
        address = fixed(data.get('address'))
        target = _replace_address(entry['raw'], address)
        command = f"修改：{entry['raw'].strip()}\n    → {target.strip()}"
    else:
        target = None
        command = f"删除：{entry['raw'].strip()}"

    new_text = _with_block(_drop_line(text, entry['raw']), rules) if target is None else \
        _with_block(text.replace(entry['raw'], target, 1), rules)
    commands = [command, '/usr/bin/dscacheutil -flushcache',
                '/usr/bin/killall -HUP mDNSResponder']
    warning = ''
    if len(entry['domains']) > 1:
        others = '、'.join(entry['domains'][1:])
        warning = f"这一行同时包含 {others}，本操作会一并影响它们。"
    return dict(kind='hosts', action=action, domain=' '.join(entry['domains']),
                rule=None, entry=dict(address=entry['address'], domains=entry['domains'],
                                      id=entry['id'], raw=entry['raw']),
                line_after=None if target is None else target.strip(),
                before=[dict(r) for r in rules], after=rules, digest=digest(text),
                external_before=external, external_after=parse(new_text)[1],
                text_before=text, text_after=new_text, backup_dir=str(backup_dir or ''),
                commands=commands, adopted=False, warning=warning)


def execute(p: dict, path=HOSTS, flush=_flush, backup_dir=None) -> dict:
    """Runs only inside the elevated helper; rechecks the file after authorization."""
    text = read(path)
    if digest(text) != p['digest']:
        raise HostsError('/etc/hosts 在授权期间被其他程序修改，本次操作已取消，请刷新后重试。')
    rules, external = parse(text)
    if _shape(rules) != _shape(p['before']):
        raise HostsError('受管块在授权期间发生变化，本次操作已取消，请刷新后重试。')
    new_text = p['text_after']
    # Hard invariant: entries outside the managed block may only change by the
    # single line explicitly adopted in the preview. Nothing else is ours to touch.
    if parse(new_text)[1] != p['external_after']:
        raise HostsError('操作预览与将写入的内容不一致，已取消。请重新预览。')
    folder = backup_dir or p.get('backup_dir') or None
    changed = new_text != text
    if changed:
        _backup(text, folder)
        try:
            _write(new_text, path)
            current, current_external = parse(read(path))
            if _shape(current) != _shape(p['after']) or current_external != p['external_after']:
                raise HostsError('写入后核对不一致，正在尝试恢复原文件。')
        except Exception:
            try:
                _write(text, path)
            except Exception:
                pass
            raise
        flush()
    return dict(ok=True, message='已更新 /etc/hosts 受管块。' if changed else '受管块已是最新状态，无需改动。',
                managed=p['after'], changed=changed)
