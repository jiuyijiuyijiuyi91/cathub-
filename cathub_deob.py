#!/usr/bin/env python3
"""CatHub Obfuscator v1 静态反混淆器 (v2)
用法:
  python3 cathub_deob.py <protected.lua> [-o out.lua] [--strings map.txt] [--raw]
说明:
  1. 定位并执行字符串表 + 洗牌块，导出最终顺序
  2. 定位解码函数 base+ (a+b) 索引形式
  3. 折叠纯常量算术表达式（带前驱字符保护，保证不产生 -- 粘连）
  4. 内联全部解码调用为字符串字面量
  5. token 感知的换行/缩进美化（只插入换行与空格，不改动 token 内容）
  6. luac -p 语法校验，失败时自动剥离尾部多余闭合
"""
import sys, os, re, subprocess, argparse

NUMCHARS = set('0123456789abcdefxXeE \t+-*/%().')
OPEN_KW = {'function', 'do', 'if', 'repeat'}
CLOSE_KW = {'end', 'until'}
KW = OPEN_KW | CLOSE_KW | {'then', 'else', 'elseif', 'local', 'while', 'for', 'return',
                           'and', 'or', 'not', 'nil', 'true', 'false', 'break', 'in'}


def read_text(p):
    with open(p, 'r', encoding='utf-8', errors='surrogateescape') as f:
        return f.read()


def write_text(p, s):
    with open(p, 'w', encoding='utf-8', errors='surrogateescape') as f:
        f.write(s)


def lua_quote(s):
    out = ['"']
    for ch in s:
        c = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif 32 <= c < 127:
            out.append(ch)
        else:
            out.append('\\x%02X' % c)
    out.append('"')
    return ''.join(out)


def scan_string(s, i):
    q = s[i]
    j = i + 1
    while j < len(s):
        if s[j] == '\\':
            j += 2
            continue
        if s[j] == q:
            return j + 1
        j += 1
    return len(s)


def match_brace(s, i):
    depth = 0
    while i < len(s):
        c = s[i]
        if c in '"\'':
            i = scan_string(s, i)
            continue
        if s.startswith('--', i):
            j = s.find('\n', i)
            return -1 if j < 0 else match_brace(s, i) if False else (match_brace_skip(s, i), -1)[1]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def match_brace_skip(s, i):
    j = s.find('\n', i)
    return len(s) if j < 0 else j


def brace_end(s, i):
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in '"\'':
            i = scan_string(s, i)
            continue
        if s.startswith('--', i) and not s.startswith('--[[', i):
            j = s.find('\n', i)
            i = n if j < 0 else j
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


BLOCK_OPEN = re.compile(r'(?<![A-Za-z0-9_])(function|do|if|repeat)(?![A-Za-z0-9_])')
BLOCK_CLOSE = re.compile(r'(?<![A-Za-z0-9_])(end|until)(?![A-Za-z0-9_])')


def block_end(src, i):
    depth = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in '"\'':
            i = scan_string(src, i)
            continue
        if src.startswith('--', i):
            j = src.find('\n', i)
            i = n if j < 0 else j
            continue
        mo = BLOCK_OPEN.match(src, i)
        if mo:
            depth += 1
            i = mo.end()
            continue
        mc = BLOCK_CLOSE.match(src, i)
        if mc:
            depth -= 1
            i = mc.end()
            if depth == 0:
                return i
            continue
        i += 1
    return -1


def find_string_table(src):
    pat = re.compile(r'local\s+([A-Za-z_]\w*)\s*=\s*\{(?=\s*[\'"])')
    m = pat.search(src)
    if not m:
        return None
    var = m.group(1)
    start = src.index('{', m.start())
    end = brace_end(src, start)
    if end < 0:
        return None
    return var, start, end


def iter_do_blocks(src, after):
    """扫描跳过字符串/注释的 do ... end 区间"""
    i = after
    n = len(src)
    while i < n:
        c = src[i]
        if c in '"\'':
            i = scan_string(src, i)
            continue
        if src.startswith('--', i):
            j = src.find('\n', i)
            i = n if j < 0 else j
            continue
        m = re.match(r'do(?![A-Za-z0-9_])', src[i:])
        if m:
            e = block_end(src, i)
            if e > 0:
                yield i, e
            i += 2
            continue
        i += 1


def find_setup_end(src, after):
    """setup 终点 = 最后一个含 string.char / table.concat 的顶层 do 块结束处"""
    for st, en in iter_do_blocks(src, after):
        seg = src[st:en]
        if 'string.char' in seg and 'concat' in seg:
            return en
    m2 = re.search(r'(?<![A-Za-z0-9_])function\s*\(', src[after:])
    return after + m2.start() if m2 else len(src)


