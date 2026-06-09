-- ============================================
-- Satellaview (BS-X) 综合同步监控
-- 开发版本：2026.06.09 (macOS 兼容修复)
-- 作者：AcFun 游戏咖啡馆 & AI Collaborator
-- ============================================

-- 缓存常用全局函数，提升每帧执行效率
local read       = emu.read
local log        = emu.log
local getState   = emu.getState
local setInput   = emu.setInput
local snesMem    = emu.memType.snesMemory
local bsxMem     = emu.memType.bsxMemoryPack
local sramMem    = emu.memType.snesSaveRam

-- ====== 1. 内存地址与常量配置 ======
-- [系统信号]
local ADDR_SIGNAL        = 0x5BC9B      -- 关键：BS 周任务信号地址 (位于 BS-X Memory Pack 映射区)
local ADDR_LOAD_DONE     = 0xFFFFF      -- 硬件挂载标志：当此地址被写入时，代表模拟器已加载完.BS ROM
local ADDR_GENDER        = 0x10501C     -- 玩家性别存储位 (1:女, 0:男)
local ADDR_SETTLE_STATE  = 0x7FFFFF     -- 结算状态触发位 (游戏时间57分钟，通常对应 0x39)
local ADDR_PLOT_STATE    = 0x7E2000     -- 剧情逻辑开关：0x0B 代表加农在出现，触发 SoundLink 静音

-- [游戏数值统计 - SRAM 偏移]
local ADDR_DEATHS        = 0x263A       -- 死亡次数累计
local ADDR_HEART         = 0x2636       -- 角色损失的心数量 (原始值需除以2)
local ADDR_RUPEE         = 0x201F       -- 卢比地址
local ADDR_TRIFORCE      = 0x2022       -- 三角力碎片收集状态 (按位存储)
local ADDR_GANON_DEFEAT  = 0x263C       -- 加农击破标志

-- [同步参数]
local TARGET_FRAME       = 24600        -- 理想跳转帧 (对应真实时间线)
local LATE_DELAY         = 120          -- 迟到玩家直接跳转

-- [外部同步文件]
local SIGNAL_FILE        = "chapter_signal.txt"  -- 章节和性别切换信号
local LOAD_FILE          = "load_complete.txt"   -- 载入完成信号
local RESULT_FILE        = "result_data.txt"     -- 成绩记录
local TRIGGER_FILE       = "settle_trigger.txt"  -- 结算信号
local GANON_SPAWN_FILE   = "ganon_spawn.txt"     -- 触发加农战信号

-- ====== 2. 运行时状态变量 ======
local HAS_LOADED         = false
local LOAD_TIME_FRAME    = 0            -- 记录玩家下载完成帧
local HAS_TRIGGERED_SETTLE = false
local HAS_PRESSED_A      = false        -- 入场 A 键同步拦截位
local HAS_GANON_SPAWNED  = false        -- 加农出现状态拦截位
local is_input_disabled  = false        -- 默认放行按键
local LAST_SIGNAL        = "FF"         -- 上次记录的章节信号

local LAST_DEATHS, LAST_HEARTS, LAST_RUPEES, LAST_TRIFORCE, LAST_GANON = -1, -1, -1, -1, -1
local frameCounter       = 0
local checkInterval      = 4            -- 每 4 帧检查一次
local load_callback_handle = nil        -- 防重复注册的硬件回调句柄

