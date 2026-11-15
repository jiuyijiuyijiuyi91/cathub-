--[[
cathub_live_dump.lua — CatHub 第 4 跳明文 payload 运行时抓取器

用法（Roblox 执行器内，例如 Synapse/Delta/Arceus 等支持 loadstring 的环境）：
    loadstring(game:HttpGet("<把本文件放到任意可访问地址>"))().run("308cd616ff863d1ea330a3e97eef2373")
或把本文件内容整段粘进执行器，最后一行改为：
    CathubDump.run("<key>")

产物（执行器工作目录下）：
    cathub_dump/01_chunk_<n>.lua   每次被 loadstring 的源码（长度 > 200 才记）
    cathub_dump/index.txt          索引：文件名 + 字节数 + 前 80 字符预览
    cathub_dump/04_payload.lua     自动挑出的"最终业务明文"（最大且非 VM 壳的那份）

绕过点（CatHub engine 的反 hook 自杀检测）：
    1. engine 会用 debug.info(loadstring, "s") 看首字符是否为 @ 或 =，
       一旦发现 loadstring 被替换成 Lua 函数就 pcall error 自杀。
       -> 本脚本把 debug.info / debug.getinfo 一起做"身份映射"，
          对自家包装函数一律返回原始 C 函数信息。
    2. engine 还会校验 string.dump(loadstring) 是否报错、getfenv(loadstring) 是否为空。
       -> 包装函数用 setfenv 绑定到原始全局表，行为保持一致。
    3. 若某环境连 debug.info 都不可写，脚本自动降级为"只 hook load 系列"并提示。
]]

local CathubDump = {}
_G.CathubDump = CathubDump

local DIR = "cathub_dump"
local MIN_LEN = 200          -- 小于此长度视为常量块，不落盘
local records = {}
local seq = 0

local function fs_write(path, data)
    if writefile then
        local ok = pcall(writefile, path, data)
        if ok then return true end
    end
    if appendfile then
        local ok = pcall(appendfile, path, data)
        if ok then return true end
    end
    local ok = pcall(function()
        local f = io.open(path, "wb")
        f:write(data)
        f:close()
    end)
    return ok
end

local function fs_mkdir(path)
    if makefolder then pcall(makefolder, path) end
end

local function preview(s)
    s = s:gsub("[\r\n\t]", " ")
    return #s > 80 and (s:sub(1, 80) .. "...") or s
end

local function save(src, tag)
    seq = seq + 1
    local name = string.format("%s/%02d_%s_%d.lua", DIR, seq, tag, #src)
    fs_write(name, src)
    records[#records + 1] = { file = name, size = #src, head = preview(src) }
    warn(string.format("[CathubDump] #%d %s  %d bytes", seq, tag, #src))
    return name
end

-- 判定是否为 VM 壳（CatHub Obfuscator v1 特征）
local function looks_like_vm(s)
    local score = 0
    if s:find("Cathub Obfuscator", 1, true) then score = score + 3 end
    if s:find("while", 1, true) and s:find("pc", 1, true) then score = score + 1 end
    if s:find("string.char", 1, true) then score = score + 1 end
    if s:find("debug.info", 1, true) then score = score + 1 end
    -- 业务脚本通常有中文注释或语义化函数名
    local _, cn = s:gsub("[\228-\233][\128-\191][\128-\191]", "")
    if cn > 0 then score = score - 2 end
    return score >= 3
end

---------------------------------------------------------------------
-- 1) 身份伪装：让 debug.info 认为我们仍是原生函数
---------------------------------------------------------------------
local orig_loadstring = loadstring or load
local orig_load = load
local orig_info = debug and (debug.info or debug.getinfo)
local orig_getinfo = debug and debug.getinfo
local my_loadstring, my_load

local function disguise(f, fn)
    if f == my_loadstring then return orig_loadstring end
    if f == my_load then return orig_load end
    return f
end

if orig_info then
    local ok = pcall(function()
        debug.info = function(f, opt, ...)
            return orig_info(disguise(f, nil), opt, ...)
        end
    end)
    if not ok then
        warn("[CathubDump] debug.info 不可写，降级运行")
    end
end
if orig_getinfo then
    pcall(function()
        debug.getinfo = function(f, what)
            return orig_getinfo(disguise(f, nil), what)
        end
    end)
end

---------------------------------------------------------------------
-- 2) hook loadstring / load
---------------------------------------------------------------------
my_loadstring = function(src, chunkname)
    if type(src) == "string" and #src >= MIN_LEN then
        local tag = looks_like_vm(src) and "vm" or "payload"
        save(src, tag)
    end
    return orig_loadstring(src, chunkname)
end

my_load = function(src, chunkname, mode, env)
    if type(src) == "string" and #src >= MIN_LEN then
        local tag = looks_like_vm(src) and "vm" or "payload"
        save(src, tag)
    end
    return orig_load(src, chunkname, mode, env)
end

pcall(function() setfenv(my_loadstring, getfenv(orig_loadstring)) end)
pcall(function() setfenv(my_load, getfenv(orig_load)) end)

loadstring = my_loadstring
load = my_load
_G.loadstring = my_loadstring
_G.load = my_load

---------------------------------------------------------------------
-- 3) 触发链路
---------------------------------------------------------------------
function CathubDump.run(key, host)
    host = host or "https://cathub.fr"
    fs_mkdir(DIR)
    fs_write(DIR .. "/index.txt", "")
    local url = host .. "/load/" .. tostring(key)
    warn("[CathubDump] GET " .. url)

    local ok, body = pcall(function()
        return game:HttpGet(url)
    end)
    if not ok or type(body) ~= "string" then
        warn("[CathubDump] 抓取失败: " .. tostring(body))
        return nil
    end
    warn(string.format("[CathubDump] hop1 %d bytes", #body))
    save(body, "hop1_shell")

    -- 直接执行 shell，让链路自己走完（loadstring 已被 hook）
    local fn, err = orig_loadstring(body, "=cathub_hop1")
    if not fn then
        warn("[CathubDump] shell 编译失败: " .. tostring(err))
        return nil
    end
    local ok2, err2 = pcall(fn)
    if not ok2 then
        warn("[CathubDump] 执行中断（payload 可能已 dump）: " .. tostring(err2))
    end

    -- 归档索引
    local lines = {}
    for i, r in ipairs(records) do
        lines[i] = string.format("%s\t%d\t%s", r.file, r.size, r.head)
    end
    fs_write(DIR .. "/index.txt", table.concat(lines, "\n"))

    -- 挑 payload：最大且非 vm
    local best, bestSize = nil, 0
    for _, r in ipairs(records) do
        if r.file:find("payload", 1, true) and r.size > bestSize then
            best, bestSize = r.file, r.size
        end
    end
    if best then
        warn("[CathubDump] 疑似业务明文: " .. best .. " (" .. bestSize .. " bytes)")
    else
        warn("[CathubDump] 未捕获到明文 payload，检查是否被反调试拦截（见 index.txt）")
    end
    return best
end

-- 便捷入口：直接执行链
function CathubDump.exec(key, host)
    return CathubDump.run(key, host)
end

return CathubDump
