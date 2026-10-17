#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
《环行旅舍》假业务服 —— TCP 10301。

协议(CSPKG_nf):
  帧   = 4字节大端头 P + payload;  P = (compress&7) | ((encrypt&7)<<4) | (len<<8)
  payload = XXTEA( protobuf CSPKG_nf{ field1=Head(CSHead), field2=Body } )
  CSHead  = 1=CmdId 2=SeqId 3=RetCode 4=StrCmdId 6=SecretKey
  XXTEA key = b"AA9zg1CZMnYLpA5H"   (长度字在末尾, 词小端)

用法: python bizserver.py [port]
"""
import hashlib
import random
import socket
import struct
import sys
import threading
import time

import bizproto as B
import paths
import playerdata

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 10301
# ★★ 2026-10-17: 运行期数据全搬到**用户数据目录**, 程序目录只留程序和只读配置。
#   落点规则见 paths.py: 会写的一律外置; 只读的留下。
LOG = paths.data_path(None, "logs", "bizserver.log")
# ★ bizdata.json 是从正式服抓回来的原始回包 ⇒ 算游戏资产, 进 resources(只读)。
BIZDATA = paths.data_path(None, "resources", "bizdata.json")

_bizcache = {"mtime": None, "data": {}}

# 玩家设置(服务端存储): KeyValuePair{Key, IntValue, StrValue}  持久化到文件
import json as _json
# ★ settings.json 会被游戏写 ⇒ state。
# ★★★ 2026-10-17: **按客户端(渠道+版本)分开** —— 它里面存的是"这个客户端这份档"的
#   设置(场景id / BGM / 舞台背景那类), 两个客户端共用一个文件就会互相覆盖
#   (用户实测报的「舞台背景没分开」就是这个)。落到 `state\<渠道><版本>\settings.json`。
#   ★ 这里是**初始值**(平面), 真正的路径由 `refresh_settings()` 在
#     每个请求开头 / 启动时重定 —— 和存档作用域同一时机(见 playerdata.refresh_save)。
SETTINGS = paths.data_path(None, "state", "settings.json")
# 默认设置: Key=2 场景id(1000=第一章), Key=3 BGM, Key=4 用默认BGM
DEFAULT_SETTINGS = {"2": 1000, "3": 10001, "4": 1}

_settings_scope = None


def refresh_settings():
    """按当前客户端重定 settings.json 的落点。返回 `(路径, 有没有变)`。

    ★ 认不出客户端 ⇒ 保持平面的 `state\\settings.json`(行为同改动前)。
    ★ 只在**作用域真的变了**的时候才切换, 顺便把平面那份**收编**进新作用域
      (第一次上这套时, 老设置不该丢)。
    """
    global SETTINGS, _settings_scope
    sc = paths.client_scope()
    if sc == _settings_scope:
        return SETTINGS, False
    _settings_scope = sc
    old = SETTINGS
    SETTINGS = paths.scoped_state_path("settings.json")
    if old != SETTINGS:
        # 平面的老设置收编过来(目标已在 ⇒ 不动)
        try:
            if os.path.exists(old) and not os.path.exists(SETTINGS):
                import shutil as _sh
                _sh.copyfile(old, SETTINGS)
                log("★ 把老的 settings.json 收编进 %s" % SETTINGS)
        except Exception as _e:
            log("!! 收编 settings.json 失败(忽略): %r" % (_e,))
    return SETTINGS, True


# ★ settings.json 是**运行期状态**（游戏里改设置就会写它），所以不进 git。
#   仓库里带的是种子 settings.default.json（只读 ⇒ 留在程序目录）；第一次运行读它，
#   之后就以 settings.json 为准。
SETTINGS_DEFAULT = paths.path("settings.default.json")


def load_settings():
    for p in (SETTINGS, SETTINGS_DEFAULT):
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                d = _json.load(f)
            if d:
                return d
        except Exception:
            continue
    return dict(DEFAULT_SETTINGS)


def save_settings(d):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            _json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("!! settings 保存失败: %s" % e)


def load_bizdata():
    """bizdata.json 结构: { "<reqCmd>": {"body_hex": "..", "ret": 0, "res_cmd": optional} }
    每次请求按 mtime 热重载。"""
    import json
    import os
    try:
        m = os.path.getmtime(BIZDATA)
    except OSError:
        return {}
    if _bizcache["mtime"] != m:
        try:
            with open(BIZDATA, "r", encoding="utf-8-sig") as f:
                _bizcache["data"] = json.load(f)
            _bizcache["mtime"] = m
        except Exception as e:
            log("!! bizdata.json 解析失败: %s" % e)
    return _bizcache["data"]

# 已知命令号
CMD_NAMES = {
    1001: "CSLoginReq", 1002: "CSLoginRes",
    1003: "CSCreateActorReq", 1004: "CSCreateActorRes",
    1005: "CSHeartReq", 1006: "CSHeartRes",
    1009: "CSKickOffNotify",
    1010: "CSGetRoleListReq", 1011: "CSGetRoleListRes",
    # 非等张共鸣 (Emotion) / 记忆分析 (dump.cs 真值)
    1371: "CSEmotionActAnalysisReq", 1372: "CSEmotionActAnalysisRes",
    1373: "CSEmotionActFixModuleReq", 1374: "CSEmotionActFixModuleRes",
    1381: "CSEmotionActInfoReq", 1382: "CSEmotionActInfoRes",
    1383: "CSEmotionActStateChangeNotify",
    1229: "CSReLoginReq", 1230: "CSReLoginRes",
    # 周常兑换 (dump.cs 真值)
    1099: "CSGetStagePlotReq", 1100: "CSGetStagePlotRes",
    1101: "CSStagePlotFinishReq", 1102: "CSStagePlotFinishRes",
    1106: "CSGetTaskExchangeReq", 1107: "CSGetTaskExchangeRes",
    1108: "CSTaskExchangeReq", 1109: "CSTaskExchangeRes",
    1194: "CSGetTaskExchangeNotify",
    1249: "CSStagePlotAwardReq", 1250: "CSStagePlotAwardRes",
    1289: "CSGetFourteenSignInfoReq", 1290: "CSGetFourteenSignInfoRes",
    1050: "CSUseItemReq", 1051: "CSUseItemRes", 1193: "CSUseItemNotify",
    1067: "CSDataNotify", 1361: "CSGameDataNotify",
    1097: "CSGetStageIntellectReq", 1098: "CSGetStageIntellectRes",
    1156: "CSGetPlayerCenterDataReq", 1157: "CSGetPlayerCenterDataRes",
    1182: "CSGetNoticeReq", 1183: "CSGetNoticeRes",
    19000: "CSResetPlayerDataReq(自建)", 19001: "CSResetPlayerDataRes(自建)",
    1158: "CSUpdatePlayerCenterDataReq", 1159: "CSUpdatePlayerCenterDataRes",
    1364: "CSGetHeadAndHeadFrameDataReq", 1365: "CSGetHeadAndHeadFrameDataRes",
    1366: "CSHeadFrameUnlockNotify",
    1291: "CSGetFourteenSignInfoNotify",
    1292: "CSSignFourteenSignReq", 1293: "CSSignFourteenSignRes",
    1202: "CSModuleUnlockNotify", 1203: "CSGetUnlockModuleReq", 1204: "CSGetUnlockModuleRes",
    1205: "CSIsModuleUnlockReq", 1206: "CSIsModuleUnlockRes",
    1022: "CSRolePotentialReq", 1023: "CSRolePotentialRes",
    1018: "CSRoleStrengthReq", 1019: "CSRoleStrengthRes",
    1026: "CSRoleLevelUpReq", 1027: "CSRoleLevelUpRes",
    1030: "CSRoleTalenUnlockReq", 1031: "CSRoleTalenUnlockRes",
    1211: "CSRoleDefaultTalenReq", 1212: "CSRoleDefaultTalenRes",
    1242: "CSRoleAbilityLevelUpReq", 1243: "CSRoleAbilityLevelUpRes",
    1244: "CSRoleStrengthenAwardReq", 1245: "CSRoleStrengthenAwardRes",
    1265: "CSRoleHandbookAwardReq", 1266: "CSRoleHandbookAwardRes",
    1046: "CSHeroBuyReq", 1047: "CSHeroBuyRes",
    1207: "CSGetLearnedFoodsReq", 1208: "CSGetLearnedFoodsRes",
    1166: "CSCookingReq", 1167: "CSCookingRes", 1209: "CSLearnFoodNotify",
    1152: "CSGiftToRoleReq", 1153: "CSGiftToRoleRes", 1214: "CSGiftToPlayerNotify",
    1180: "CSGetAllRoomDataReq", 1181: "CSGetAllRoomDataRes",
    1172: "CSAssignApartmentRoomReq", 1173: "CSAssignApartmentRoomRes",
    1176: "CSAskIntoRoomReq", 1177: "CSAskIntoRoomRes",
    1178: "CSGetFloorRoomDataReq", 1179: "CSGetFloorRoomDataRes",
    1195: "CSUnlockFloorNotify",
    1397: "CSSetRoleSkinReq", 1398: "CSSetRoleSkinRes",
    1213: "CSMultiRoleNotify", 1053: "CSRoleNotify",
}

_lock = threading.Lock()


def log(s):
    with _lock:
        print(s, flush=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(s + "\n")


# ======================================================================
#  单客户端锁 (免责声明, 设计见 docs\08 §8.37)
# ======================================================================
# 用户拍板的规则 (2026-10-15 修订, 见 docs\08 §8.53):
#   1. 服务端生成一个 8 位码, **只塞进 accId 做显示**, 不参与任何账号逻辑。
#   2. 一旦某个连接开口说话就"落锁" —— 任一时刻**只有一个连接持锁**。
#   3. 断开(FIN/RST, 或 120 秒空闲超时)立刻清锁清码 —— 下次登录重新发码。
#   4. **没有任何 bypass 开关**(不留 nolock 之类的后门, 那等于没有锁)。
#
# ★★ 2026-10-15 修订: "第二个客户端"改成**按账号身份**判, 不再一刀切拒绝。
#    起因: 客户端**退回登录页不会关掉旧 socket**, 重登时开一条新连接 ⇒ 被自己的
#    旧连接挡住 ⇒ 用户看到"网络问题"进不去游戏(日志实证, 见 §8.53)。
#    → 新连接的 1001 登录包身份 == 持锁者身份 ⇒ **踹掉旧连接**, 新连接接管;
#      身份不一致(真·另一个账号) ⇒ 照旧拒绝。
#    不变量没变: 任一时刻仍然只有一个连接持锁, 被顶掉的那条**立刻被关闭**。
#    身份 = sha1(UserId|Account|Device|Di|Pi), 只存哈希不存原值。
# ★ 兜底: 持锁者**静默** >= LOCK_TAKEOVER_IDLE 秒 ⇒ 判定僵尸, 允许新连接接管。
#    实测客户端心跳 ~5 秒一次(见 handle() 里能源结算那段), 30 秒 = 6 个心跳,
#    正常在玩的连接不可能静默这么久, 只有"旧会话已经是尸体"才会触发。
# ★ 为什么是"开口说话才落锁"而不是 accept 就落锁:
#     .bat 的端口探测、浏览器误连、任何只连不发的探测都不该把游戏锁在外面。
#     只有真正发业务命令的连接才算客户端。实测客户端只用**一条长连接**。
_LOCK = threading.Lock()
_LOCK_OWNER = None          # 持有锁的 socket 对象; None = 空闲
_LOCK_CODE = 0              # 8 位显示码; 0 = 没有码
_LOCK_IDENT = None          # 持锁者登录时留下的账号身份哈希; None = 没拿到
_LOCK_LAST = 0.0            # 持锁者最近一次说话的时刻(兜底用)
_LOCK_REFUSED = 0           # 统计: 被拒的连接数(只用于日志)

LOCK_TAKEOVER_IDLE = 30.0   # 秒

# ★ 登录包里代表"你是谁"的字段, 故意**不含** 2 Token(会轮换)
#   和 10 Version / 13 Revision(会变) —— 身份必须跨会话稳定。
_LOGIN_IDENT_FIELDS = (1, 21, 18, 8, 7)     # UserId Account Device Di Pi


def login_info(body):
    """从 1001 登录包里取 (身份哈希, 是否重登)。取不到身份返回 None。

    ★ 只用来判断"是不是同一个人重登", 不存原值、不落盘。
    """
    try:
        f = {}
        for k, w, v in B.pb_fields(body or b""):
            f.setdefault(k, v)
        relogin = bool(f.get(4, 0))
        parts = []
        for k in _LOGIN_IDENT_FIELDS:
            v = f.get(k)
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            parts.append(v if isinstance(v, str) else "")
        s = "\x1f".join(parts)
        if not s.strip("\x1f"):
            return None, relogin       # 这个客户端一个身份字段都没给, 不猜
        return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12], relogin
    except Exception:
        return None, False


def lock_acquire(conn, ident=None):
    """连接开口说话时调用。

    返回 (是否放行, 当前 8 位码, 说明, 需要踹掉的旧连接或 None)。
    """
    global _LOCK_OWNER, _LOCK_CODE, _LOCK_IDENT, _LOCK_LAST
    with _LOCK:
        now = time.time()
        if _LOCK_OWNER is None:
            _LOCK_OWNER, _LOCK_IDENT, _LOCK_LAST = conn, ident, now
            _LOCK_CODE = random.randint(10000000, 99999999)
            return True, _LOCK_CODE, "首次落锁", None
        if _LOCK_OWNER is conn:
            _LOCK_LAST = now
            if ident:
                _LOCK_IDENT = ident
            return True, _LOCK_CODE, "", None
        # ---- 已经有别的连接持锁 ----
        idle = now - _LOCK_LAST
        if ident and _LOCK_IDENT and ident == _LOCK_IDENT:
            old = _LOCK_OWNER
            _LOCK_OWNER, _LOCK_IDENT, _LOCK_LAST = conn, ident, now
            return (True, _LOCK_CODE,
                    "同账号重登 -> 顶掉旧连接(旧连接静默 %.0fs)" % idle, old)
        if idle >= LOCK_TAKEOVER_IDLE:
            old = _LOCK_OWNER
            _LOCK_OWNER, _LOCK_IDENT, _LOCK_LAST = conn, ident, now
            return (True, _LOCK_CODE,
                    "旧连接静默 %.0fs(>= %.0fs) -> 判定僵尸, 新连接接管"
                    % (idle, LOCK_TAKEOVER_IDLE), old)
        return (False, _LOCK_CODE,
                "已有客户端持锁(静默 %.0fs); 本条连接身份%s"
                % (idle, "不一致" if (ident and _LOCK_IDENT) else "未知"), None)


def lock_release(conn):
    """连接关闭时调用。只有持锁者能解锁 —— 免得别人的断线把锁清了。"""
    global _LOCK_OWNER, _LOCK_CODE
    with _LOCK:
        if _LOCK_OWNER is conn:
            code, _LOCK_OWNER, _LOCK_CODE = _LOCK_CODE, None, 0
            return code
    return None


def lock_code():
    with _LOCK:
        return _LOCK_CODE


def lock_refuse_count():
    global _LOCK_REFUSED
    with _LOCK:
        _LOCK_REFUSED += 1
        return _LOCK_REFUSED


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def push_frame(msg_cmd, body, ref_cmd=0):
    """服务端**主动推送**(notify)的帧。

    ★ SeqId 必须是 0。客户端 C# 侧 (Broadcast) 的回调表按 (cmdId, seqId) 做键,
      Lua 的 NetworkMgr.regist(cmd, cb) 走 BusinessServer.AddBroadcastEventHandler
      (cmdId, cb) —— seqId 参数默认 0。所以只有 SeqId=0 的包能找到注册的回调。
      之前推送时回填了请求的 seq => 全部匹配失败 => "领完不刷新"(实测探针证实
      RefreshDataList / RefreshTaskList 一次都没被调用)。
    """
    return B.build_reply(ref_cmd, 0, res_cmd=msg_cmd, body=body)


# ★★ 任务页签刷新 (2026-10-15)。心跳约 5 秒一次 => 默认约 15 秒推一次。设 0 关闭。
#
# 为什么需要这个:
#   view_task_ui 的页签可见性是在 **TASK_UPDATE 事件**里算的:
#       -- view_task_ui proto#1(注册在 TASK_UPDATE 上), 末尾:
#       SetActive(tabs[1].btn, logic_task_main.CheckUnlock())   -- 主线
#       SetActive(tabs[3].btn, ...)                             -- 周常
#       SetActive(tabs[4].btn, ...)                             -- 发展
#   而这个注册是在 **打开任务页那一刻**(view_task_ui.Init)才做的。
#   实测: 打开任务页时客户端**一个请求都不发**(bizserver.log 里那段时间只有
#   1005/1006 心跳), 登录阶段那几次 TASK_UPDATE 早就过去了 =>
#   页签永远停在 prefab 默认态(**只剩主线一个**)。
#
# 【2026-10-15 结论: 没用, 已关】
#   后来实测发现页签根本不看 TASK_UPDATE —— view_task_ui.InitData 里那次
#   SetActive(tabs[N], CheckUnlock()) 才是决定性的, 而 CheckUnlock() 查的是
#   "第 1000 章第 7 关通没通"(见 _make_baseline.py 的 stages 注释)。
#   真正的病因是「还原数据」把 stages 清空了, 跟推送无关。
#   留着开关不删(以后排查别的 UI 刷新问题可能还用得上), 默认关闭。设 0 = 关。
TASK_REFRESH_EVERY = 0
_task_tick = {"n": 0}


def task_refresh_due():
    if TASK_REFRESH_EVERY <= 0:
        return False
    _task_tick["n"] += 1
    return (_task_tick["n"] % TASK_REFRESH_EVERY) == 0


def _pb_ints(body):
    """把请求体里**顶层 varint 字段**读成 {字段号: 值}。

    养成那几条命令的请求都是"纯标量"(1018{ID} / 1244{RoleId,StrengthenLevel} /
    1030{RoleId,Rank} ...), 用这个就够; 嵌 message / repeated 的还是走
    每个处理器自己的解析(见 1026 的 UseList)。
    """
    out = {}
    for f, w, v in B.pb_fields(body or b""):
        if w == 0 and isinstance(v, int):
            out[f] = v
    return out


def handle(conn, addr):
    log("\n===== CONNECT %s =====" % (addr,))
    conn.settimeout(120)
    # ★ TCP keepalive: 客户端进程被强杀时不会有 FIN, 光靠 recv 永远等不到。
    #   开了之后 OS 15 秒探一次、3 秒一次重试 —— 死连接约 20~30 秒内必定报错,
    #   从而走 finally 把锁放掉。否则锁会一直挂到 120 秒空闲超时。
    try:
        conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 15000, 3000))
    except Exception:
        pass
    # ★ 荣誉(logic_challenge)引导标记 —— 每次连接只推第一条,
    #   第二条由 1062(TaskType=5) 的回包之后补上。推导见
    #   `playerdata.build_honor_notify` 的注释。
    _honor_pushed = False
    try:
        while True:
            hdr = recv_exact(conn, 4)
            if hdr is None:
                break
            P = struct.unpack(">I", hdr)[0]
            compress = P & 7
            encrypt = (P >> 4) & 7
            ln = P >> 8
            payload = recv_exact(conn, ln)
            if payload is None:
                log("!! 读 payload 中断")
                break
            try:
                pl = B.decode_payload(payload, compress, encrypt)
                head, body = B.parse_cspkg(pl)
            except Exception as e:
                log("!! 解包失败: %s  P=%08x comp=%d enc=%d len=%d head=%s"
                    % (e, P, compress, encrypt, ln, payload[:24].hex(" ")))
                continue
            cmd = head.get(1, 0)
            seq = head.get(2, 0)
            ret = head.get(3, 0)
            # --- 单客户端锁: 见 §8.37 / §8.53 ---
            #   ★ 登录包先算出账号身份, 好让"同一个人重登"能顶掉自己的旧连接。
            _ident, _relogin = login_info(body) if cmd == 1001 else (None, False)
            _ok, _code, _why, _old = lock_acquire(conn, _ident)
            if not _ok:
                log("!! 拒绝第二个客户端连接 %s —— %s" % (addr, _why))
                log("   单客户端锁: 同时只允许一个客户端。若前一个已经关掉游戏, "
                    "它会在被判定断开(最长 120 秒空闲)后自动解锁。")
                log("   累计被拒: %d 次" % lock_refuse_count())
                break
            if _old is not None:
                try:
                    _old_peer = _old.getpeername()
                except Exception:
                    _old_peer = "?"
                log("★ 锁接管: %s —— 踹掉旧连接 %s" % (_why, _old_peer))
                try:
                    _old.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    _old.close()
                except Exception:
                    pass
            name = CMD_NAMES.get(cmd, "?")
            log("<- cmd=%d(%s) seq=%d ret=%d comp=%d enc=%d len=%d bodyLen=%d"
                % (cmd, name, seq, ret, compress, encrypt, ln, len(body or b"")))
            # ★ 1001 登录包里带 token / openID / 设备指纹, 不原样落盘。
            #   (其他命令的 body 全是游戏数据, 留着排查用。)
            if body:
                if cmd == 1001:
                    log("   body: <%d 字节, 登录包已省略>" % len(body))
                else:
                    log("   body: %s" % body.hex(" "))

            # --- 玩家数据(存档) ---
            # ★ 客户端的「还原数据」在**登录页**点, 那时它还没连上本服,
            #   所以走的是**文件标记**: 在这里(拉存档之前)兑现, 本次回包
            #   直接就是新档 —— 和连接状态无关, 也不用重连/清缓存。
            # ★ 两个按钮都走文件标记(公告页「重置数据」):
            #   重置存档 -> do_reset.flag        (整份存档回基准档)
            #   重置角色 -> do_reset_roles.flag  (只回角色部分)
            # --- 客户端渠道 (2026-10-17) ---
            # ★ HTTP 层刮到 subPlatType 写进 state/client_channel.json, 这里兑现。
            #   必须赶在 load() 之前 —— 否则本次请求读的还是上一个渠道的表。
            try:
                _ch = playerdata.consume_client_channel()
                if _ch:
                    import cfgdata
                    if cfgdata.switch_client(_ch):
                        log("★ 客户端渠道 -> %s ; 配置表目录 %s (渠道 %s)"
                            % (_ch, cfgdata.CFG_DIR, cfgdata.CLIENT_DIR))
                        # ★★ 2026-10-17: playerdata 那些"配置表算出来的"模块级缓存
                        #    也必须清 —— `cfgdata.switch_client` 只管它自己那份。
                        #    服务端启动时渠道可能还没确定，那时建出来的空缓存
                        #    不清掉就会永久生效(月卡礼包不折算就是这么来的)。
                        try:
                            playerdata.reset_cfg_caches()
                        except Exception as _e3:
                            log("!! 清配置缓存失败(忽略): %r" % (_e3,))
                        # ★★ 全新数据目录时，bizserver 启动那会儿渠道还不知道、一张表都
                        #    读不到 ⇒ 启动时的索引生成必然失败，之后也不会再试。
                        #    渠道一确定、表就能读了，这里补一次（幂等：文件已在就跳过）。
                        try:
                            _w, _n = cfgdata.ensure_roles_index(log=log)
                            if _n:
                                log("角色名索引: %s (%d 个角色)"
                                    % ("已生成" if _w else "沿用", _n))
                        except Exception as _e2:
                            log("角色名索引生成失败(忽略): %r" % (_e2,))
                    else:
                        log("★ 客户端渠道 -> %s (未变)" % _ch)
            except Exception as _e:
                log("!! 切渠道失败(忽略): %r" % (_e,))

            # --- 存档作用域: 按客户端(渠道+版本)分 (2026-10-17 用户要求) ---
            # ★ 和上面一样必须赶在 load() 之前 —— 否则本次请求读写的还是上一个
            #   客户端的存档。换客户端测试时两个人会串档(国服领的道具 Steam 没有)。
            # ★ 认不出客户端就保持平面 saves\player.json, 行为和改动前一致。
            try:
                _sp, _sp_new = playerdata.refresh_save()
                if _sp_new:
                    log("★ 存档作用域 -> %s" % _sp)
            except Exception as _e:
                log("!! 重定存档路径失败(忽略): %r" % (_e,))

            # --- 设置作用域: settings.json 也按客户端分 (2026-10-17 用户报告) ---
            # ★ 用户原话: 「有些东西没有分开存储, 比如用户个人信息和舞台背景,
            #   所有可以改的东西都应该放存档里分开」。
            #   settings.json 里存的就是那类"这个客户端这份档"的设置。
            try:
                _st, _st_new = refresh_settings()
                if _st_new:
                    log("★ 设置作用域 -> %s" % _st)
            except Exception as _e:
                log("!! 重定设置路径失败(忽略): %r" % (_e,))

            _reset_done = False
            if playerdata.consume_reset_flag():
                _r_ok, _r_why = playerdata.reset_to_baseline()
                log("★ 重置存档(公告页按钮兑现): %s -> %s"
                    % ("成功" if _r_ok else "失败", _r_why))
                _reset_done = _reset_done or _r_ok
            if playerdata.consume_reset_roles_flag():
                _r_ok, _r_why = playerdata.reset_roles_to_baseline()
                log("★ 重置角色数据(公告页按钮兑现): %s -> %s"
                    % ("成功" if _r_ok else "失败", _r_why))
                _reset_done = _reset_done or _r_ok
            # --- 公告页「角色工具」: 删除 / 获得 (2026-10-16 用户要求) ---
            # ★ 这**不是测试工具** —— 是给"不想抽卡的玩家"用的正常功能,
            #   所以获得要走完整流程(角色进 roles、道具进 items、该推的都推)。
            _tmode, _tids = playerdata.consume_role_tool_flag()
            if _tids:
                _pd0 = playerdata.load()
                _newroles, _items, _notes = [], [], []
                if _tmode == "drop":
                    _chg = playerdata.drop_roles(_pd0, _tids)
                    log("★ 角色工具[删除] %s -> %s" % (_tids, _chg or "本来就没有"))
                    # ★★ 不立刻推 1009(用户要求改条件式): 公告 WebView 还开着的时候
                    #   把人踢回登录页体验很怪。改成登记"待重登", 等客户端发 19101
                    #   (从公告栏退回主页)时再推 —— 见下面 cmd 19101 那一段。
                    playerdata.set_relogin_pending(_pd0)
                else:
                    for _rid in _tids:
                        _d, _nr, _its = playerdata.give_role_for_player(_pd0, _rid)
                        _notes.append("%s: %s" % (_rid, _d))
                        if _nr is not None:
                            _newroles.append(_nr)
                        for _i, _n in _its:
                            _items.append((_i, _n))
                    log("★ 角色工具[获得] %s" % ("; ".join(_notes),))
                playerdata.save(_pd0)
                # ★★ 顺序: 先角色弹窗, 再物品到账(用户要求"先弹获得界面, 再发补充协议")
                if _newroles:
                    conn.sendall(push_frame(
                        1213, playerdata.build_multi_role_notify(_pd0, _newroles)))
                    for _rid in _newroles:
                        conn.sendall(push_frame(19100, str(int(_rid)).encode("ascii")))
                    log("   -> 推 1213 + 19100 %s" % (_newroles,))
                if _items:
                    # ★★ 发补充协议也要弹「获得物品」窗(用户 2026-10-16 要求)。
                    #   用游戏自带的 `UICfg.COMMON_GET_ITEMS_UI` —— 抽卡重复奖励
                    #   弹的就是它(view_single_recruit_ui 的 jumpType==2 分支)。
                    #   body = "id:count,id:count", 由客户端补丁 inject_newrole.py 解。
                    _body = playerdata.items_push_body(_items)
                    conn.sendall(push_frame(19102, _body.encode("ascii")))
                    conn.sendall(push_frame(1052, playerdata.build_item_counts(
                        _pd0, [i for i, _n in _items])))
                    log("   -> 推 19102(获得物品窗) + 1052 %s" % (_items,))

            # --- 公告页「删除角色」按钮: 删掉标记文件里列的角色 ---
            # ★ 用户要求: 测试「没有该舍友时舍友合同给不给角色本身」要能反复重建前提,
            #   每次叫我不方便 ⇒ 做成公告页上的按钮(见 fakeserver 的 reset_html)。
            _drop = playerdata.consume_drop_role_flag()
            if _drop:
                _pd0 = playerdata.load()
                _chg = playerdata.drop_roles(_pd0, _drop)
                if _chg:
                    playerdata.save(_pd0)
                    _reset_done = True          # 复用"重登"那套: 让客户端重新拉角色列表
                    log("★ 删除角色(公告页按钮兑现): %s -> %s" % (_drop, _chg))
                else:
                    log("★ 删除角色 %s: 这些角色本来就不在存档里" % (_drop,))
                    _reset_done = True          # 也让客户端刷一次
            if _reset_done:
                # ★★ 重置/删角色之后必须让客户端**重登** —— 否则满屏都是旧缓存
                #    (角色/背包/关卡全是旧的)。1009 配合客户端补丁 inject_logout.py
                #    就是"回登录页"。
                log("   -> 推 1009 让客户端回登录页重新拉数据")
                try:
                    conn.sendall(push_frame(1009, b""))
                except Exception as _e:
                    log("   !! 推 1009 失败: %r" % (_e,))
            # --- 公告页「退出登录」: 推 1009 CSKickOffNotify ---
            # ★ 原版客户端收到 1009 只打一句 print(proto#10), 什么也不做 ⇒ 光靠推
            #   1009 登不出去(用户实测: 只会断线然后自动重连回来)。
            #   所以配了客户端补丁 `_re\inject_logout.py`: 它把 1009 的处理器换成
            #   **设置->用户->退出登录** 那套正牌流程
            #   (sdk_mgr.SwitchAccount + ChangeIsSwitchAccountState + scene_mgr.ReLoginGame)。
            # ★ 这里**不再主动断连** —— 客户端 ReLoginGame 自己会收尾; 我们再断一次
            #   只会让它在切场景时多弹一个「与服务器连接异常」。
            #   排查用日志: `_re\client_logout.log`(补丁有没有加载 / 有没有收到 1009)。
            if playerdata.consume_logout_flag():
                log("★ 收到「退出登录」-> 推 cmd=1009 (客户端补丁负责回登录页)")
                try:
                    conn.sendall(push_frame(1009, b""))
                except Exception as _e:
                    log("   !! 推 1009 失败: %r" % (_e,))
            pd = playerdata.load()
            # ★★ 重置流程要**吞掉**"新角色"弹窗(用户实测的闪屏 bug, 2026-10-16):
            #   基准档 items 里带着 26 张舍友合同, 重置后的第一次 load() 会把它们
            #   一次性兑现成角色 → 25+ 次 _note_new_role → 推 1213+19100 二十五次。
            #   而同一个请求里我们还要推 1009 让客户端回登录页 —— 界面在转场里被
            #   反复销毁, 每次销毁都回调 closeCallBackEvent 去显示下一个 ⇒ **一直闪**;
            #   最后一个还会留下"名字已经换成新角色、立绘还是上一个"的残影
            #   (文字同步赋值, 立绘异步从 AB 加载)。
            #   ⇒ 重置是"把存档拉回基准档", 本来就不该给玩家弹「获得角色」。
            if _reset_done:
                _swallowed = playerdata.consume_pending_role_push()
                if _swallowed:
                    log("★ 重置流程: 丢弃 %d 个「新角色」弹窗(基准档合同兑现, 不是玩家获得)"
                        % len(_swallowed))
            # ★ 老存档里残留的舍友合同被兜底兑掉之后, 必须告诉客户端"这格没了",
            #   否则背包界面上那张合同会一直挂着(服务端删了, 客户端不知道)。
            # ★★ 顺序有讲究(用户要求「先弹获得界面, 再发补充协议」):
            #   19100(角色弹窗) 必须排在 1052(物品到账) **前面**。
            _nr = playerdata.consume_pending_role_push()
            if _nr:
                # 1213: 先把角色数据交给客户端(否则它的角色列表里还没有这个人)
                conn.sendall(push_frame(1213, playerdata.build_multi_role_notify(pd, _nr)))
                # ★★ 19100: 自建命令 —— 让客户端**立刻弹「获得角色」**。
                #   只靠 1213 是不行的: 客户端 logic_role_data.UpdateRoleData 末尾
                #   有个**场景闸门**, 只在战斗场景/免费招募页才记新角色, 主城/商店里
                #   直接吞掉。见 inject_newrole.py 的说明。
                for _rid in _nr:
                    conn.sendall(push_frame(19100, str(int(_rid)).encode("ascii")))
                log("   新获得角色 -> 推 1213 + 19100 %s" % (_nr,))
            _cf = playerdata.consume_pending_item_push()
            if _cf:
                conn.sendall(push_frame(1052, playerdata.build_item_counts(pd, _cf)))
                log("   清理残留舍友合同 -> 推 1052 %s" % (_cf,))
            # ★ 2026-10-17: load() 的兜底把月卡礼包折算成天数时也要推 1246 ——
            #   否则客户端 logic_sign.GetSignInfo() 还是登录时的旧值，
            #   月卡横幅/弹窗一直显示「未订购」（天数其实已经加上了）。
            if playerdata.consume_pending_sign_push():
                conn.sendall(push_frame(1246, playerdata.build_sign_info(pd)))
                log("   月卡天数(兜底折算)已变 -> 补推 1246")
            # ★ 能源自然恢复。客户端**不会自己往上加**(实测 view_common_power_ui
            #   只读 GetIntellect()、只挂 INTELLECT_CHANGE 事件, 没有本地计时器),
            #   所以必须服务端推 1067 CSDataNotify。客户端每 ~5 秒一个心跳,
            #   所以每个请求顺手结算一次就已经够及时了, 不需要另开后台线程。
            try:
                _e_changed, _e_dirty, _e_cd = playerdata.settle_energy(pd)
                # ★ dirty 和 changed 是两回事: 起表那次只改了 intellect_ts,
                #   不落盘的话下次 load() 又是 0 => 永远不涨(2026-10-15 实测)。
                if _e_dirty:
                    playerdata.save(pd)
                if _e_changed:
                    for _nb in playerdata.build_energy_notify_bodies(pd):
                        conn.sendall(push_frame(1067, _nb))
                    log("   能源自然恢复 -> %d (下一点 %ds)"
                        % (pd["actor"]["intellect"], _e_cd))
            except Exception as _e:
                log("   !! 能源结算出错(不影响本次请求): %r" % (_e,))
            if cmd == 1001:
                # ★ 日常任务 50001「登录1次」
                playerdata.bump_daily(pd, "login")
                playerdata.save(pd)
                conn.sendall(B.build_reply(
                    1001, seq, body=playerdata.build_login(pd, lock_code())))
                log("-> cmd=1002 (login, lv=%d, 显示 uid=%d, ReLogin=%s, 身份=%s)"
                    % (pd["actor"]["level"], lock_code(),
                       _relogin, _ident or "未提供"))
                continue
            if cmd == 1010:
                conn.sendall(B.build_reply(1010, seq, body=playerdata.build_role_list(pd)))
                log("-> cmd=1011 (roles=%d)" % len(pd["roles"]))
                continue
            if cmd == 1048:
                conn.sendall(B.build_reply(1048, seq, body=playerdata.build_store(pd)))
                continue
            # --- 商店 (Shop) ---
            # 数据源: dataconfig_shop_base(大类) / shop_label(标签) / shop_goods(商品)
            if cmd == 1117:      # CSGetShopListReq -> CSGetShopListRes
                conn.sendall(B.build_reply(1117, seq,
                                           body=playerdata.build_shop_list(pd)))
                import cfgdata as _c
                log("-> cmd=1118 (商店大类 x%d: %s)"
                    % (len(playerdata._shop_types()), playerdata._shop_types()))
                continue
            if cmd == 1119:      # CSGetShopReq{1:Type} -> CSGetShopRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                st = int(m.get(1, 0))
                conn.sendall(B.build_reply(1119, seq,
                                           body=playerdata.build_shop(pd, st)))
                log("   商店大类 %d 明细" % st)
                continue
            if cmd == 1124:      # CSBuyCheckReq{1:RechargePoint} -> CSBuyCheckRes + 推送 1126
                # 人民币商品: 客户端发完 1124 就去调 sdk_mgr:SdkOrder(真支付)。
                # 离线环境 sdk_none 的 SdkOrder 是空实现(RET0) => 卡住 => "点不动"。
                # 做法: 照常回 1125, 然后**立刻推送 1126 CSBuySuccessNotify** 把商品发下去。
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                rp = int(m.get(1, 0))
                conn.sendall(B.build_reply(1124, seq, body=B.pb_b(1, "offline")))
                _mc_before = pd.get("month_card_days")
                gain = playerdata.buy_check(pd, rp)
                conn.sendall(push_frame(1126,
                                        playerdata.build_buy_success(pd, rp, gain)))
                # ★ 再推一条 CSItemNotify(1052): 客户端靠它更新**本地道具缓存**,
                #   否则顶部货币区不刷新(实测: 要等抽卡触发另一次刷新才跳数字)。
                if gain:
                    conn.sendall(push_frame(1052,
                                            playerdata.build_item_notify(pd, gain)))
                # ★ 月卡天数变了就推 CSLoginSignInfoNotify(1246):
                #   logic_sign 注册了它 -> 刷新 GetSignInfo() 缓存 + 派发 SIGN_UPDATE。
                #   商店今日声讯那个月卡 banner 的「剩余 N 天」是客户端用
                #   GetSignInfo().MonthCardDays 自己拼的, 不推这一条买完不会变。
                if pd.get("month_card_days") != _mc_before:
                    conn.sendall(push_frame(1246, playerdata.build_sign_info(pd)))
                    log("   月卡天数 %s -> %s, 已推 1246 刷新签到信息"
                        % (_mc_before, pd.get("month_card_days")))
                log("   人民币购买 RechargePoint=%d -> 直接发放 %s (已推 1126 + 1052)"
                    % (rp, gain))
                continue
            if cmd == 1121:      # CSShopBuyReq{1:Type 2:LabelID 3:Id 4:Count} -> CSShopBuyRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                st, lid = int(m.get(1, 0)), int(m.get(2, 0))
                gid, cnt = int(m.get(3, 0)), int(m.get(4, 1) or 1)
                _mc_before = pd.get("month_card_days")
                gain, ok, paid = playerdata.shop_buy(pd, st, lid, gid, cnt)
                if ok:
                    # ★ 日常任务 50009「在沙多集市购买任意商品1次」。
                    #   shop_buy 内部已经 save 过, 所以 bump 之后必须再存一次。
                    daily = playerdata._task_rows().get(playerdata.TASK_TYPE_DAILY, [])
                    before = {r["id"]: playerdata.task_flag(pd, r["id"]) for r in daily}
                    playerdata.bump_daily(pd, "shop_buy")
                    playerdata.save(pd)
                    changed = [tid for tid, f in before.items()
                               if playerdata.task_flag(pd, tid) != f]
                    if changed:
                        conn.sendall(push_frame(1105,
                                                playerdata.build_task_notify(pd, changed)))
                conn.sendall(B.build_reply(1121, seq, body=playerdata.build_shop_buy(
                    pd, st, lid, gid, cnt, gain)))
                if ok and (gain or paid):
                    # ★★ 2026-10-15 修「购买稀因商品没消耗稀因」:
                    #   1052 的 1 号字段是**绝对数量**, 客户端收到才会刷新本地缓存
                    #   (顶部货币区就读它)。以前这里只推 gain(买到的), 没推 paid
                    #   (花掉的) => 数字不动。
                    #   ★ 注意**不能**用 `build_item_notify(pd, [(i,0) ...])` 来补 ——
                    #     那个函数里 `if n == 0: continue` 会把整条跳过(静默无效)。
                    #     付掉的道具要用 build_item_counts(只发绝对数量, 无增量)。
                    conn.sendall(push_frame(
                        1052,
                        playerdata.build_item_notify(pd, gain)
                        + playerdata.build_item_counts(pd, paid)))
                if pd.get("month_card_days") != _mc_before:
                    conn.sendall(push_frame(1246, playerdata.build_sign_info(pd)))
                    log("   月卡天数 %s -> %s, 已推 1246"
                        % (_mc_before, pd.get("month_card_days")))
                log("   商店购买 type=%d label=%d goods=%d x%d -> %s"
                    % (st, lid, gid, cnt, gain if ok else "失败(余额不足/未找到商品)"))
                continue
            if cmd == 1339:      # CSShopSupportAwardInfoReq -> CSShopSupportAwardInfoRes
                # 大厅里被反复轮询(实测 14 次), 给全 0 让它安定下来
                conn.sendall(B.build_reply(1339, seq,
                                           body=playerdata.build_shop_support_award(pd)))
                continue
            if cmd == 1357:      # CSGetGameDataReq -> CSGetGameDataRes{1:Data=CSGameData}
                # ★ 整份存档快照。客户端购买/签到之后会调 game_data.ReqGameData()
                #   拉一次, 拿到 ItemList 就**立刻刷新货币显示**。
                #   实测: 不拉的话要等抽卡(抽卡响应里带 GameData)才会跳数字。
                # ★★ 2026-10-16 更正: 这里以前写的是「拿到 MoneyList/ItemList」——
                #   反汇编证明 `MoneyList`(我们发的 gold/diamond/copper 三条)**完全无用**:
                #   `logic_player_data.SetUpPlayerData` 只认 5 个 `ERDT_` 开头的 key。
                #   刷新货币靠的是 **ItemList**(item 1/2/3)。
                conn.sendall(B.build_reply(1357, seq,
                                           body=B.pb_b(1, playerdata.build_gamedata(pd))))
                log("-> cmd=1358 (整份存档快照)")
                continue
            if cmd == 1086:      # CSLoginSignInfoReq -> CSLoginSignInfoRes{1:DaySignData}
                # 月卡的购买弹窗要靠这份数据(logic_sign.GetSignInfo()), 空响应会让它崩。
                conn.sendall(B.build_reply(1086, seq, body=playerdata.build_sign_info(pd)))
                log("-> cmd=1087 (签到/月卡信息)")
                continue
            if cmd == 1247:      # CSLoginSignReq -> CSLoginSignRes(领取今天的签到奖励)
                gain = playerdata.sign_in(pd)
                # ★ 回复的命令号由 playerdata.SIGN_REPLY_RES_CMD 决定(None = 正常 1248)。
                #   设成 19001 时客户端会把这一包丢掉, 用来二分卡死到底出在哪一步。
                conn.sendall(B.build_reply(
                    1247, seq, body=playerdata.build_sign_res(pd, gain),
                    res_cmd=playerdata.SIGN_REPLY_RES_CMD))
                _all = ((gain.get("gain") or []) + (gain.get("mc") or [])
                        if isinstance(gain, dict) else (gain or []))
                if _all and playerdata.SIGN_PUSH_ITEM_NOTIFY:
                    if playerdata.SIGN_PUSH_FIELDS == "abs":
                        # ★ 只发字段1(背包绝对数量) —— 见 playerdata 里开关 4 的注释
                        conn.sendall(push_frame(1052, playerdata.build_item_counts(
                            pd, [i for i, _n in _all])))
                    else:
                        conn.sendall(push_frame(1052,
                                                playerdata.build_item_notify(pd, _all)))
                log("-> cmd=1248 (签到领取 %s)" % (gain,))
                continue
            # --- 社交 (2026-10-14): 好友/分享/助战 全部"直接给" ---
            # 客户端在 1280/1281(好友列表)、1296/1297(好友申请)、
            # 1284/1285(推荐好友) 上都会走通用空响应, 界面显示"没有好友"。
            if cmd == 1345:      # CSGetShareAwardStatusReq -> {1:LeftAwardTimes}
                conn.sendall(B.build_reply(1345, seq,
                                           body=playerdata.build_share_award_status(pd)))
                log("-> cmd=1346 (分享领奖剩余次数 %s)"
                    % pd.get("share_award_left", playerdata.SHARE_AWARD_TIMES))
                continue
            if cmd == 1305:      # CSRecruitShareAwardReq{1:ShareType} -> Res{1:ItemList}
                gain = playerdata.claim_share_award(pd)
                playerdata.save(pd)
                rb = b""
                for i, n in gain:
                    rb += B.pb_b(1, B.pb_v(1, int(i)) + B.pb_v(2, int(n)))
                conn.sendall(B.build_reply(1305, seq, body=rb))
                if gain:
                    conn.sendall(push_frame(1052,
                                            playerdata.build_item_notify(pd, gain)))
                log("   分享抽卡领奖 -> %s" % (gain,))
                continue
            if cmd == 1315:      # CSGetSupportRewardReq -> Res{1:RewardList}
                gain = playerdata.claim_support_reward(pd)
                playerdata.save(pd)
                rb = b""
                for i, n in gain:
                    rb += B.pb_b(1, B.pb_v(1, int(i)) + B.pb_v(2, int(n)))
                conn.sendall(B.build_reply(1315, seq, body=rb))
                if gain:
                    conn.sendall(push_frame(1052,
                                            playerdata.build_item_notify(pd, gain)))
                log("   支援奖励 -> %s" % (gain,))
                continue
            if cmd == 1309:      # CSGetSupportRoleReq -> Res(支援角色列表)
                conn.sendall(B.build_reply(1309, seq,
                                           body=playerdata.build_support_role(pd)))
                log("-> cmd=1310 (支援角色, 用玩家自己的角色顶替好友)")
                continue
            if cmd == 1311:      # CSGetStageSupportReq{1:GroupIdx}
                conn.sendall(B.build_reply(1311, seq,
                                           body=playerdata.build_stage_support(pd)))
                log("-> cmd=1312 (关卡助战列表)")
                continue

            # --- 邮件 (2026-10-15) ---
            if cmd == 1080:      # CSMailGetListReq{1:Page 2:MaxCount} -> CSMailGetListRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                conn.sendall(B.build_reply(1080, seq, body=playerdata.build_mail_list(
                    pd, m.get(1, 1), m.get(2, 200))))
                log("   邮件列表 page=%s max=%s (存档里未删 %d 封)"
                    % (m.get(1, 1), m.get(2, 200),
                       len([1 for s in playerdata._mail_store(pd).values()
                            if int(s.get("state") or 0) not in (3, 4)])))
                continue
            if cmd == 1082:      # CSMailReceiveReq{1:GotType 2:MailIdList} -> Res{1:ItemList}
                m, ids = {}, []
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 0:
                        m[1] = v2
                    elif f2 == 2 and w2 == 2:          # Uint64Id{1:ID}
                        for f3, w3, v3 in B.pb_fields(v2):
                            if f3 == 1:
                                ids.append(v3)
                gain = playerdata.claim_mails(pd, ids, m.get(1, 0))
                rb = b""
                for i, n in gain:
                    rb += B.pb_b(1, B.pb_v(1, int(i)) + B.pb_v(2, int(n)))
                conn.sendall(B.build_reply(1082, seq, body=rb))
                if gain:
                    conn.sendall(push_frame(1052,
                                            playerdata.build_item_notify(pd, gain)))
                log("   邮件领奖 GotType=%s 邮件=%s -> %s" % (m.get(1, 0), ids, gain))
                continue
            if cmd == 1084:      # CSMailDeleteReq{1:DelType 2:MailId 3:Location}
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                n = playerdata.delete_mails(pd, m.get(2, 0), m.get(3, 1))
                conn.sendall(B.build_reply(1084, seq, body=b""))
                if n == 0 and not int(m.get(2, 0) or 0):
                    log("   邮件删除 DelType=%s MailId=0 Location=%s -> "
                        "已禁用(批量删邮件), 一封没动" % (m.get(1, 0), m.get(3, 1)))
                else:
                    log("   邮件删除 DelType=%s MailId=%s Location=%s -> 删了 %d 封"
                        % (m.get(1, 0), m.get(2, 0), m.get(3, 1), n))
                continue

            # --- 任务 / 成就 (2026-10-14) ---
            # 客户端事实: 1111 的类型表只认 KTAB#19 = {1,2,6,8,12};
            #             成就(5)与荣誉(4)由 logic_challenge 自己发 1062 拉。
            # ★ 模块解锁 (2026-10-15 补): 以前没实现, 客户端只能靠补丁强开
            #   defaultUnLockAll 兜底。README/dump.cs: EMUT_* + 1034。
            if cmd == 1203:      # CSGetUnlockModuleReq(空) -> CSGetUnlockModuleRes
                conn.sendall(B.build_reply(1203, seq,
                                           body=playerdata.build_module_unlock_res(pd)))
                log("-> cmd=1204 (模块解锁 x%d)" % len(playerdata.MODULE_UNLOCK_IDS))
                continue
            if cmd == 1205:      # CSIsModuleUnlockReq{1:ModuleID} -> CSIsModuleUnlockRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                conn.sendall(B.build_reply(1205, seq,
                                           body=playerdata.build_module_is_unlock_res(
                                               pd, int(m.get(1, 0)))))
                continue

            if cmd == 1110:      # CSGetTaskTypeListReq -> 类型表
                conn.sendall(B.build_reply(1110, seq,
                                           body=playerdata.build_task_type_list(pd)))
                log("-> cmd=1111 (任务类型表 x%d)" % len(playerdata.TASK_TYPE_LIST))
                continue
            if cmd == 1062:      # CSGetTaskListReq{1:TaskType 2:Param} -> CSGetTaskListRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                tt = int(m.get(1, 0))
                pa = int(m.get(2, 0))
                conn.sendall(B.build_reply(1062, seq,
                                           body=playerdata.build_task_list(pd, tt, pa)))
                log("   任务列表 type=%s param=%s -> %d 条"
                    % (tt, pa, len(playerdata._task_rows().get(tt, []))))
                # ★★ 荣誉引导(2/2): 客户端拿到成就(TaskType=5)的列表、把 uv2 建起来之后,
                #   必须**再收一条**带荣誉条目(TaskType=4, AwardFlag=2)的 CSTaskNotify,
                #   才会自己去发 CSGetTaskListReq(4) —— 那一次的回包
                #   (`GetTaskListRes` → `SetTaskList`) 才是唯一填充荣誉列表的地方。
                #   推导见 `playerdata.build_honor_notify` 的注释。
                if tt == playerdata.TASK_TYPE_ACHIEVE:
                    conn.sendall(push_frame(1105, playerdata.build_honor_notify(pd)))
                    log("   荣誉引导(2/2) -> 推 1105, 带 %d 条荣誉(等客户端来要荣誉列表)"
                        % len(playerdata.honor_ids()))
                continue
            if cmd == 1064:      # CSGetTaskAwardReq{1:AwardType 2:Param} -> CSGetTaskAwardRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                at, pa = int(m.get(1, 0)), int(m.get(2, 0))
                gain, changed, scope = playerdata.claim_task_award(pd, at, pa)
                playerdata.save(pd)
                conn.sendall(B.build_reply(1064, seq, body=playerdata.build_task_award_res(
                    pd, at, pa, gain, changed, scope)))
                # ★ 推送 CSTaskNotify(1105): 客户端只在它上面注册了 RefreshTaskList,
                #   不发的话日常列表领完不刷新(AwardFlag 不更新)。
                if changed:
                    conn.sendall(push_frame(1105,
                                            playerdata.build_task_notify(pd, changed)))
                if gain:
                    conn.sendall(push_frame(1052,
                                            playerdata.build_item_notify(pd, gain)))
                log("   领任务奖励 AwardType=%s%s Param=%s -> %s (改了 %s)"
                    % (at, playerdata.AWARD_TYPE_LABEL.get(at, ""), pa, gain, changed))
                continue
            # --- 使用物品 (2026-10-15) ---
            if cmd == 1050:      # CSUseItemReq{1:UseItemList(repeated UseItem)} -> CSUseItemRes
                # UseItem{1:ID 2:Expired 3:Count 4:ArgList(repeated Uint32Id)}
                # ★ ArgList 只在「自选礼包」里非空, 内容是 package_item_list 的位置下标。
                req = []
                for f1, w1, v1 in B.pb_fields(body or b""):
                    if f1 != 1 or w1 != 2:
                        continue
                    it = {"args": []}
                    for f2, w2, v2 in B.pb_fields(v1):
                        if w2 == 0 and f2 in (1, 2, 3):
                            it[{1: "id", 2: "expired", 3: "count"}[f2]] = v2
                        elif f2 == 4 and w2 == 2:
                            for f3, w3, v3 in B.pb_fields(v2):
                                if f3 == 1 and w3 == 0:
                                    it["args"].append(v3)
                    req.append(it)
                _intel_before = int((pd.get("actor") or {}).get("intellect") or 0)
                rb, gain, spent, refused = playerdata.use_items(pd, req)
                # ★ 自选包"什么都没选"-> 用返回码让客户端弹提示(用户要求)。
                #   69 = 「该物品相关参数错误」(lang_zh.json)。此时**一点不消耗**。
                _no_sel = any("自选包未选择" in str(r[1]) for r in refused)
                # ★★ 2026-10-16 修「通行证道具(私域通讯 172/173/174)点了没反应」:
                #   以前**只有**"自选包没选"才回非 0, 其它拒绝一律 ret_code=0(成功)
                #   ⇒ 服务端明明拒了、道具也没消耗, 客户端却以为成功 ⇒ 界面毫无反应。
                #   现在: **全都被拒、且一个都没消耗** 就回 69(「该物品相关参数错误」),
                #   客户端会照码弹提示。
                #   ★ 想要「该功能已禁用（本地服务端）」那句原文得走客户端补丁 ——
                #     客户端是按 ret_code 查文案的, 服务端给不了自定义字符串。
                _all_refused = bool(refused) and not spent
                conn.sendall(B.build_reply(
                    1050, seq, body=rb,
                    ret_code=(playerdata.RET_ITEM_PARAM_ERROR
                              if (_no_sel or _all_refused) else 0)))
                # ★ 用了能源道具就必须推 1067 —— `UseItemContent.GameData` 是
                #   CSGameData, **里面根本没有 Intellect 字段**, 客户端无从得知新值。
                #   只回 1051 的话表现就是「提示成功, 但主页能源没变」(用户实测)。
                if int((pd.get("actor") or {}).get("intellect") or 0) != _intel_before:
                    for _nb in playerdata.build_energy_notify_bodies(pd):
                        conn.sendall(push_frame(1067, _nb))
                if gain or spent:
                    # 消耗也要推 —— build_item_notify 里 1 号字段发的是**绝对数量**
                    conn.sendall(push_frame(1052, playerdata.build_item_notify(
                        pd, list(gain) + [(i, -n) for i, n in spent])))
                else:
                    # ★ 什么都没发生 = 客户端很可能记着**错误的数量**
                    #   (例如那个 2^64-1 的幽灵礼包: 服务端早就删了, 客户端还以为有一堆,
                    #    于是"使用"按钮还能点, 点了又没反应)。
                    #   把真实数量推过去让它自愈 —— 数量归零, 那个礼包就从背包消失。
                    ids = [int(r["id"]) for r in req if r.get("id")]
                    if ids:
                        conn.sendall(push_frame(1052,
                                                playerdata.build_item_counts(pd, ids)))
                log("   使用物品 %s -> 得到 %s 消耗 %s%s"
                    % ([(r.get("id"), r.get("count"), r.get("args")) for r in req],
                       gain, spent, ("  ★拒绝 %s" % refused) if refused else ""))
                continue
            # --- 14日签到「安抚」(2026-10-15) ---
            if cmd == 1289:      # CSGetFourteenSignInfoReq(空) -> Res{1:Info(FourteenSignInfo)}
                conn.sendall(B.build_reply(1289, seq,
                                           body=playerdata.build_fourteen_sign_info(pd)))
                log("-> cmd=1290 (14日签到: 空列表 + Status=Close, 首页入口隐藏)")
                continue
            if cmd == 1292:      # CSSignFourteenSignReq{1:Day} -> Res{1:ItemList}
                conn.sendall(B.build_reply(1292, seq,
                                           body=playerdata.build_fourteen_sign_award_res(pd)))
                log("   14日签到被点了(入口本该是隐藏的) -> 空 ItemList")
                continue
            # --- 剧情关 / 剧情回看 (2026-10-15) ---
            if cmd == 1099:      # CSGetStagePlotReq(空) -> CSGetStagePlotRes{1:PlotList}
                conn.sendall(B.build_reply(1099, seq,
                                           body=playerdata.build_plot_list(pd)))
                log("-> cmd=1100 (已看剧情 x%d)"
                    % len(set(playerdata._plot_store(pd))))
                continue
            if cmd == 1101:      # CSStagePlotFinishReq{1:PlotId} -> Res{1:PlotId}
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                conn.sendall(B.build_reply(1101, seq,
                                           body=playerdata.mark_plot_finished(
                                               pd, m.get(1, 0))))
                log("   剧情看完 PlotId=%s" % m.get(1, 0))
                continue
            if cmd == 1249:      # CSStagePlotAwardReq{1:Stage(CSStageBase)} -> Res
                sb = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 2:
                        for f3, w3, v3 in B.pb_fields(v2):
                            if w3 == 0:
                                sb[f3] = v3
                rb, gain = playerdata.plot_stage_award(
                    pd, sb.get(1, 0), sb.get(2, 0), sb.get(3, 0), sb.get(4, 0))
                conn.sendall(B.build_reply(1249, seq, body=rb))
                if gain:
                    conn.sendall(push_frame(1052, playerdata.build_item_notify(pd, gain)))
                log("   剧情关领奖 stage=%s/%s/%s/%s -> %s"
                    % (sb.get(1, 0), sb.get(2, 0), sb.get(3, 0), sb.get(4, 0), gain))
                continue

            # --- 周常兑换 (2026-10-15) ---
            # 登录必备请求里就调 GetTaskExchange() 发 1106; 以前默认分支回空 body,
            # ExchangeList 为空 -> 周常界面的兑换入口**不显示**。
            if cmd == 1106:      # CSGetTaskExchangeReq(空) -> CSGetTaskExchangeRes
                conn.sendall(B.build_reply(1106, seq,
                                           body=playerdata.build_task_exchange_list(pd)))
                cnt = playerdata.exchange_counts(pd)
                log("-> cmd=1107 (周常兑换 x%d, 已换 %s)"
                    % (len(playerdata._exchange_rows()), cnt or "无"))
                continue
            if cmd == 1108:      # CSTaskExchangeReq{1:ExchangeId 2:Count} -> Res
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                eid, cn = int(m.get(1, 0)), int(m.get(2, 1))
                rb, gain, spend, err = playerdata.task_exchange(pd, eid, cn)
                if err:
                    # 非 0 retCode 客户端会弹对应的错误文案(1026 代币不足 / 1021 已换满)
                    conn.sendall(B.build_reply(1108, seq, body=b"", ret_code=err))
                    log("   周常兑换 id=%s x%s -> 失败 retCode=%d" % (eid, cn, err))
                    continue
                conn.sendall(B.build_reply(1108, seq, body=rb))
                # ★★ 2026-10-17 修「兑换完顶栏三个货币都不刷新」:
                #   以前写 `[(i, 0) for ...]` 是**静默无效**的 ——
                #   `build_item_notify` 里 `if n == 0: continue` 会把花掉的那一项
                #   整条跳过 ⇒ 客户端根本不知道钱少了。付掉的道具要用 build_item_counts。
                conn.sendall(push_frame(
                    1052,
                    playerdata.build_item_notify(pd, gain)
                    + playerdata.build_item_counts(pd, [i for i, _n in spend])))
                # 1194: 客户端收到后会自己重新发 1106 拉列表(限购次数刷新)
                conn.sendall(push_frame(1194, playerdata.build_task_exchange_list(pd)))
                log("   周常兑换 id=%s x%s -> 得 %s, 花 %s" % (eid, cn, gain, spend))
                continue

            if cmd == 1154:      # CSExchangeMoneyReq{1:ItemID 2:Count 3:TargetID}
                # ★ 2026-10-17 新实现（以前没实现 ⇒ 回空包 ⇒ 客户端"数量没变化"）:
                #   比率在客户端本地表 `dataconfig_currency_exchange`，
                #   1(稀因)->3(因) 是 1:1；3(因)->75 是 160:1。
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                rb, gain, spend, err = playerdata.currency_exchange(
                    pd, m.get(1, 0), m.get(2, 0), m.get(3, 0))
                if err:
                    conn.sendall(B.build_reply(1154, seq, body=b"", ret_code=err))
                    log("   货币兑换 %s -> %s x%s 失败 retCode=%d"
                        % (m.get(1), m.get(3), m.get(2), err))
                    continue
                conn.sendall(B.build_reply(1154, seq, body=rb))
                # ★★ 2026-10-17 修「兑换完顶栏三个货币都不刷新」:
                #   以前写 `[(i, 0) for ...]` 是**静默无效**的 ——
                #   `build_item_notify` 里 `if n == 0: continue` 会把花掉的那一项
                #   整条跳过 ⇒ 客户端根本不知道稀因少了。见 build_item_notify 的注释。
                conn.sendall(push_frame(
                    1052,
                    playerdata.build_item_notify(pd, gain)
                    + playerdata.build_item_counts(pd, [i for i, _n in spend])))
                log("   货币兑换 %s -> %s x%s -> 得 %s, 花 %s"
                    % (m.get(1), m.get(3), m.get(2), gain, spend))
                continue

            if cmd == 1068:
                conn.sendall(B.build_reply(1068, seq, body=playerdata.build_stage_list(pd)))
                import cfgdata as _c
                log("-> cmd=1069 (章节开放 x%d, 已通关 %d)"
                    % (len(_c.load("dataconfig_stage_chapter")), len(pd["stages"])))
                continue
            if cmd == 1074:      # CSStageStartReq{1:Stage(CSStageBase)} -> CSStageStartRes
                # ★ 进关卡扣能源。数值全部来自 dataconfig_stage_minor ——
                #   按 (type, chapter, main_id, minor_id) 查, 所以**主线和副本
                #   (type=2) 走的是同一个查表**, 没有硬编码。(实测进场都是 1 点;
                #   差别在扫荡: 副本 sweep_intellect 10~30, 主线 0~24。扫荡命令
                #   1091~1096 目前整个没接。)
                sb = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 2:
                        for f3, w3, v3 in B.pb_fields(v2):
                            if w3 == 0:
                                sb[f3] = v3
                _cost = playerdata.stage_intellect(
                    sb.get(1, 0), sb.get(2, 0), sb.get(3, 0), sb.get(4, 0))
                ok, pay, left = playerdata.consume_stage_intellect(
                    pd, sb.get(1, 0), sb.get(2, 0), sb.get(3, 0), sb.get(4, 0))
                if not ok:
                    # ★ 能源不足 -> 不放行。ret=116 = STAGE_NOT_ENOUGH_INTELLECT,
                    #   对应本地化表里的「116 能源不足」。
                    conn.sendall(B.build_reply(1074, seq, body=b"",
                                               ret_code=playerdata.RET_STAGE_NOT_ENOUGH_INTELLECT))
                    log("-> cmd=1075 ★能源不足(有 %d, 需要 %d), 拒绝 %s/%s/%s/%s"
                        % (pd["actor"]["intellect"], _cost, sb.get(1, 0), sb.get(2, 0),
                           sb.get(3, 0), sb.get(4, 0)))
                    continue
                conn.sendall(B.build_reply(1074, seq,
                                           body=playerdata.build_stage_start(pd)))
                if pay:
                    for _nb in playerdata.build_energy_notify_bodies(pd):
                        conn.sendall(push_frame(1067, _nb))
                log("-> cmd=1075 (开战 %s/%s/%s/%s, 耗能源 %d -> %d)"
                    % (sb.get(1, 0), sb.get(2, 0), sb.get(3, 0), sb.get(4, 0), pay, left))
                continue
            # --- 还原数据 (登录页「还原数据」按钮, 2026-10-15) ---
            # 自定义命令 19000 -> 19001。ECV 枚举只到 1410, 这个号绝不撞车。
            # ★ 成功之后**主动断开连接**: 客户端会重连并重新登录, 这样所有界面
            #   都拿到新数据, 不用指望客户端自己去刷新一堆缓存。
            if cmd == playerdata.CMD_RESET:      # 19000(手动/脚本用; 客户端走文件标记)
                ok_r, why = playerdata.reset_to_baseline()
                playerdata.consume_reset_flag()  # 已经立刻兑现了, 别再兑现一次
                log("-> cmd=19001 (还原数据: %s) %s"
                    % ("成功" if ok_r else "失败", why))
                conn.sendall(B.build_reply(19000, seq, body=b"",
                                           res_cmd=19001,
                                           ret_code=0 if ok_r else 1))
                # ★ **不再主动断连**: 登录回包本来就是按新档现拼的, 客户端
                #   点"进入游戏"就能看到新数据, 不用重连也不用清缓存。
                continue
            # --- 自建命令 19101: 「玩家从公告栏退回主页了」---
            # ★★ 用途(用户 2026-10-16 要求的**条件式**重登):
            #   用角色工具改过角色之后**不立刻**踢回登录页(公告还开着的时候踢体验很怪),
            #   而是登记"待重登"; 等客户端把公告关掉、退回主页时发这个 19101,
            #   这时才推 1009 让客户端重新登录, 拿到新的角色列表。
            #   客户端补丁: `inject_noticeback.py`(挂在 view_normal_notice_ui.ClickBack)。
            if cmd == 19101:
                _need = playerdata.consume_relogin_pending(pd)
                # ★ 先回包(客户端那边挂着这个 seq, 不回会让它记一个超时),
                #   再推 1009 把人送回登录页。
                conn.sendall(B.build_reply(19101, seq, body=b""))
                log("★ 收到 19101(退出公告) -> 待重登=%s" % _need)
                if _need:
                    try:
                        conn.sendall(push_frame(1009, b""))
                    except Exception as _e:
                        log("   !! 推 1009 失败: %r" % (_e,))
                continue
            # --- 启动公告 / 滚动公告 (2026-10-15) ---
            # 客户端进主页时发 1182 CSGetNoticeReq{1:Type 2:Num 3:Offset},
            # 只认 Notice.Type==Actual_Time(2) 的条目并丢给 ShowUrgentNotice。
            # 文案在 _re\notice.json, 改完直接生效(服务端每次请求重读)。
            if cmd == 1182:      # CSGetNoticeReq -> CSGetNoticeRes
                rt = 0
                for f, w, v in B.pb_fields(body or b""):
                    if f == 1 and w == 0:
                        rt = v
                rb = playerdata.build_notice_res(pd, rt)
                conn.sendall(B.build_reply(1183, seq, body=rb, res_cmd=1183))
                _cfg = playerdata.load_notice()
                log("-> cmd=1183 (公告: reqType=%s enabled=%s 长度=%d 字, 滚 %s 遍 x %ss)"
                    % (rt, _cfg.get("enabled"), len(playerdata.notice_text(_cfg)),
                       _cfg.get("roll_times"), _cfg.get("interval")))
                continue
            # --- 个人中心 / 头像头像框 (2026-10-15) ---
            if cmd == 1156:      # CSGetPlayerCenterDataReq(空) -> Res{1:PlayerCenter}
                conn.sendall(B.build_reply(
                    1156, seq,
                    body=playerdata.build_player_center(pd, lock_code())))
                log("-> cmd=1157 (个人中心: name=%s sign=%r)"
                    % (pd["actor"].get("name"), pd["actor"].get("sign")))
                continue
            if cmd == 1158:      # CSUpdatePlayerCenterDataReq{1:DataType 2:PlayerCenter}
                # ★ CSUpdatePlayerCenterDataRes 是**空包**, 失败只能靠 ret_code 表达。
                m, pcf = {}, {}
                _NUM = {3: "head_id", 5: "create_tm", 6: "role_num",
                        7: "head_frame_id", 8: "achievement_num"}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                    elif f2 == 2 and w2 == 2:
                        for f3, w3, v3 in B.pb_fields(v2):
                            if w3 == 2 and f3 in (2, 4):
                                pcf[{2: "name", 4: "sign"}[f3]] = \
                                    v3.decode("utf-8", "replace")
                            elif w3 == 0 and f3 in _NUM:
                                pcf[_NUM[f3]] = v3
                            elif f3 == 9 and w3 == 2:      # Honors(repeated Uint32Id)
                                for f4, w4, v4 in B.pb_fields(v3):
                                    if w4 == 0:
                                        pcf.setdefault("honors", []).append(v4)
                _before = dict(pcf)
                rc, msg = playerdata.update_player_center(pd, m.get(1, 0), pcf)
                conn.sendall(B.build_reply(1158, seq, body=b"", ret_code=rc))
                # 改名会扣掉一件道具 —— 推真实数量让客户端背包当场刷新
                if rc == 0 and int(m.get(1, 0)) == playerdata.UPDATE_TYPE_NAME:
                    conn.sendall(push_frame(1052, playerdata.build_item_counts(
                        pd, [playerdata.RENAME_ITEM_ID, playerdata.RENAME_ITEM_ID_ALT])))
                log("   个人中心更新 DataType=%s rc=%s %s -> %s"
                    % (m.get(1, 0), rc, msg, pd["actor"].get("name")))
                continue
            if cmd == 1364:      # CSGetHeadAndHeadFrameDataReq -> Res{1:HeadIconList 2:HeadFrameList}
                conn.sendall(B.build_reply(1364, seq,
                                           body=playerdata.build_head_and_frame(pd)))
                log("-> cmd=1365 (头像/头像框列表)")
                continue
            if cmd == 1097:      # CSGetStageIntellectReq{1:Type 2:Chapter}
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                conn.sendall(B.build_reply(1097, seq,
                                           body=playerdata.build_stage_intellect_list(
                                               m.get(1, 0), m.get(2, 0))))
                log("-> cmd=1098 (关卡能源消耗 type=%s chapter=%s)"
                    % (m.get(1, 0), m.get(2, 0)))
                continue
            # --- 活动时间窗 (所有活动/限时玩法的时间判断都靠它) ---
            # 不返回的话客户端把该活动的 OpenTime/CloseTime 都设成 now-86400 => 一律"已结束"
            if cmd == 1347:      # CSGetActivityTimeInfoReq -> CSGetActivityTimeInfoRes
                # ★ 构造逻辑在 playerdata (有自检): 排期表 + 章节表引用但排期表里没有的 time_id。
                _rb, _n, _extra = playerdata.build_activity_time_info()
                conn.sendall(B.build_reply(1347, seq, body=_rb))
                log("-> cmd=1348 (活动时间窗 x%d, 全部 Open%s)"
                    % (_n, "; 补发章节引用的 %s" % (_extra,) if _extra else ""))
                continue
            if cmd == 1401:      # CSLimitedTimeInfoReq -> CSLimitedTimeInfoRes
                conn.sendall(B.build_reply(1401, seq,
                                           body=playerdata.build_limited_time_info()))
                log("-> cmd=1402 (限时活动时间窗; 回合活动 TimeId=%s 已置于首位)"
                    % (playerdata.turn_activity_time_id(),))
                continue
            # --- 非等张共鸣 (Emotion) / 记忆分析 ---
            # ★ 客户端这条线**不用 turn_activity 那套 UI**, 走 logic/event_emotion/,
            #   以前我们一条都没回 ⇒ 页面打不开。见 playerdata 顶上的注释。
            if cmd == 1381:      # CSEmotionActInfoReq -> CSEmotionActInfoRes
                conn.sendall(B.build_reply(1381, seq,
                                           body=playerdata.build_emotion_info(pd)))
                log("-> cmd=1382 (Emotion: TimeId=%d, 已分析 %d 个, FixModule=%s)"
                    % (playerdata.EMOTION_TIME_ID,
                       len(playerdata._emotion_analysed(pd)),
                       bool(pd.get("emotion_fix_module"))))
                continue
            if cmd == 1371:      # CSEmotionActAnalysisReq{1:Id} -> ...Res{1:ItemList}
                _id = _pb_ints(body).get(1, 0)
                _gain, _ok, _why = playerdata.emotion_analyse(pd, _id)
                conn.sendall(B.build_reply(
                    1371, seq, body=playerdata.build_emotion_analysis_res(_gain)))
                log("-> cmd=1372 (Emotion 分析 id=%s %s%s)"
                    % (_id, "成功" if _ok else "拒绝: " + _why,
                       "" if not _gain else " 发 " + str(_gain)))
                if _gain:
                    playerdata.save(pd)
                    # ★ 响应体里只有 ItemList, 没有 GameData ⇒ 必须自己推背包
                    #   (和剧情领奖 1249 那边一个写法)
                    conn.sendall(push_frame(1052,
                                            playerdata.build_item_notify(pd, _gain)))
                continue
            if cmd == 1373:      # CSEmotionActFixModuleReq -> ...Res{1:StageList}
                conn.sendall(B.build_reply(1373, seq,
                                           body=playerdata.emotion_fix_module(pd)))
                playerdata.save(pd)
                log("-> cmd=1374 (Emotion 修复模块 -> 已标记完成)")
                continue
                continue
            # 注意: 谜题剪辑(1251/1252)与定向演绎(1407/1408)暂未实现。
            #       给 1408 塞 TimeId 会让客户端以为开放, 随后去读空的 StageList 直接蓝屏,
            #       必须先有完整的关卡数据才能开, 在此之前保持"未开放"(不崩)。
            #   ★ 2026-10-15 补充: 上面这条理由**已经过时** —— 现在 build_stage_list
            #     是一口气下发**全部**关卡的, StageList 不再是空的。真要开这两个,
            #     重新验一次即可(参考 §8.50 的回合活动开法)。

            # --- 编队 (HeroGroup) ---
            if cmd == 1054:      # CSGetHeroGroupReq -> CSGetHeroGroupRes
                conn.sendall(B.build_reply(1054, seq,
                                           body=playerdata.build_hero_group(pd)))
                log("-> cmd=1055 (hero groups=%d)" % len(pd["groups"]))
                continue
            if cmd == 1060:      # CSModifyHeroGroupReq -> 增删队员并落盘
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                gi, slot, mt = m.get(1), m.get(2), m.get(3)
                hid, tid = m.get(4), m.get(5)
                # 客户端 GroupIdx 是 0-based, 存档键是 1-based
                g = pd["groups"].get(str(int(gi) + 1)) if gi is not None else None
                if g is not None and slot is not None:
                    mem = g.setdefault("members", [])
                    if mt == 0:                  # 移除
                        if 0 <= slot < len(mem):
                            mem.pop(slot)
                    else:                        # 放置/替换
                        while len(mem) <= slot:
                            mem.append([0, 0])
                        mem[slot] = [int(hid or 0), int(tid or 0)]
                    playerdata.save(pd)
                    log("   编队 %s 修改: type=%s slot=%s hero=%s -> %s"
                        % (gi, mt, slot, hid, mem))
                conn.sendall(B.build_reply(1060, seq, body=b""))
                continue
            if cmd == 1056:      # CSRenameHeroGroupReq -> 保存编队名
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    m[f2] = v2
                g = pd["groups"].get(str(int(m.get(1, 0)) + 1))
                if g is not None and 2 in m:
                    g["name"] = m[2]
                    playerdata.save(pd)
                    log("   编队 %s 改名 -> %s" % (m.get(1), m[2]))
                conn.sendall(B.build_reply(1056, seq, body=b""))
                continue
            if cmd == 1058:      # CSDeleteHeroGroupReq -> 清空该编队
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    m[f2] = v2
                g = pd["groups"].get(str(int(m.get(1, 0)) + 1))
                if g is not None:
                    g["members"] = []
                    playerdata.save(pd)
                    log("   编队 %s 已清空" % m.get(1))
                conn.sendall(B.build_reply(1058, seq, body=b""))
                continue
            if cmd == 1078:      # CSShortCutHeroGroupReq{1:GroupIdx 3:RoleList} (快捷编队)
                m = {}
                members = []
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 0:
                        m[1] = v2
                    elif f2 == 3 and w2 == 2:      # RoleTalent{1:RoleID, 2:TalentId}
                        rt = {}
                        for f3, w3, v3 in B.pb_fields(v2):
                            rt[f3] = v3
                        members.append([int(rt.get(1, 0)), int(rt.get(2, 0))])
                g = pd["groups"].get(str(int(m.get(1, 0)) + 1))
                if g is not None:
                    g["members"] = members
                    playerdata.save(pd)
                    log("   快捷编队 %s -> %s" % (m.get(1), members))
                conn.sendall(B.build_reply(1078, seq, body=b""))
                continue
            # --- 抽卡 (招募) ---
            if cmd == 1168:      # CSRecruitInfoReq -> CSRecruitInfoRes(卡池列表)
                conn.sendall(B.build_reply(1168, seq,
                                           body=playerdata.build_recruit_info(pd)))
                import cfgdata as _c
                log("-> cmd=1169 (卡池第 %d/%d 批, 本批 %d 个 / 共 %d 个)"
                    % (playerdata.recruit_page(pd) + 1, playerdata.recruit_page_count(),
                       len(playerdata.build_recruit_info(pd)), len(_c.recruit_pools())))
                continue
            if cmd == 1185:      # CSPublicRecruitInfoReq
                # ★ 「换一批卡池」按钮走这里。
                #   CSPublicRecruitInfoReq 是**空消息**, 客户端正常发的 body 是空串;
                #   我们的按钮会带一个非空标记 body => 只有带标记时才翻页,
                #   不会干扰游戏自己发的 1185。
                if body:
                    n = playerdata.recruit_page_next(pd)
                    conn.sendall(B.build_reply(1185, seq, res_cmd=1186, body=b""))
                    # 推一条新的 CSRecruitInfoRes(1169) 让客户端刷新卡池列表
                    conn.sendall(push_frame(1169,
                                            playerdata.build_recruit_info(pd)))
                    log("   换一批卡池 -> 第 %d/%d 批"
                        % (n + 1, playerdata.recruit_page_count()))
                else:
                    conn.sendall(B.build_reply(1185, seq, res_cmd=1186, body=b""))
                continue
            if cmd == 1046:      # CSHeroBuyReq{1:Time 2:PoolID} -> CSHeroBuyRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                times, pid = m.get(1, 1), m.get(2, 0)
                draws = playerdata.draw(pd, pid, times)
                conn.sendall(B.build_reply(1046, seq, body=playerdata.build_hero_buy(
                    pd, pid, draws)))
                log("-> cmd=1047 (抽卡 pool=%s x%s -> %s)" % (pid, times, draws))
                continue
            # ★ 因序链(潜能)升级 2026-10-15
            #   CSRolePotentialReq{1:ID 2:isCommon} -> CSRolePotentialRes
            #       {1:ID 2:PotentialLevel 3:GameData}
            #   ★ 补充协议(专属 3001~3065 / 通用 68)是 use_way=0 CanNotUse,
            #     **只能被这条命令消耗** —— 客户端不给它们「使用」按钮。
            #     以前完全没实现 => 有材料也升不了, 因序链整个卡死。
            if cmd == 1022:
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                rid, common = int(m.get(1, 0)), int(m.get(2, 0) or 0)
                lv, ret, spent = playerdata.role_potential_up(pd, rid, common)
                if ret:
                    # 非 0 会被客户端弹成「材料不足」
                    conn.sendall(B.build_reply(1022, seq, body=b"", ret_code=ret))
                    log("   因序链 role=%s isCommon=%s -> 失败(材料不足/已满) 当前 %s"
                        % (rid, common, lv))
                else:
                    conn.sendall(B.build_reply(
                        1022, seq,
                        body=playerdata.build_role_potential_res(pd, rid, lv)))
                    log("-> cmd=1023 (因序链 role=%s %s -> Lv%s, 花 %s)"
                        % (rid, "通用" if common else "专属", lv, spent))
                    if spent:
                        # ★ 同 1018: 付掉的道具要用 build_item_counts。
                        #   (原来写 `build_item_notify(pd, [(i,0) ...])` 是无效的 ——
                        #    那个函数对 n==0 的条目直接 continue, 什么都不会推。)
                        conn.sendall(push_frame(1052, playerdata.build_item_counts(
                            pd, [i for i, _n in spent])))
                continue
            # ★ 烹饪 / 百馐餐区 (P1 2026-10-15)
            #   1207 登录时就发; 不回 1208 的话客户端"已学会菜谱"恒空 =>
            #   菜单是空的, 选不了菜 => 突破要的食物永远产出不了。
            if cmd == 1207:      # CSGetLearnedFoodsReq(空) -> CSGetLearnedFoodsRes
                conn.sendall(B.build_reply(
                    1207, seq, body=playerdata.build_learned_foods_res(pd)))
                log("-> cmd=1208 (已学菜谱 x%d)" % len(playerdata.learned_foods()))
                continue
            if cmd == 1166:      # CSCookingReq{1:FoodID 2:RoleID 3:CookTimes} -> CSCookingRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                fid, rid, tn = int(m.get(1, 0)), int(m.get(2, 0)), int(m.get(3, 1) or 1)
                body1167, gain, spent, err = playerdata.cook(pd, fid, rid, tn)
                if err:
                    conn.sendall(B.build_reply(1166, seq, body=b"", ret_code=err))
                    log("   烹饪 food=%s role=%s x%s -> 失败(食材不足)" % (fid, rid, tn))
                else:
                    conn.sendall(B.build_reply(1166, seq, body=body1167))
                    log("-> cmd=1167 (烹饪 food=%s x%s -> 得 %s, 花 %s)"
                        % (fid, tn, gain, spent))
                    # ★★ 2026-10-15 修「做菜消耗了食材但界面不刷新, 食材显示还在」:
                    #   原来写 `build_item_notify(pd, gain + [(i,0) for ...])` ——
                    #   那个函数对 n==0 的条目**直接 continue**, 所以**被扣掉的食材
                    #   一个字节都没推**。1167 的响应里又没有 GameData(装的是
                    #   SpecialCook/SpecialItem/FoodID/FoodNums), 于是厨房界面的
                    #   食材数量永远停在旧值: 用光了还显示有, 但点下去服务端说不够。
                    #   ⇒ 付掉的食材必须用 build_item_counts(只发绝对数量)。
                    if gain or spent:
                        conn.sendall(push_frame(
                            1052,
                            playerdata.build_item_notify(pd, gain)
                            + playerdata.build_item_counts(
                                pd, [i for i, _n in spent])))
                continue
            # ★ 角色认可度 / 送礼 (P2 2026-10-15)
            #   CSGiftToRoleReq{1:RoleID 2:GiftID} -> CSGiftToRoleRes{1:GiftLevel 2:GameData}
            #   ★ 礼物列表是客户端从**背包**里按 itembase type5/sub1 过滤出来的,
            #     所以真正要做的只有两件: 这条命令 + 把礼物道具发进背包。
            if cmd == 1152:
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                rid, gid = int(m.get(1, 0)), int(m.get(2, 0))
                lv, delta, err = playerdata.gift_to_role(pd, rid, gid)
                if err:
                    conn.sendall(B.build_reply(1152, seq, body=b"", ret_code=err))
                    log("   送礼 role=%s gift=%s -> 失败(次数用完/背包没有/不是礼物)"
                        % (rid, gid))
                else:
                    conn.sendall(B.build_reply(
                        1152, seq, body=playerdata.build_gift_res(pd, rid, lv)))
                    log("-> cmd=1153 (送礼 role=%s gift=%s 档位=%s 认可度 +%s -> %s)"
                        % (rid, gid, lv, delta,
                           (pd["roles"].get(str(rid)) or {}).get("friendship")))
                    # 扣掉的那份礼物要推 1052, 否则背包里数字不变
                    conn.sendall(push_frame(1052, playerdata.build_item_notify(
                        pd, [(gid, 0)])))
                continue
            if cmd == 1127:      # CSHeroBuyInfoReq —— 本服用作"重置角色数据"触发
                playerdata.reset_roles(pd)
                conn.sendall(B.build_reply(1127, seq, body=b""))
                log("-> cmd=1128 (角色数据已重置为 1001..1005)")
                continue
            # --- 更换着装 (皮肤) ---
            if cmd == 1395:      # CSGetRoleSkinInfoReq -> CSGetRoleSkinInfoRes
                conn.sendall(B.build_reply(1395, seq,
                                           body=playerdata.build_skin_info(pd)))
                log("-> cmd=1396 (皮肤列表)")
                continue
            if cmd == 1397:      # CSSetRoleSkinReq{1:RoleId 2:SkinId}
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                ok = playerdata.set_role_skin(pd, m.get(1), m.get(2, 0))
                conn.sendall(B.build_reply(1397, seq, body=b""))
                # ★ 换装后主动推一份新角色数据: 客户端此刻本地数据还是旧的,
                #   不推的话角色界面里的 3D 模型要回大厅再进才换过来。
                if ok:
                    conn.sendall(push_frame(1213, playerdata.build_multi_role_notify(
                        pd, [m.get(1, 0)])))
                log("   角色 %s 更换着装 -> skin=%s (ok=%s, 已推 1213)"
                    % (m.get(1), m.get(2), ok))
                continue
            # ★ 舍友宿舍 / 公寓房间 (P3 2026-10-15)
            #   ★★ UnlockFloor 是命门: 不发的话 view_select_floor_ui 点哪层
            #      都弹「未开启」, 整个公寓进不去。
            if cmd == 1180:      # CSGetAllRoomDataReq(空) -> CSGetAllRoomDataRes
                conn.sendall(B.build_reply(
                    1180, seq, body=playerdata.build_all_room_data(pd)))
                log("-> cmd=1181 (公寓: 解锁楼层 %s, 已入住 %s)"
                    % (playerdata.unlocked_floors(pd), pd.get("rooms")))
                continue
            if cmd == 1172:      # CSAssignApartmentRoomReq{1:HostRoleID 2:FloorIdx 3:RoomIdx}
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                okroom = playerdata.assign_room(pd, m.get(1, 0), m.get(2, 0), m.get(3, 0))
                conn.sendall(B.build_reply(1172, seq, body=b"",
                                           ret_code=0 if okroom else 1))
                log("   分配房间 role=%s -> %s 层 %s 号: %s"
                    % (m.get(1), m.get(2), m.get(3), "成功" if okroom else "拒绝(越界/未解锁)"))
                continue
            if cmd == 1176:      # CSAskIntoRoomReq{1:FloorIdx 2:RoomIdx} -> CSAskIntoRoomRes
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                conn.sendall(B.build_reply(
                    1176, seq,
                    body=playerdata.build_room_enter_res(pd, m.get(1, 0), m.get(2, 0))))
                log("-> cmd=1177 (进房间 %s 层 %s 号)" % (m.get(1), m.get(2)))
                continue
            if cmd == 1178:      # CSGetFloorRoomDataReq —— 客户端没用, 空实现
                m = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 == 0:
                        m[f2] = v2
                conn.sendall(B.build_reply(
                    1178, seq, body=playerdata.build_floor_room_res(pd, m.get(1, 0))))
                continue
            if cmd == 1217:      # CSGetCockpitReq -> CSGetCockpitRes
                conn.sendall(B.build_reply(1217, seq, body=playerdata.build_cockpit(pd)))
                log("-> cmd=1218 (cockpit=%s)" % pd.get("cockpit"))
                continue
            if cmd == 1215:      # CSSetCockpitReq -> 保存驾驶舱
                cockpit = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 2:
                        cd = {}
                        for f3, w3, v3 in B.pb_fields(v2):
                            cd[f3] = v3
                        if 1 in cd:
                            cockpit[str(cd[1])] = cd.get(2, 0)
                pd["cockpit"] = cockpit
                playerdata.save(pd)
                log("   驾驶舱已保存: %s" % cockpit)
                conn.sendall(B.build_reply(1215, seq, body=b""))
                continue
            if cmd == 1026:      # CSRoleLevelUpReq{1:ID 2:UseList} -> 按材料升到能升的最高级
                rid = None
                use_list = []
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 0:
                        rid = v2
                    elif f2 == 2 and w2 == 2:      # CSItemPair{1:ID,2:Count}
                        pair = {}
                        for f3, w3, v3 in B.pb_fields(v2):
                            pair[f3] = v3
                        if 1 in pair:
                            use_list.append((int(pair[1]), int(pair.get(2, 1))))
                if rid is not None:
                    rd, lvl, gold = playerdata.apply_level_up(pd, rid, use_list)
                    log("   角色 %s 升级 -> lv=%d exp=%s (用材料 %s, 扣格 %d)"
                        % (rid, lvl, rd["exp"], use_list, gold))
                    rb = B.pb_v(2, int(rid)) + B.pb_b(3, playerdata.build_gamedata(pd))
                    conn.sendall(B.build_reply(1026, seq, body=rb))
                    continue
            # ==================== 角色养成补全 (2026-10-15) ====================
            # 1018 突破 / 1244 突破奖励 / 1242 能力升级 / 1030 天赋升阶 /
            # 1211 默认天赋 / 1265 图鉴收录奖励。
            #
            # ★ 这 6 条的客户端回包处理器**形状完全一样**: 只读 GameData ->
            #   game_data.AnalysisGameData() 入账。所以服务端把改动塞进
            #   CSGameData{RoleList, ItemList, MoneyList} 就够。
            # ★ 扣什么是客户端先按配置表算好的; 不够它根本不发请求。服务端必须
            #   照同一张表扣, 见 playerdata.role_strength_up / role_ability_up。
            if cmd == 1018:      # CSRoleStrengthReq{1:ID} -> 突破一档
                arg = _pb_ints(body)
                rid = arg.get(1)
                if rid is not None:
                    step, ret, spent = playerdata.role_strength_up(pd, rid)
                    if ret:
                        conn.sendall(B.build_reply(1018, seq, body=b"", ret_code=ret))
                        log("   突破 role=%s -> 失败(材料不足/已满档), 当前档 %s" % (rid, step))
                    else:
                        conn.sendall(B.build_reply(
                            1018, seq,
                            body=playerdata.build_role_strength_res(pd, rid, step)))
                        log("-> cmd=1019 (突破 role=%s -> 档 %s, 花 %s)" % (rid, step, spent))
                        if spent:
                            # ★ 把"扣完还剩多少"一起打出来 —— 光看日志就能确认背包里的
                            #   材料真的被扣了(用户 2026-10-15 要求"你想办法看")。
                            log("   突破后余额: %s"
                                % {i: playerdata._have_item(pd, i) for i, _n in spent})
                            # ★ 付掉的道具必须用 build_item_counts(只发绝对数量):
                            #   build_item_notify 遇到 n==0 会整条跳过, 是**静默无效**的。
                            conn.sendall(push_frame(1052, playerdata.build_item_counts(
                                pd, [i for i, _n in spent])))
                continue
            if cmd == 1244:      # CSRoleStrengthenAwardReq{1:RoleId 2:StrengthenLevel}
                arg = _pb_ints(body)
                rid, st = arg.get(1), arg.get(2)
                if rid is not None and st is not None:
                    gain, ret = playerdata.role_strengthen_award(pd, rid, st)
                    if ret:
                        conn.sendall(B.build_reply(1244, seq, body=b"", ret_code=ret))
                        log("   突破奖励 role=%s 档%s -> 失败(未突破/已领过)" % (rid, st))
                    else:
                        conn.sendall(B.build_reply(
                            1244, seq,
                            body=playerdata.build_role_item_award_res(pd, gain)))
                        log("-> cmd=1245 (突破奖励 role=%s 档%s -> %s)" % (rid, st, gain))
                continue
            if cmd == 1242:      # CSRoleAbilityLevelUpReq{1:RoleId} -> 能力升一级
                arg = _pb_ints(body)
                rid = arg.get(1)
                if rid is not None:
                    lvl, ret, spent = playerdata.role_ability_up(pd, rid)
                    if ret:
                        conn.sendall(B.build_reply(1242, seq, body=b"", ret_code=ret))
                        log("   能力升级 role=%s -> 失败(材料不足/已满级), 当前 %s" % (rid, lvl))
                    else:
                        conn.sendall(B.build_reply(
                            1242, seq, body=playerdata.build_role_gamedata_res(pd)))
                        log("-> cmd=1243 (能力升级 role=%s -> lv%s, 花 %s)" % (rid, lvl, spent))
                        if spent:
                            # ★ 付掉的道具必须用 build_item_counts(只发绝对数量):
                            #   build_item_notify 遇到 n==0 会整条跳过, 是**静默无效**的。
                            conn.sendall(push_frame(1052, playerdata.build_item_counts(
                                pd, [i for i, _n in spent])))
                continue
            if cmd == 1030:      # CSRoleTalenUnlockReq{1:RoleId 2:Rank} -> 天赋升一步
                arg = _pb_ints(body)
                rid, rank = arg.get(1), arg.get(2)
                if rid is not None and rank is not None:
                    st, ret, spent = playerdata.role_talen_unlock(pd, rid, rank)
                    if ret:
                        conn.sendall(B.build_reply(1030, seq, body=b"", ret_code=ret))
                        log("   天赋升阶 role=%s rank=%s -> 失败(材料不足/已满)"
                            % (rid, rank))
                    else:
                        conn.sendall(B.build_reply(
                            1030, seq, body=playerdata.build_role_gamedata_res(pd)))
                        log("-> cmd=1031 (天赋 role=%s rank=%s -> step%s, 花 %s)"
                            % (rid, rank, st, spent))
                        if spent:
                            # ★ 付掉的道具必须用 build_item_counts(只发绝对数量):
                            #   build_item_notify 遇到 n==0 会整条跳过, 是**静默无效**的。
                            conn.sendall(push_frame(1052, playerdata.build_item_counts(
                                pd, [i for i, _n in spent])))
                continue
            if cmd == 1211:      # CSRoleDefaultTalenReq{1:RoleId 2:Rank} -> 设默认天赋
                arg = _pb_ints(body)
                rid, rank = arg.get(1), arg.get(2)
                if rid is not None and rank is not None:
                    ret = playerdata.role_default_talen(pd, rid, rank)
                    conn.sendall(B.build_reply(1211, seq, body=b"", ret_code=ret))
                    log("   默认天赋 role=%s rank=%s -> ret=%s" % (rid, rank, ret))
                continue
            if cmd == 1265:      # CSRoleHandbookAwardReq{1:RoleId} -> 图鉴收录奖励
                arg = _pb_ints(body)
                rid = arg.get(1)
                if rid is not None:
                    gain, ret = playerdata.role_handbook_award(pd, rid)
                    if ret:
                        conn.sendall(B.build_reply(1265, seq, body=b"", ret_code=ret))
                        log("   图鉴奖励 role=%s -> 失败(未拥有/已领过)" % rid)
                    else:
                        conn.sendall(B.build_reply(
                            1265, seq,
                            body=playerdata.build_role_item_award_res(pd, gain)))
                        log("-> cmd=1266 (图鉴奖励 role=%s -> %s)" % (rid, gain))
                continue
            # --- 怪物图鉴 (2026-10-16 新增) ---
            if cmd == 1070:      # CSStageDiscoverMonReq{1:MonId} -> ...Res{}（空包）
                _mid = 0
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 0:
                        _mid = v2
                _new = playerdata.record_monster(pd, _mid)
                conn.sendall(B.build_reply(1070, seq, body=b""))
                log("-> cmd=1071 (图鉴记录怪物 %s%s, 共 %d)"
                    % (_mid, " ★新发现" if _new else "(已有)", len(pd.get("monsters") or [])))
                continue
            if cmd == 1072:      # CSGetStageDiscoverMonListReq -> Res{1:MonIdList}
                conn.sendall(B.build_reply(1072, seq,
                                           body=playerdata.build_monster_list(pd)))
                log("-> cmd=1073 (怪物图鉴 x%d)" % len(pd.get("monsters") or []))
                continue
            # --- 角色互动 (2026-10-16 新增) ---
            if cmd == 1129:      # CSRoleInteractReq{1:ID 2:SceneID 3:DialogID} -> 空包
                _ri, _sc, _dl = 0, 0, 0
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if w2 != 0:
                        continue
                    if f2 == 1:
                        _ri = v2
                    elif f2 == 2:
                        _sc = v2
                    elif f2 == 3:
                        _dl = v2
                playerdata.record_interact(pd, _ri, _sc, _dl)
                # ★ 回**空包**就够 —— 台词、表情、好感全在客户端，
                #   它拿到 SUCCESS 之后什么都不读（proto#3 只有失败才打日志）。
                conn.sendall(B.build_reply(1129, seq, body=b""))
                log("-> cmd=1130 (角色互动 role=%s scene=%s dialog=%s)" % (_ri, _sc, _dl))
                continue
            # --- 关卡扫荡 (2026-10-16 新增) ---
            #   协议与 State 语义见 playerdata.py 里「关卡扫荡」那一段的注释。
            if cmd == 1089:      # CSGetStageSweepReq -> CSGetStageSweepRes
                conn.sendall(B.build_reply(1089, seq,
                                           body=playerdata.build_sweep_info(pd)))
                _st, _done, _left = playerdata.sweep_status(pd)
                log("-> cmd=1090 (扫荡状态 State=%d 已完成 %d 剩 %ds)"
                    % (_st, _done, _left))
                continue
            if cmd == 1091:      # CSStageSweepReq{1:Stage 2:Count} -> CSStageSweepRes{}
                _stage, _cnt = {}, 1
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 2:
                        for f3, w3, v3 in B.pb_fields(v2):
                            if w3 == 0:
                                _stage[f3] = v3
                    elif f2 == 2 and w2 == 0:
                        _cnt = v2
                # ★ 回码由 playerdata 给（131 = 关卡未通关 / 116 = 能源不足 / ...）
                _ok, _why, _ret = playerdata.sweep_start(pd, _stage, _cnt)
                if not _ok:
                    conn.sendall(B.build_reply(1091, seq, body=b"", ret_code=_ret))
                    log("-> cmd=1092 ★扫荡开始被拒(ret=%s): %s" % (_ret, _why))
                    continue
                conn.sendall(B.build_reply(1091, seq, body=b""))
                # 扣了能源 -> 必须推 1067，否则顶栏数字不动（和 1076 同一个道理）
                for _nb in playerdata.build_energy_notify_bodies(pd):
                    conn.sendall(push_frame(1067, _nb))
                log("-> cmd=1092 %s" % _why)
                continue
            if cmd == 1095:      # CSFinishStageSweepReq -> CSFinishStageSweepRes
                # ★★★ 2026-10-16 修: 这里原来写的是 `bs(...)` —— **bizserver 里根本没有
                #   `bs` 这个名字**（序列化助手叫 `B.pb_b`）⇒ 每次都抛 NameError
                #   `(err name 'bs' is not defined)` ⇒ **一个字节都没回**。
                #   客户端一直在等这个回包, 所以「完成重游（领奖）」按钮点不动。
                _rb, _gain = playerdata.sweep_claim(pd)
                conn.sendall(B.build_reply(1095, seq, body=(
                    _rb + B.pb_b(3, playerdata.build_gamedata(pd)))))
                if _gain:
                    # ★ `build_item_counts` 收的是**物品 id 列表**（不是 (id,count) 对）。
                    #   传错了会抛 TypeError ⇒ **连接被关** ⇒ 客户端重连转圈。已修。
                    conn.sendall(push_frame(1052, playerdata.build_item_counts(
                        pd, [i for i, _n in _gain])))
                log("-> cmd=1096 (扫荡完成 -> %d 件奖励)" % len(_gain))
                continue
            if cmd == 1093:      # CSStopStageSweepReq -> CSStopStageSweepRes
                # ★ 中途停止: 只结算**已经跑完的**那几关（没跑完的不给）
                # ★ 同一个 bug（`bs` 未定义）也在这 —— 一并修掉。
                _rb, _gain = playerdata.sweep_claim(pd, only_done=True)
                conn.sendall(B.build_reply(1093, seq, body=(
                    _rb + B.pb_b(2, playerdata.build_gamedata(pd)))))
                if _gain:
                    conn.sendall(push_frame(1052, playerdata.build_item_counts(
                        pd, [i for i, _n in _gain])))
                log("-> cmd=1094 (扫荡中止 -> %d 件奖励)" % len(_gain))
                continue
            if cmd == 1239:      # CSGetStageSweepGroupReq{1:stage} -> ...Res{RoleList}
                _stage = {}
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 2:
                        for f3, w3, v3 in B.pb_fields(v2):
                            if w3 == 0:
                                _stage[f3] = v3
                conn.sendall(B.build_reply(
                    1239, seq, body=playerdata.build_sweep_group(pd, _stage)))
                log("-> cmd=1240 (扫荡可用舍友 x%d)"
                    % len(pd.get("roles") or {}))
                continue
            if cmd == 1076:      # CSStageFinishReq -> CSStageFinishRes
                # ★★★ 2026-10-16 重大纠正: `CSStageFinishReq{1:Type 2:Stage 3:BattleData}`
                #   里的 **`Type` 不是"胜/负"**! 它是 `EnmStageFinishType`:
                #       ESFT_Normal = 0   正常打完(胜负都有可能)
                #       ESFT_Cancel = 1   玩家在战斗里**主动退出**(view_battle_main 的
                #                         退出确认回调设 `modeData.activeExit=true` →
                #                         play_normal_mode 据此把 finishType 置 1)
                #       ESFT_Retry  = 2   重试
                #   (证据: `play_normal_mode` proto#4 →
                #      `finishType = activeExit and ESFT_Cancel or ESFT_Normal`)
                #   ★ 之前把 `Type == 1` 当成"胜利", 语义**整个反了** —— 于是
                #     玩家的"退出"被记成通关(还把章节标成熟), 而且**不退能源**。
                #     用户报的「退出或失败没有返还能源」就是它。
                #   胜负只能看 `BattleData.RemainHp`(客户端 modeData.curHp,
                #   队伍剩余血量; 团灭=0)。
                ftype = None
                stage = {}
                remain_hp = None
                for f2, w2, v2 in B.pb_fields(body or b""):
                    if f2 == 1 and w2 == 0:
                        ftype = v2
                    elif f2 == 2 and w2 == 2:
                        for f3, w3, v3 in B.pb_fields(v2):
                            if w3 == 0:
                                stage[f3] = v3
                    elif f2 == 3 and w2 == 2:        # BattleData{1:RemainHp}
                        for f3, w3, v3 in B.pb_fields(v2):
                            if f3 == 1 and w3 == 0:
                                remain_hp = v3
                # 判定: 只有「正常打完」且「基地还有血量」才算胜利
                win = playerdata.stage_finish_is_win(ftype, remain_hp)
                # ★★★ 星数 = 基地剩余血量 按 `pass_monster`（这一关的血量上限）折算
                #   ⇒ `max(1, floor(剩余/上限 × 3))`。见 `playerdata.stage_score`。
                _score = playerdata.stage_score_of(stage, ftype, remain_hp)
                _why = playerdata.ESFT_NAME.get(ftype, "Type=%s?" % ftype)
                _fc_gain = []                     # ★ 本次要发的掉落
                _fc_first = False                 # ★ 首通（只用来打日志）
                _fc_first3 = False                # ★ 首三星（只用来打日志）
                _fc_unlock = False                # ★ 本次结算后这关能不能扫荡
                if stage:
                    key = "%d:%d:%d:%d" % (stage.get(1, 0), stage.get(2, 0),
                                           stage.get(3, 0), stage.get(4, 0))
                    if win:
                        # ★★ 先结算掉落（`stage_clear_award` 内部拿**旧**星数判首通/首三星），
                        #   再写 `stages` —— 顺序不能反，反了就永远不发那两层。
                        #   ★ 三层: output_type==3 每次掉; ==2 首次通关; ==1 首次三星。
                        _fc_gain, _fi = playerdata.stage_clear_award(pd, stage, _score)
                        _fc_first = _fi["first_pass"]
                        _fc_first3 = _fi["first_3star"]
                        playerdata.stage_note_score(pd, stage.get(1, 0), stage.get(2, 0),
                                                    stage.get(3, 0), stage.get(4, 0), _score)
                        pd["chapters"]["%d:%d" % (stage.get(1, 0), stage.get(2, 0))] = 1
                        # ★ 日常任务 50005/6/7「通关任意关卡 N 次」
                        playerdata.bump_daily(pd, "stage_win")
                        # ★★ 胜利 = 这次扣费坐实, 销掉待退记录(否则之后任何一次
                        #    "非胜利"的 1076 都会把这次的能源退回来 = 白送)。
                        playerdata.settle_stage_intellect(pd)
                    else:
                        # ★★ 失败 / 中途退出 -> 把进场扣掉的能源**原样退还**(用户要求)。
                        #   ★ 失败/退出时 CSGameData 里**没有 Intellect 字段**
                        #     (见 handle() 里能源结算那段注释), 所以必须自己推 1067。
                        _r_ok, _r_n, _r_left = playerdata.refund_stage_intellect(pd)
                        if _r_ok:
                            for _nb in playerdata.build_energy_notify_bodies(pd):
                                conn.sendall(push_frame(1067, _nb))
                            log("      ↳ %s -> 退还能源 %d (剩 %d)" % (_why, _r_n, _r_left))
                        else:
                            log("      ↳ %s -> 没有待退的能源(没进场/已结算过)" % _why)
                    playerdata.save(pd)
                    _fc_unlock = playerdata.sweep_unlocked(
                        pd, stage.get(1, 0), stage.get(2, 0), stage.get(3, 0), stage.get(4, 0))
                    log("   关卡结算 %s %s (Type=%s/%s, 基地血量=%s -> %d星, 扫荡=%s, 共%d关)"
                        % (key, "胜利" if win else "未胜利", ftype, _why,
                           remain_hp, _score, "开" if _fc_unlock else "锁",
                           len(pd["stages"])))
                    sb = B.pb_v(1, stage.get(1, 0)) + B.pb_v(2, stage.get(2, 0)) + \
                         B.pb_v(3, stage.get(3, 0)) + B.pb_v(4, stage.get(4, 0))
                    # ★★ FinishData 用 `build_finish_data` 造 —— 它会把**掉落**放进
                    #   `DropList`(field 3)、把**扫荡解锁位**放进 `IsUnlockSweep`(field 6)。
                    #   以前这里是手搓的 `pb_v(1, score)`：只有 Score，没有 DropList、
                    #   也没有 IsUnlockSweep ⇒ 掉落一个不发、扫荡入口永远不开。
                    rb = B.pb_b(1, playerdata.build_finish_data(_score, _fc_gain, _fc_unlock)) + \
                         B.pb_b(2, sb) + B.pb_b(3, playerdata.build_gamedata(pd))
                    conn.sendall(B.build_reply(1076, seq, body=rb))
                    if _fc_gain:
                        # ★ 掉落要推 1052，否则背包/顶栏不会刷新
                        conn.sendall(push_frame(1052, playerdata.build_item_counts(
                            pd, [i for i, _n in _fc_gain])))
                    if _fc_gain or _fc_unlock:
                        log("      ↳ ★掉落 %d 件%s%s: %s"
                            % (len(_fc_gain),
                               "(含首通)" if _fc_first else "",
                               "(含首三星)" if _fc_first3 else "",
                               _fc_gain[:6]))
                    continue

            # --- 设置(KeyValuePair)服务端存储 ---
            if cmd == 1235:        # CSGetAllSettingsReq -> CSGetAllSettingsRes
                sd = load_settings()
                rb = b""
                for k, v in sd.items():
                    kv = B.pb_v(1, int(k))
                    if isinstance(v, str):
                        kv += B.pb_b(3, v)
                    else:
                        kv += B.pb_v(2, int(v))
                    rb += B.pb_b(1, kv)
                conn.sendall(B.build_reply(1235, seq, body=rb, ret_code=0))
                log("-> cmd=1236 (settings: %s)" % sd)
                continue
            if cmd == 1237:        # CSSetSettingReq -> CSSetSettingRes
                sd = load_settings()
                for ff, ww, val in B.pb_fields(body or b""):
                    if ff == 1 and ww == 2:
                        kv = {}
                        for f2, w2, v2 in B.pb_fields(val):
                            kv[f2] = v2
                        key = kv.get(1)
                        if key is not None:
                            sd[str(key)] = kv.get(2, kv.get(3, 0))
                save_settings(sd)
                conn.sendall(B.build_reply(1237, seq, body=b"", ret_code=0))
                log("-> cmd=1238 (settings saved: %s)" % sd)
                continue

            # ★ 心跳: 顺手定期推一条任务类型表, 让任务页的页签重算(见 task_refresh_due 注释)
            if cmd == 1005:
                conn.sendall(B.build_reply(1005, seq, body=B.pb_v(1, int(time.time())),
                                           ret_code=0))
                if task_refresh_due():
                    conn.sendall(push_frame(1111,
                                            playerdata.build_task_type_list(pd)))
                    log("   心跳顺带推 1111 -> 刷新任务页签")
                # ★ 荣誉引导(1/2): 每连接推一次带荣誉条目的 CSTaskNotify。
                #   `logic_challenge` 只监听 1105, 没有这一条它永远不会去要荣誉数据。
                if not _honor_pushed:
                    _honor_pushed = True
                    conn.sendall(push_frame(1105, playerdata.build_honor_notify(pd)))
                    log("   荣誉引导(1/2) -> 推 1105, 带 %d 条荣誉(客户端接着会去要成就列表)"
                        % len(playerdata.honor_ids()))
                continue

            reply = build_reply(cmd, seq, body)
            if reply is not None:
                conn.sendall(reply)
                log("-> cmd=%d seq=%d" % (cmd + 1, seq))
    except socket.timeout:
        log("(timeout)")
    except Exception as e:
        log("(err %s)" % e)
    finally:
        # ★ 持锁的连接一断, 立刻清锁清码 —— 下次登录重新发码(用户要求)。
        #   被拒的连接拿不到锁, 这里自然什么也不做。
        freed = lock_release(conn)
        if freed is not None:
            log("===== CLOSE %s (锁已释放, 8 位码 %d 作废) =====" % (addr, freed))
        else:
            log("===== CLOSE %s =====" % (addr,))
        try:
            conn.close()
        except Exception:
            pass


def build_reply(cmd, seq, req_body):
    """返回要发送的线上字节; None 表示不回。"""
    # 优先查热重载数据表 bizdata.json
    e = load_bizdata().get(str(cmd))
    if e is not None:
        body = bytes.fromhex(e.get("body_hex", "").replace(" ", ""))
        return B.build_reply(cmd, seq, body=body, ret_code=e.get("ret", 0),
                             res_cmd=e.get("res_cmd"))

    if cmd == 1001:            # CSLoginReq -> CSLoginRes
        # 注意: 客户端把"空 body"当成超时错误(logic_login_main proto#22), 必须非空。
        now = int(time.time())
        body = B.pb_v(2, now) + B.pb_v(3, 1) + B.pb_v(8, now) + B.pb_v(10, 0)
        return B.build_reply(1001, seq, body=body, ret_code=0)
    if cmd == 1005:            # CSHeartReq -> CSHeartRes
        return B.build_reply(1005, seq, body=B.pb_v(1, int(time.time())), ret_code=0)
    if cmd == 1198:            # CSGetAllCompletedGuideReq -> CSGetAllCompletedGuideRes
        # GmFinishGuide 置真 => 客户端 shareData.FinishAllGuide=true, 跳过全部新手引导
        return B.build_reply(1198, seq, body=B.pb_v(1, 1), ret_code=0)
    # 其余: 一律回 cmd+1, RetCode=0, 空体
    return B.build_reply(cmd, seq, body=b"", ret_code=0)


# 启动时预热的大配置表 —— 登录路径上会被立刻请求的那几张。
# dataconfig_stage_minor 有 234 万行 / 47MB, 冷解析 1.5 秒, 不预热就是"登录转圈"。
WARM_TABLES = ("dataconfig_stage_minor", "dataconfig_itembase",
               "dataconfig_task_base", "dataconfig_system_mail",
               "dataconfig_shop_goods", "dataconfig_role_config")


def warm_up():
    """后台线程预热配置表缓存。纯读, 不碰存档, 和请求线程不冲突。"""
    try:
        import cfgdata
        for t in WARM_TABLES:
            try:
                cfgdata.load(t)
            except Exception:
                pass
        log("配置表预热完成 (登录不再卡解析)")
        # ★ 角色名索引（「角色工具」页要用）。以前必须手动跑
        #   `python tools\make_roles_index.py`，不生成的话那一页是**全空的**。
        #   这里在预热线程里顺手生成一次（幂等：文件已在就一个字节都不动）。
        try:
            _wrote, _n = cfgdata.ensure_roles_index(log=log)
            if _n:
                log("角色名索引: %s (%d 个角色)"
                    % ("已生成" if _wrote else "沿用", _n))
            else:
                # 启动时渠道还没定、配置表还没 dump 出来是**正常**情况 ——
                # 客户端登录后切到对应渠道目录时会补生成（见 handle()）。
                log("角色名索引: 暂缺（等客户端登录、渠道确定后再生成）")
        except Exception as _e:
            log("角色名索引生成失败(不影响启动): %r" % (_e,))
    except Exception as e:
        log("配置表预热失败(不影响功能): %r" % (e,))


def main():
    try:
        sys.stdout.reconfigure(errors="replace")     # 控制台编码兜底, 避免中文打印炸掉
    except Exception:
        pass
    open(LOG, "w", encoding="utf-8").close()

    # ★ 2026-10-17: 数据目录迁移(程序目录 -> 用户数据目录)。**幂等**, 重复启动无副作用。
    #   ★ 必须在 cfg_report / load() 之前 —— 它决定这次到底读哪份表、哪份存档。
    try:
        paths.migrate_legacy(log)
    except Exception as e:
        log("!! 数据目录迁移失败(不影响启动): %r" % (e,))
    log("    %s" % paths.summary())

    # ★ 配置表体检 —— 缺表时服务端**不崩但功能全空**, 最容易让人以为"服务端坏了"。
    #   所以启动时明确喊出来, 并给出拿到配置表的步骤。
    try:
        import cfgdata
        # ★ 启动时先按 HTTP 层握手留下的渠道定一次目录(第一个请求还没到之前就得对)。
        cfgdata.sync_client_from_state()
        _ok, _rep = cfgdata.cfg_report()
        for _line in _rep.splitlines():
            log(_line)
        if not _ok:
            print(_rep, flush=True)
    except Exception as e:
        log("!! 配置表体检失败(不影响启动): %r" % (e,))

    # ★★★ 2026-10-17: 存档按「客户端(渠道+版本)」分 —— 必须在下面 load() **之前**定一次。
    #   此刻 `state/client_channel.json` 里可能还留着**上一次运行**的客户端身份;
    #   如果还在, 这次启动就直接落在对的作用域里, 不会去读写别个客户端的存档。
    #   第一个业务请求到达后(`handle()` 里)会再校一次 —— 客户端换了会当场切换。
    try:
        _sp, _sp_new = playerdata.refresh_save()
        log("★ 存档作用域: %s%s" % (_sp, "  (本次重定)" if _sp_new else "  (沿用)"))
    except Exception as e:
        log("!! 重定存档路径失败(不影响启动): %r" % (e,))

    try:
        _st, _st_new = refresh_settings()
        log("★ 设置作用域: %s%s" % (_st, "  (本次重定)" if _st_new else "  (沿用)"))
    except Exception as e:
        log("!! 重定设置路径失败(不影响启动): %r" % (e,))

    # ★ 导出"本地存档用户名"给客户端登录页看(它在连上本服之前就要显示)。
    #   见 playerdata.write_local_name / openKleinsPatcher 的 inject_login。
    try:
        playerdata.write_local_name(playerdata.load())
    except Exception as e:
        log("!! 导出用户名失败(不影响启动): %r" % (e,))
    threading.Thread(target=warm_up, daemon=True).start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(16)
    log("业务服监听 127.0.0.1:%d" % PORT)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