-- ====== 3. 基础文件操作 ======
local cachedDataFolder = nil
local function writeToFile(filename, content)
    if not cachedDataFolder then
        local folder = emu.getScriptDataFolder()
        if type(folder) == "string" and folder ~= "" then
            -- 追加 /bs 子目录（若路径尚未以 /bs 或 \bs 结尾）
            local _, endPos = string.find(folder, "[/\\]bs$")
            if not endPos then
                folder = folder .. "/bs"
            end
            cachedDataFolder = folder
        else
            -- macOS/受限环境：尝试 emu.getPath(14) 作为备选
            pcall(function() folder = emu.getPath(14) end)
            if type(folder) == "string" and folder ~= "" then
                cachedDataFolder = folder .. "/bs"
            else
                -- 最终降级：手动构造路径
                local home = os.getenv("HOME") or os.getenv("USERPROFILE") or "."
                if home == "." then
                    cachedDataFolder = "LuaScriptData\\bs"
                else
                    cachedDataFolder = home .. "/Library/Application Support/MesenCE/LuaScriptData/bs"
                end
                log("[bs.lua] 使用降级路径: " .. cachedDataFolder)
                pcall(function()
                    os.execute("mkdir -p '" .. cachedDataFolder:gsub("'", "'\\''") .. "'")
                end)
            end
        end
    end

    local path = cachedDataFolder .. "/" .. filename
    local file, err = io.open(path, "w")
    if file then
        file:write(content)
        file:close()
    else
        log(string.format("[bs.lua] 无法写入 %s: %s", filename, (err or "未知错误")))
    end
end

-- ====== 4. 核心事件回调注册 ======

-- 手柄轮询级输入拦截，用于看剧情等待时间
-- 一旦处于前置锁死状态（is_input_disabled == true），玩家手柄将完全失灵
emu.addEventCallback(function()
    if is_input_disabled then
        local block_input = {
            a = false, b = false, x = false, y = false, 
            up = false, down = false, left = false, right = false, 
            select = false, start = false, l = false, r = false
        }
        setInput(block_input, 0)
    end
end, emu.eventType.inputPolled)


-- 硬件加载 Hook：当检测到 ROM 挂载完成时触发
local function onHardwareLoadWrite(address, value)
    if not HAS_LOADED then
        writeToFile(LOAD_FILE, "1")
        HAS_LOADED = true
        LOAD_TIME_FRAME = getState().frameCount
        
        -- 锁死玩家的手柄输入
        is_input_disabled = true
        log(string.format("Memory Pack 已挂载。记录入场帧: %d。进入前置锁死状态，禁止玩家操作！", LOAD_TIME_FRAME))
    end
end