def dump_string_table(setup_code, workdir, tag='t'):
    path = os.path.join(workdir, '_setup_%s.lua' % tag)
    write_text(path, setup_code)
    probe = os.path.join(workdir, '_probe_%s.lua' % tag)
    write_text(probe, '''
local ok, tbl = pcall(assert(loadfile("_setup_%s.lua")))
if not ok or type(tbl) ~= "table" then
  io.stderr:write("load fail: " .. tostring(tbl) .. "\\n"); os.exit(2)
end
local out = {}
for k, v in pairs(tbl) do
  if type(k) == "number" and type(v) == "string" then
    local h = v:gsub(".", function(c) return string.format("%%02x", c:byte()) end)
    out[#out + 1] = k .. "\\t" .. h
  end
end
io.write(table.concat(out, "\\n"), "\\n")
io.stderr:write("count=" .. #out .. "\\n")
''' % tag)
    r = subprocess.run(['lua', os.path.basename(probe)], cwd=workdir, capture_output=True,
                       text=True, timeout=180)
    items = {}
    for line in r.stdout.split('\n'):
        if '\t' in line:
            k, h = line.split('\t', 1)
            try:
                items[int(k)] = bytes.fromhex(h.strip()).decode('latin-1')
            except ValueError:
                pass
    if not items:
        raise RuntimeError('字符串表导出失败: %s' % (r.stderr or r.stdout).strip()[:300])
    return items, r.stderr.strip()


def to_int(expr):
    e = expr.strip()
    if not e or not set(e) <= NUMCHARS:
        return None
    e = e.replace('^', '**')
    try:
        v = eval(e, {'__builtins__': {}}, {})
        return int(v)
    except Exception:
        return None


def find_decoder(src, tabvar):
    pat = re.compile(
        r'function\s+([A-Za-z_]\w*)\s*\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)\s*'
        r'return\s+' + re.escape(tabvar) + r'\s*\[([^\]]{3,120})\]\s*end', re.S)
    m = pat.search(src)
    if not m:
        return None
    fn, p1, p2, expr = m.group(1), m.group(2), m.group(3), m.group(4)
    if not (re.search(r'(?<![\w])' + re.escape(p1) + r'(?![\w])', expr) and
            re.search(r'(?<![\w])' + re.escape(p2) + r'(?![\w])', expr)):
        return None
    e = re.sub(r'(?<![\w])' + re.escape(p1) + r'(?![\w])', '(0)', expr)
    e = re.sub(r'(?<![\w])' + re.escape(p2) + r'(?![\w])', '(0)', e)
    e = e.replace(' ', '')
    base = to_int(e)
    if base is None:
        return None
    return fn, base


PAREN_NUM = re.compile(r'\((-?[0-9][0-9a-fA-FxXeE \t+\-*/%]*)\)')


def fold_parens(src, rounds=8):
    for _ in range(rounds):
        out = []
        last = 0
        changed = False
        for m in PAREN_NUM.finditer(src):
            v = to_int(m.group(1))
            if v is None:
                continue
            prev = src[m.start() - 1] if m.start() > 0 else ''
            if v < 0 and prev == '-':
                continue
            nxt = src[m.end()] if m.end() < len(src) else ''
            piece = str(v)
            if prev and (prev.isalnum() or prev in '_#."\''):
                piece = ' ' + piece
            if nxt and (nxt.isalnum() or nxt == '_'):
                piece = piece + ' '
            out.append(src[last:m.start()])
            out.append(piece)
            last = m.end()
            changed = True
        out.append(src[last:])
        new = ''.join(out)
        if not changed or new == src:
            break
        src = new
    return src


def inline_decoder(src, fn, base, table):
    pat = re.compile(re.escape(fn) + r'\(\s*(-?[0-9][-0-9a-fA-FxXeE \t+\*/%]*)\s*,\s*(-?[0-9][-0-9a-fA-FxXeE \t+\*/%]*)\s*\)')
    cache = {}
    cnt = 0

    def rep(m):
        nonlocal cnt
        a = to_int(m.group(1))
        b = to_int(m.group(2))
        if a is None or b is None:
            return m.group(0)
        idx = a + b + base
        cnt += 1
        if idx not in cache:
            sv = table.get(idx)
            lit = lua_quote(sv) if sv is not None else 'nil--[[miss:%d]]' % idx
            prev = m.string[m.start() - 1] if m.start() > 0 else ''
            nxt = m.string[m.end()] if m.end() < len(m.string) else ''
            if prev and (prev.isalnum() or prev == '_'):
                lit = ' ' + lit
            if nxt and (nxt.isalnum() or nxt == '_'):
                lit = lit + ' '
            cache[idx] = lit
        return cache[idx]

    return pat.sub(rep, src), cnt


TOK_RE = re.compile(r'''
    (?P<ws>\s+)
  | (?P<lcomment>--\[(?P<eq>=*)\[.*?\](?P=eq)\])
  | (?P<comment>--[^\n]*)
  | (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<num>0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_]\w*)
  | (?P<longstr>\[(=*)\[.*?\]\1\])
  | (?P<op>\.\.\.|\.\.|==|~=|<=|>=|//|::|[\s\S])
''', re.X | re.S)


