#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cathub_pull.py — CatHub 加载链抓取器

用一条命令把 CatHub loader 的四跳链路全部拉下来，并对每层自动跑静态反混淆。

用法：
    # 1) 完整链路（需要可用 key）
    python3 cathub_pull.py --key 308cd616ff863d1ea330a3e97eef2373 -o out/

    # 2) 只抓不还原
    python3 cathub_pull.py --key <key> -o out/ --raw-only

    # 3) 对本地已有文件反混淆（离线）
    python3 cathub_pull.py --local temp/loader2.lua -o out/

产物：
    out/hop1_shell.lua          /  hop1_shell.deob.lua
    out/hop2_er.lua             /  hop2_er.deob.lua
    out/hop3_engine.lua         /  hop3_engine.deob.lua
    out/hop4_payload.lua        /  hop4_payload.deob.lua   （明文时无需还原）
    out/_chain.json             链路元数据

为什么需要 UA=Roblox/WinInet：CatHub 边缘节点按 UA 分流，普通 UA 直接 403。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

UA = 'Roblox/WinInet'
HOST = 'https://cathub.fr'

# 链路里出现过的路径模板（按实际观测顺序）
HOP_PATH = {
    1: '/load/{key}',
    2: None,   # 由第 1 跳正文解析
    3: '/v1/loader/{key}/t',
    4: '/h?tk={token}',
}

URL_RE = re.compile(rb'https?://[^\s"\'<>\)\]\}]+')
PATH_RE = re.compile(rb'/[A-Za-z0-9_\-/\.]{6,120}')


def http_get(url, timeout=20, headers=None):
    h = {'User-Agent': UA, 'Accept': '*/*'}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def is_lua_text(b):
    if not b:
        return False
    head = b[:400]
    if b'\x00' in b[:200]:
        return False
    try:
        head.decode('utf-8')
    except UnicodeDecodeError:
        try:
            head.decode('latin-1')
        except Exception:
            return False
    return b'loadstring' in b or b'local' in b or b'cathub' in b.lower() or b'function' in b


def extract_refs(body, key):
    """从正文里挖下一跳候选 URL / 路径"""
    urls, paths = [], []
    for m in URL_RE.finditer(body):
        u = m.group(0).decode('latin-1').rstrip('.,;')
        if u not in urls:
            urls.append(u)
    for raw in (b'/load/init', b'/v1/loader', b'/h?tk', b'init-'):
        if raw in body:
            pass
    for m in PATH_RE.finditer(body):
        p = m.group(0).decode('latin-1')
        if any(k in p for k in ('/load/', '/v1/loader', '/h?', '/init')):
            if p not in paths:
                paths.append(p)
    # 把 key 拼进模板路径
    for i, p in enumerate(list(paths)):
        if '<key>' in p or '{key}' in p:
            paths[i] = p.replace('<key>', key).replace('{key}', key)
    return urls, paths


