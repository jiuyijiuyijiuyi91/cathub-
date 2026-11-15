
local f = assert(loadfile("setup.lua"))
local tbl, err = f()
if type(tbl) ~= "table" then io.stderr:write("load fail: " .. tostring(err) .. "\n"); os.exit(2) end
local n = 0
for k, v in pairs(tbl) do
  if type(k) == "number" and type(v) == "string" then
    n = n + 1
    io.write(k, "\t", (v:gsub(".", function(c) return string.format("%02x", c:byte()) end)), "\n")
  end
end
io.stderr:write("count=" .. n .. "\n")