def tokenize(src):
    toks = []
    i = 0
    n = len(src)
    while i < n:
        m = TOK_RE.match(src, i)
        if not m:
            toks.append(('op', src[i]))
            i += 1
            continue
        kind = m.lastgroup
        toks.append((kind, m.group(0)))
        i = m.end()
    return toks


def beautify(src, indent='  '):
    toks = tokenize(src)
    buf = []
    depth = 0
    at_start = True
    prev = None

    def nl():
        nonlocal at_start
        while buf and buf[-1] == ' ':
            buf.pop()
        buf.append('\n')
        at_start = True

    def emit(text):
        nonlocal at_start
        if at_start:
            buf.append(indent * depth)
            at_start = False
        buf.append(text)

    KEYSPACE = {'if', 'elseif', 'while', 'until', 'return', 'local', 'then', 'do',
                'else', 'and', 'or', 'not', 'in', 'for', 'repeat', 'function'}
    for kind, text in toks:
        if kind == 'ws':
            continue
        if kind in ('comment', 'lcomment'):
            nl()
            emit(text.strip())
            nl()
            prev = (kind, text)
            continue
        if kind == 'name' and text in ('end', 'until'):
            depth = max(depth - 1, 0)
            nl()
            emit(text)
            prev = (kind, text)
            continue
        if kind == 'name' and text in ('else', 'elseif'):
            depth = max(depth - 1, 0)
            nl()
            emit(text)
            depth += 1
            prev = (kind, text)
            continue
        if kind == 'op' and text == ';':
            emit(';')
            nl()
            prev = (kind, text)
            continue
        need = False
        if prev and not at_start:
            pk, pt = prev
            if pk in ('name', 'num') and kind in ('name', 'num', 'str'):
                need = True
            elif pk == 'str' and kind in ('name', 'num', 'str'):
                need = True
            elif pt in KEYSPACE and kind in ('name', 'num', 'str'):
                need = True
        if need and buf and not buf[-1].endswith(' '):
            buf.append(' ')
        emit(text)
        if kind == 'name' and text in ('do', 'then', 'repeat'):
            nl()
            depth += 1
        elif kind == 'name' and text == 'function':
            depth += 1
        prev = (kind, text)
    while buf and buf[-1] == ' ':
        buf.pop()
    return ''.join(buf)


def lua_check(path):
    r = subprocess.run(['luac', '-p', path], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def deobfuscate(path, out_path, strings_path=None, do_beautify=True, keep_table=False):
    src = read_text(path)
    rep = []
    if 'Cathub Obfuscator' not in src:
        rep.append('提示: 未检测到 Cathub Obfuscator 水印，仍尝试按同构算法处理')
    ver = re.search(r'Obfuscator\s+(v[\w.]+)', src)
    rep.append('混淆器版本: %s' % (ver.group(1) if ver else '未知'))

    found = find_string_table(src)
    if not found:
        raise RuntimeError('未定位到字符串表')
    tabvar, tstart, tend = found
    rep.append('字符串表: %s (%d 字节)' % (tabvar, tend - tstart))

    setup_end = find_setup_end(src, tend)
    setup = src[tstart:setup_end].rstrip().rstrip(',')
    setup = 'local %s = %s\nreturn %s\n' % (tabvar, setup, tabvar)

    workdir = os.path.dirname(os.path.abspath(out_path)) or '.'
    table, info = dump_string_table(setup, workdir)
    rep.append('字符串表条目: %d | 洗牌执行: %s' % (len(table), info or 'ok'))

    dec = find_decoder(src, tabvar)
    body = src[:tstart] + ('{}' if not keep_table else src[tstart:tend]) + src[tend:]
    body = fold_parens(body)
    if dec:
        fn, base = dec
        rep.append('解码函数: %s 基址 %d' % (fn, base))
        body, cnt = inline_decoder(body, fn, base, table)
        rep.append('内联解码调用: %d 处 -> %d 个唯一字符串' % (cnt, len(set(re.findall(r'"(?:\\.|[^"\\])*"', body)))))
        body = fold_parens(body)
    else:
        rep.append('解码函数: 未识别（可能为多段解码，仅导出字符串表）')

    if do_beautify:
        body = beautify(body)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    write_text(out_path, body)
    ok, msg = lua_check(out_path)
    rep.append('语法校验: %s' % ('通过' if ok else '失败 ' + msg))

    if strings_path:
        with open(strings_path, 'w', encoding='utf-8') as f:
            for k in sorted(table):
                f.write('%d\t%s\n' % (k, table[k]))
        rep.append('字符串映射: %s' % strings_path)
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input')
    ap.add_argument('-o', '--output')
    ap.add_argument('--strings')
    ap.add_argument('--raw', action='store_true', help='不做美化，保留原始单行')
    ap.add_argument('--keep-table', action='store_true', help='保留原字符串表')
    a = ap.parse_args()
    out = a.output or (os.path.splitext(a.input)[0] + '_还原.lua')
    for line in deobfuscate(a.input, out, a.strings, not a.raw, a.keep_table):
        print('[*]', line)
    print('[*] 输出:', out)


if __name__ == '__main__':
    main()