def try_chain(key, outdir, timeout=20):
    os.makedirs(outdir, exist_ok=True)
    meta = {'key': key, 'ua': UA, 'hops': [], 'started': time.strftime('%Y-%m-%d %H:%M:%S')}
    token = None
    token_len = None

    def save(hop, tag, body, note=''):
        name = os.path.join(outdir, 'hop%d_%s.lua' % (hop, tag))
        with open(name, 'wb') as f:
            f.write(body)
        rec = {'hop': hop, 'file': os.path.basename(name), 'bytes': len(body), 'note': note}
        meta['hops'].append(rec)
        print('  hop%d  %-22s %6d B  %s' % (hop, tag, len(body), note))
        return name

    # hop1: shell
    url = HOST + HOP_PATH[1].format(key=key)
    try:
        st, body = http_get(url, timeout)
    except Exception as e:
        print('  hop1 失败: %s' % e)
        meta['error'] = str(e)
        return meta
    print('  hop1  HTTP %s  %d B' % (st, len(body)))
    save(1, 'shell', body, url)

    # 从 hop1 找 hop2
    urls, paths = extract_refs(body, key)
    hop2_candidates = [u for u in urls if '/load/' in u or u.endswith('.lua')]
    hop2_candidates += [HOST + p for p in paths]
    seen = set()
    for u in hop2_candidates:
        if u in seen or u.rstrip('/') == url:
            continue
        seen.add(u)
        try:
            st, body2 = http_get(u, timeout)
        except Exception as e:
            print('  hop2 候选失败 %s -> %s' % (u, e))
            continue
        if st == 200 and is_lua_text(body2):
            print('  hop2  HTTP 200  %d B  %s' % (len(body2), u))
            save(2, 'er', body2, u)
            break
        else:
            print('  hop2 候选 %s HTTP %s 非 lua' % (u, st))

    # hop3: token
    url3 = HOST + HOP_PATH[3].format(key=key)
    try:
        st, body3 = http_get(url3, timeout)
        txt = body3.decode('utf-8', 'replace').strip()
        print('  hop3  HTTP %s  %r' % (st, txt[:80]))
        parts = txt.split()
        if len(parts) >= 2:
            token_len = parts[0]
            token = parts[1]
        save(3, 'token', body3, url3 + ' -> ' + txt[:60])
    except Exception as e:
        print('  hop3 失败: %s' % e)

    # hop4: payload（拿到的可能是密文摘要，真正明文要靠 cathub_live_dump.lua）
    if token:
        url4 = HOST + HOP_PATH[4].format(token=urllib.parse.quote(token))
        try:
            st, body4 = http_get(url4, timeout)
            print('  hop4  HTTP %s  %d B' % (st, len(body4)))
            save(4, 'payload', body4, url4[:80])
            dec = None
            try:
                cand = body4.strip()
                if re.fullmatch(rb'[A-Za-z0-9+/=\r\n]+', cand):
                    dec = base64.b64decode(cand + b'==')
            except Exception:
                dec = None
            if dec and b'function' in dec or (dec and b'local' in dec):
                p = os.path.join(outdir, 'hop4_payload_decoded.lua')
                open(p, 'wb').write(dec)
                print('  hop4  解码出明文 payload  %d B -> %s' % (len(dec), os.path.basename(p)))
        except Exception as e:
            print('  hop4 失败: %s' % e)

    meta['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(os.path.join(outdir, '_chain.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


def run_deob(paths, deob, outdir):
    os.makedirs(outdir, exist_ok=True)
    for p in paths:
        base = os.path.splitext(os.path.basename(p))[0]
        out = os.path.join(outdir, base + '.deob.lua')
        strs = os.path.join(outdir, base + '.strings.txt')
        cmd = [sys.executable, deob, p, '-o', out, '--strings', strs]
        print('  -> %s' % ' '.join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        for line in (r.stdout or '').splitlines():
            if line.strip().startswith('[*]'):
                print('     ' + line.strip())
        if r.returncode != 0:
            print('     ! 失败: %s' % (r.stderr or '').strip()[:200])


def main():
    ap = argparse.ArgumentParser(description='CatHub 加载链抓取 + 静态反混淆')
    ap.add_argument('--key', help='loader key')
    ap.add_argument('--local', nargs='*', default=[], help='本地 lua 文件，仅做反混淆')
    ap.add_argument('-o', '--out', default='cathub_out', help='输出目录')
    ap.add_argument('--raw-only', action='store_true', help='只抓取不还原')
    ap.add_argument('--deob', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cathub_deob.py'))
    ap.add_argument('--timeout', type=int, default=20)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    targets = list(a.local)
    if a.key:
        print('[*] 抓取链路 key=%s' % a.key)
        meta = try_chain(a.key, a.out, a.timeout)
        targets += [os.path.join(a.out, h['file']) for h in meta.get('hops', []) if h['hop'] <= 3]

    if targets and not a.raw_only:
        if not os.path.exists(a.deob):
            print('[!] 找不到反混淆器: %s' % a.deob)
            return 2
        print('[*] 静态反混淆 %d 个文件' % len(targets))
        run_deob(targets, a.deob, os.path.join(a.out, 'deob'))
    print('[*] 完成 -> %s' % os.path.abspath(a.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
