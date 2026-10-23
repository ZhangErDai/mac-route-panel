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


def read(path=HOSTS) -> str:
    try:
        return Path(path).read_text()
    except OSError as e:
        raise HostsError(f'无法读取 {path}：{e}') from e


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def name(value) -> str:
    text = str(value or '').strip().lower().rstrip('.')
    if not DOMAIN.match(text):
        raise HostsError('域名格式不正确，只能包含字母、数字、点和连字符，例如 test.com。')
    if text in RESERVED or text.endswith('.localhost'):
        raise HostsError('系统保留的主机名不能在这里修改。')
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
                                kind='block' if address == BLOCK else 'pin'))
            continue
        if stripped and not stripped.startswith('#'):
            parts = stripped.split('#')[0].split()
            if len(parts) >= 2:
                external.append(dict(address=parts[0], domains=parts[1:],
                                     raw=line))
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
                text_before=text, text_after=new_text, backup_dir=str(backup_dir or ''),
                commands=commands, adopted=bool(adopting))


def execute(p: dict, path=HOSTS, flush=_flush, backup_dir=None) -> dict:
    """Runs only inside the elevated helper; rechecks the file after authorization."""
    text = read(path)
    if digest(text) != p['digest']:
        raise HostsError('/etc/hosts 在授权期间被其他程序修改，本次操作已取消，请刷新后重试。')
    rules, _ = parse(text)
    if [dict(r) for r in rules] != p['before']:
        raise HostsError('受管块在授权期间发生变化，本次操作已取消，请刷新后重试。')
    new_text = p['text_after']
    folder = backup_dir or p.get('backup_dir') or None
    changed = new_text != text
    if changed:
        _backup(text, folder)
        try:
            _write(new_text, path)
            current, _ = parse(read(path))
            if [dict(r) for r in current] != p['after']:
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
