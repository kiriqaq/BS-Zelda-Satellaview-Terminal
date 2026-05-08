-- ===================================
-- Satellaview (BS-X) 综合同步监控脚本
-- 开发版本：2026.05.09 Final
-- 作者：AcFun 游戏咖啡馆
-- ===================================

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
-- SaveRAM 区域，记录玩家成绩
local ADDR_DEATHS        = 0x263A       -- 死亡次数累计
local ADDR_HEART_VAL     = 0x2636       -- 角色损失的心数量 (原始值需除以2)
local ADDR_RUPEE_LOW     = 0x201F       -- 卢比低位地址
local ADDR_TRIFORCE      = 0x2022       -- 三角力碎片收集状态 (按位存储)
local ADDR_GANON_DEFEAT  = 0x263C       -- 加农击破标志

-- [同步参数]
-- 因PSRAM丢失，使用模拟按键执行游戏本体
local TARGET_FRAME       = 24640        -- 理想跳转帧 (对应真实时间线)
local LATE_DELAY         = 120           -- 迟到玩家直接跳转

-- [外部同步文件]
local SIGNAL_FILE        = "chapter_signal.txt"  -- 章节和性别切换信号
local LOAD_FILE          = "load_complete.txt"   -- 载入完成信号
local RESULT_FILE        = "result_data.txt"     -- 成绩记录
local TRIGGER_FILE       = "settle_trigger.txt"  -- 结算信号
local GANON_SPAWN_FILE   = "ganon_spawn.txt"     -- 触发加农战信号

-- ====== 2. 运行时状态变量 ======
local LAST_SIGNAL        = "FF"
local HAS_LOADED         = false
local LOAD_TIME_FRAME    = 0            -- 记录玩家下载完成帧
local HAS_TRIGGERED_SETTLE = false
local HAS_PRESSED_A      = false        -- 入场 A 键同步拦截位
local HAS_GANON_SPAWNED  = false        -- 加农出现状态拦截位

local LAST_DEATHS, LAST_HEARTS, LAST_RUPEES, LAST_TRIFORCE, LAST_GANON = -1, -1, -1, -1, -1
local frameCounter       = 0
local checkInterval      = 4            -- 每 4 帧检查一次

-- ====== 3. 基础文件操作 ======
-- 必须开启 Allow access to I/O and OS functions
local cachedDataFolder = nil
local function writeToFile(filename, content)
    if not cachedDataFolder then
        cachedDataFolder = emu.getScriptDataFolder()
        if not cachedDataFolder or cachedDataFolder == "" then return end
    end
    
    local path = cachedDataFolder .. "\\" .. filename
    local file, err = io.open(path, "w")
    if file then
        file:write(content)
        file:close()
    else
        log(string.format("无法写入文件 %s: %s", filename, (err or "未知错误")))
    end
end

-- ====== 4. 硬件加载 Hook ======
-- 当 Mesen 加载 ROM 完成时触发，模拟卫星信号就绪
local function onHardwareLoadWrite(address, value)
    if not HAS_LOADED then
        writeToFile(LOAD_FILE, "1")
        HAS_LOADED = true
        LOAD_TIME_FRAME = getState().frameCount
        log(string.format("Memory Pack 已挂载。记录入场帧: %d", LOAD_TIME_FRAME))
    end
end

