# CatHub 反混淆工具链

针对 `Cathub Obfuscator v1` / CatHub loader 的成套还原工具：**静态反混淆器 + 链路抓取器 + 运行时 dump 脚本**。

## 一、先搞清楚这条链路（决定了工具该怎么分工）

```
key
 └─ hop1  cathub.fr/load/<key>          → 壳脚本（loadstring(HttpGet(...))）
     └─ hop2  /load/init-<n>-<hash>.lua → ER 明文加载器（CONFIG 表 / host 识别 / 反调试）
         └─ hop3  /v1/loader/<key>/t    → 一次性 token，再 /h?tk= 取 engine
             └─ hop4  engine 里 VM 执行后 base64 下发 → 业务功能脚本（本来就是明文）
```

关键结论：

| 层 | 是否真混淆 | 能不能纯静态出源码 |
|---|---|---|
| 壳 / ER 加载器 | 否，仅变量名与算术膨胀 | **能**，工具直接还原 |
| engine（CatHub Obfuscator v1） | 是，唯一真混淆层（VM 解释器，144 个 `if pc` 分叶对应 opcode） | **能还原到可读 VM 骨架**，但它是虚拟机，不是业务代码 |
| hop4 业务脚本 | **否**，只是 base64 传输 | 静态拿不到（要 token 且一次性），**必须运行时 dump**，dump 出来就是中文注释齐全的明文 |

所以「反混淆出源码」= 静态还原 hop1~hop3 + 运行时截获 hop4。本工具链覆盖这两段。

## 二、三个工具

### 1) `cathub_deob.py` — 静态反混淆器（核心）

```bash
python3 cathub_deob.py <样本.lua> -o 还原.lua --strings 字符串表.txt
```

做的四件事：

1. **字符串表抽取**：定位 `_0x?????={...}`，把 174 / 496 项原样导出
2. **洗牌求解**：把 `do ... while ... end` 的 Fisher-Yates 块单独拿去做 `lua` 子进程执行，得到真实下标顺序（不靠猜）
3. **解密执行**：定位含 `string.char` + `table.concat` 的 `do` 块，**执行它**——CatHub 的解密就发生在 setup 阶段，跑完表里已是明文（`'!'` 前缀 = 需要解码，`']'` 前缀 = 原文）
4. **内联 + 折叠 + 美化**：把 `f(a,b)` 形式的取串调用直接替换成字面量，折叠 `(a+b)` 常量表达式，再按 Lua 语法重排缩进，最后 `luac -p` 自检

实测（本机 Linux + lua5.4）：

| 样本 | 原大小 | 字符串表 | 内联调用 | 可读明文串 | 语法校验 |
|---|---|---|---|---|---|
| loader2.lua | 106 KB | 174 项 | 141 处 → 117 唯一 | `HttpGet` `loadstring` `game` `debug` `os` `setmetatable` … | **通过** |
| engine7.lua | 354 KB | 496 项 | 399 处 → 284 唯一 | `GetService` `http_request` `getgenv` `writefile` `delfile` `__index` `__gc` … | **通过** |

> 表里约 1/3 项解出来是二进制/中文 UTF-8，属正常：CatHub 会把部分串二次加密或塞诱饵项，被内联到代码里的都是可读标识符。

### 2) `cathub_pull.py` — 链路抓取器

```bash
# 有 key：全链路抓取 + 每层自动还原
python3 cathub_pull.py --key <key> -o out/

# 只有本地文件：离线还原
python3 cathub_pull.py --local loader2.lua engine7.lua -o out/
```

注意：UA 必须是 `Roblox/WinInet`，普通浏览器 UA 会被边缘节点直接 403；key 失效时 hop1 返回 `Unknown loader`。

### 3) `cathub_live_dump.lua` — 运行时 payload 截获（执行器内）

```lua
loadstring(game:HttpGet("<本文件地址>"))().run("<key>")
```

产出 `cathub_dump/` 目录：每次 `loadstring` 的源码（>200 字节）+ `index.txt` + 自动挑出的业务明文。

**它专治 engine 的反 hook 自杀**：engine 会 `debug.info(loadstring,'s')` 看首字符是不是 `@`/`=`，发现是 Lua 包装函数就直接 `error`。脚本把 `debug.info` / `debug.getinfo` 一起做身份映射，对自家包装函数一律返回**原生 C 函数**的信息，同时 `setfenv` 保持行为一致，所以 engine 看不出被劫持。

## 三、典型工作流

```bash
# 1. 抓链 + 静态还原
python3 cathub_pull.py --key 308cd616ff863d1ea330a3e97eef2373 -o run_20261115/

# 2. 手机/PC 执行器里跑 cathub_live_dump.lua，导出 cathub_dump/
#    （拿到 hop4 明文后，第 3 步可跳过）

# 3. 若只想继续深挖 engine，对 hop3 产物反复过一遍
python3 cathub_deob.py run_20261115/hop3_engine.lua -o run_20261115/engine.deob.lua
```

## 四、限制（别踩的坑）

- **不要对 hop4 明文 payload 再跑反混淆**：它没有 `_0x` 表、没有 `while pc` 状态机，跑了只会白费。
- **engine 还原的天花板是"可读 VM 骨架"**：业务 URL、门禁、key 是在运行时往 VM 常量池注册的，静态看不到。
- **本地 Lua 5.4 沙箱跑不动完整 loader**：会撞 `bytecode corrupt` 自毁，依赖 Luau 的 `typeof` / `debug.info` / `getfenv`。要动态验证只能上真执行器。
- **一次性 token**：hop3 拿到的 token 有 TTL（约 60s）且只给一次，抓链脚本必须立刻用掉。

## 五、环境依赖

- Python 3.8+（只用标准库）
- `lua` + `luac`（5.1~5.4 任一，用于执行洗牌/解密块与语法自检）
  Ubuntu/Debian: `apt install lua5.4`