-- ====== 5. 核心数据与时序监控逻辑 ======
function monitorEverything()
    frameCounter = frameCounter + 1
    if frameCounter < checkInterval then return end
    frameCounter = 0

    local state = getState()
    local currentFrame = state.frameCount

    -- 1. 加农剧情监测
    local plotState = read(ADDR_PLOT_STATE, snesMem, false)
    if plotState == 0x0B then
        if not HAS_GANON_SPAWNED then
            writeToFile(GANON_SPAWN_FILE, "1")
            HAS_GANON_SPAWNED = true
            log("检测到状态 0B：加农已出现")
        end
    else
        if HAS_GANON_SPAWNED and plotState ~= 0x0B then
            HAS_GANON_SPAWNED = false
            writeToFile(GANON_SPAWN_FILE, "0")
            log("加农房状态已结束")
        end
    end

    -- 2. 自动 A 键同步
    if HAS_LOADED and not HAS_PRESSED_A then
        local shouldPress = false
        if LOAD_TIME_FRAME < TARGET_FRAME then
            if currentFrame >= TARGET_FRAME then shouldPress = true end
        else
            if currentFrame >= (LOAD_TIME_FRAME + LATE_DELAY) then shouldPress = true end
        end

        if shouldPress then
            -- 到了自动按 A 键切入游戏的瞬间，立刻解除操作拦截锁
            is_input_disabled = false
            
            -- 下发核心入场 A 键指令，由于上一行锁已解，这一发 A 键将拥有最高时序优先级，直接送入游戏
            setInput({ a = true }, 0)
            HAS_PRESSED_A = true
            log(string.format("触发入场 A 键，操作锁定安全解除！玩家恢复自由控制  当前帧: %d", currentFrame))
        end
    end

    -- 3. 章节信号与性别监测
    local rawSignal = read(ADDR_SIGNAL, bsxMem, false)
    if rawSignal and rawSignal >= 0 and rawSignal <= 3 then
        local genderVal = read(ADDR_GENDER, snesMem, false)
        local genderSuffix = (genderVal == 1) and "g" or "b"
        local currentCombined = (rawSignal + 1) .. genderSuffix

        if currentCombined ~= LAST_SIGNAL then
            writeToFile(SIGNAL_FILE, currentCombined) 
            LAST_SIGNAL = currentCombined
            log("当前章节状态同步为: " .. currentCombined)
        end
    end

    -- 4. 结算触发检测
    local settleState = read(ADDR_SETTLE_STATE, snesMem, false)
    if settleState == 0x39 then
        if not HAS_TRIGGERED_SETTLE then
            writeToFile(TRIGGER_FILE, "READY")
            HAS_TRIGGERED_SETTLE = true
            log("检测到 0x39 信号，准备抓取 SRAM 数据。")
        end
    else
        HAS_TRIGGERED_SETTLE = false
    end

    -- 5. 结算数据统计
    local rawDeaths   = emu.read16(ADDR_DEATHS, sramMem, false) or 0
    local rawHeartVal = emu.read16(ADDR_HEART, sramMem, false) or 0
    local heartLoss   = math.floor(rawHeartVal / 2)
    local totalRupees = emu.read16(ADDR_RUPEE, sramMem, false) or 0
    local triforce    = emu.read(ADDR_TRIFORCE, sramMem, false) or 0
    local ganonDefeat = emu.read(ADDR_GANON_DEFEAT, sramMem, false) or 0

    if rawDeaths ~= LAST_DEATHS or heartLoss ~= LAST_HEARTS or 
       totalRupees ~= LAST_RUPEES or triforce ~= LAST_TRIFORCE or 
       ganonDefeat ~= LAST_GANON then
        
        local resultData = string.format("DEATH:%d|HEART_LOSS:%d|RUPEE:%d|TRIFORCE:%02X|GANON:%d", 
                                         rawDeaths, heartLoss, totalRupees, triforce, ganonDefeat)
        writeToFile(RESULT_FILE, resultData)
        LAST_DEATHS, LAST_HEARTS, LAST_RUPEES, LAST_TRIFORCE, LAST_GANON = 
            rawDeaths, heartLoss, totalRupees, triforce, ganonDefeat
        log("SRAM 战绩更新: " .. resultData)
    end
end

-- ====== 6. 系统初始化与重置 ======
local function resetSystem()
    HAS_LOADED = false
    LOAD_TIME_FRAME = 0
    HAS_TRIGGERED_SETTLE = false
    HAS_PRESSED_A = false
    HAS_GANON_SPAWNED = false  
    LAST_SIGNAL = "FF"
    LAST_DEATHS, LAST_HEARTS, LAST_RUPEES, LAST_TRIFORCE, LAST_GANON = -1, -1, -1, -1, -1
    
    -- 输入锁定开关初始化重置
    is_input_disabled = false
    
    writeToFile(SIGNAL_FILE, "FF")
    writeToFile(LOAD_FILE, "0")
    writeToFile(TRIGGER_FILE, "IDLE") 
    writeToFile(GANON_SPAWN_FILE, "0")
    
    if not load_callback_handle then
        load_callback_handle = emu.addMemoryCallback(onHardwareLoadWrite, emu.callbackType.write, ADDR_LOAD_DONE, ADDR_LOAD_DONE, emu.cpuType.snes, bsxMem)
    end
    
    log("==========================================")
    log("  BS Zelda 同步脚本已重置：[综合监控已就绪]  ")
    log("==========================================")
end

emu.addEventCallback(monitorEverything, emu.eventType.frameEnd)
emu.addEventCallback(resetSystem, emu.eventType.reset)

resetSystem()