-- ====== 5. 核心监控逻辑 ======
function monitorEverything()
    frameCounter = frameCounter + 1
    if frameCounter < checkInterval then return end
    frameCounter = 0

    local state = getState()
    local currentFrame = state.frameCount

    -- 加农剧情监测
    -- 监测加农是否生成，用于外部广播静音切换
    local plotState = read(ADDR_PLOT_STATE, snesMem, false)
    if plotState == 0x0B then
        if not HAS_GANON_SPAWNED then
            writeToFile(GANON_SPAWN_FILE, "1")
            HAS_GANON_SPAWNED = true
            log("检测到状态 0B：加农已出现")
        end
    else
        -- 状态重置逻辑：当离开加农房或剧情结束时，恢复拦截位
        if HAS_GANON_SPAWNED and plotState ~= 0x0B then
            HAS_GANON_SPAWNED = false
            writeToFile(GANON_SPAWN_FILE, "0")
            log("加农房状态已结束")
        end
    end

    -- 自动 A 键同步
    -- 模拟丢失的PSRAM跳转功能
    if HAS_LOADED and not HAS_PRESSED_A then
        local shouldPress = false
        if LOAD_TIME_FRAME < TARGET_FRAME then
            -- 准时/早到玩家：等到 TARGET_FRAME 准时切入
            if currentFrame >= TARGET_FRAME then shouldPress = true end
        else
            -- 迟到玩家：在进场后延迟 LATE_DELAY 帧强制切入
            if currentFrame >= (LOAD_TIME_FRAME + LATE_DELAY) then shouldPress = true end
        end

        if shouldPress then
            setInput({ a = true })
            HAS_PRESSED_A = true
            log(string.format("触发入场 A 键。当前帧: %d, 基准帧: %d", currentFrame, TARGET_FRAME))
        end
    end

    -- 章节信号与性别监测
    -- 自动识别当前播放的是第几周(1-4)以及角色性别
    local rawSignal = read(ADDR_SIGNAL, bsxMem, false)
    if rawSignal and rawSignal >= 0 and rawSignal <= 3 then
        local genderVal = read(ADDR_GENDER, snesMem, false)
        local genderSuffix = (genderVal == 1) and "g" or "b" -- g: Girl, b: Boy
        local currentCombined = (rawSignal + 1) .. genderSuffix

        if currentCombined ~= LAST_SIGNAL then
            writeToFile(SIGNAL_FILE, currentCombined) 
            LAST_SIGNAL = currentCombined
            log("当前章节状态同步为: " .. currentCombined)
        end
    end

    -- 结算触发检测
    -- 监测游戏是否进入最终结算
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

    -- 结算数据统计
    -- 抓取玩家战绩数据并序列化，供外部程序渲染使用
    local rawDeaths   = read(ADDR_DEATHS, sramMem, false) or 0
    local rawHeartVal = read(ADDR_HEART_VAL, sramMem, false) or 0
    local heartLoss   = rawHeartVal / 2 
    local totalRupees = emu.readWord(ADDR_RUPEE_LOW, sramMem, false) or 0
    local triforce    = read(ADDR_TRIFORCE, sramMem, false) or 0
    local ganonDefeat = read(ADDR_GANON_DEFEAT, sramMem, false) or 0

    -- 仅当数据发生变化时才写入文件
    if rawDeaths ~= LAST_DEATHS or heartLoss ~= LAST_HEARTS or 
       totalRupees ~= LAST_RUPEES or triforce ~= LAST_TRIFORCE or 
       ganonDefeat ~= LAST_GANON then
        
        local resultData = string.format("DEATH:%d|HEART_LOSS:%.1f|RUPEE:%d|TRIFORCE:%02X|GANON:%d", 
                                          rawDeaths, heartLoss, totalRupees, triforce, ganonDefeat)
        writeToFile(RESULT_FILE, resultData)
        LAST_DEATHS, LAST_HEARTS, LAST_RUPEES, LAST_TRIFORCE, LAST_GANON = 
            rawDeaths, heartLoss, totalRupees, triforce, ganonDefeat
        log("SRAM 战绩更新: " .. resultData)
    end
end

-- ====== 6. 系统初始化与重置 ======
local function resetSystem()
    -- 恢复所有拦截位至初始状态
    HAS_LOADED = false
    LOAD_TIME_FRAME = 0
    HAS_TRIGGERED_SETTLE = false
    HAS_PRESSED_A = false
    HAS_GANON_SPAWNED = false  
    LAST_SIGNAL = "FF"
    LAST_DEATHS, LAST_HEARTS, LAST_RUPEES, LAST_TRIFORCE, LAST_GANON = -1, -1, -1, -1, -1
    
    -- 初始化同步文件，防止旧数据干扰外部程序
    writeToFile(SIGNAL_FILE, "FF")
    writeToFile(LOAD_FILE, "0")
    writeToFile(TRIGGER_FILE, "IDLE") 
    writeToFile(GANON_SPAWN_FILE, "0")
    
    -- 注册硬件挂载回调
    emu.addMemoryCallback(onHardwareLoadWrite, emu.callbackType.write, ADDR_LOAD_DONE, ADDR_LOAD_DONE, emu.cpuType.snes, bsxMem)
    
    log("==========================================")
    log("  BS Zelda 同步脚本已重置：[核心监控已就绪]  ")
    log("==========================================")
end

-- 注册框架事件回调
emu.addEventCallback(monitorEverything, emu.eventType.frameEnd)
emu.addEventCallback(resetSystem, emu.eventType.reset)

-- 脚本启动
resetSystem()