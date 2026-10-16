#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
玩家数据持久化: 存档落盘到 player.json, 业务服据此生成响应, 并在玩家操作后更新+保存。

存档结构:
{
  "actor":  {accId,name,level,exp,intellect,diamond,gold,copper},
  "roles":  {"1001": {level,exp,strength,potential,ability}, ...},
  "items":  {"1": 50000000, ...},
  "stages": {"<type>:<chapter>:<main>:<minor>": score, ...},   # 关卡进度
  "chapters": {"<type>:<chapter>": 1, ...},                    # 章节开放
  "settings": {key: value}                                     # 已有 settings.json
}
"""
import json
import os
import time

import bizproto as B
import paths

# ★ 存档路径可以用环境变量 KLEINS_PLAYER_JSON 覆盖。
#   自检脚本(_chk_*.py)必须用它指向临时文件 —— 因为 save() 写的是这个**固定路径**,
#   脚本里随手 save(假数据) 会**直接覆盖玩家真存档**(2026-10-15 我在这里翻过车)。
# ★ 2026-10-17: 默认落点从「程序目录」改成**用户数据目录** saves\ (见 paths.py)。
# ★★★ 2026-10-17 二改: 再按**客户端(渠道+版本)**分子目录 —— 见下面 refresh_save()。
#   这个初始值只是"还没认出客户端"时的兜底(平面目录), 第一个请求到达后就会被重定。
SAVE = paths.data_path("KLEINS_PLAYER_JSON", "saves", "player.json")

# 当前存档所属的客户端作用域(如 `steam-GL1.5.0.0`); None = 还没认出来 / 用的平面目录。
_save_scope = None


def refresh_save():
    """按当前客户端重定存档路径。返回 `(路径, 有没有变)`。

    ★★ 为什么存档也要按客户端分: 配置表早就按渠道分了
      (`resources\\cfg\\<SubPlatType>\\`), 但存档只有一份 ⇒ **换客户端测试会串档**:
      在国服客户端领到的道具 id, Steam 客户端可能没有 —— 正是
      5133 / 头像 64,65 / 卡池 35..40 那一类崩的成因。
      用户要求: 「把 saves\\ 按客户端版本号+渠道(如 steam1.5.0.0)分开」。

    ★ 调用点必须**赶在 load() 之前**: `bizserver.main()` 启动时一次,
      `bizserver` 每个请求里(跟在渠道切换后面)一次 —— 同 do_reset.flag 那套。

    ★ `KLEINS_PLAYER_JSON` 一旦设了就固定不动: 自检脚本靠它把存档隔离到临时文件。
    ★ 认不出客户端(没有 client_channel.json) ⇒ 保持平面 `saves\\player.json`,
      行为和改动前一致, 不算新 bug。
    """
    global SAVE, _save_scope
    env = os.environ.get("KLEINS_PLAYER_JSON")
    if env:
        SAVE = env
        return SAVE, False
    sc = paths.client_scope()
    if sc == _save_scope:
        return SAVE, False                 # 没变 ⇒ 一个字节都不动
    _save_scope = sc
    SAVE = os.path.join(paths.saves_dir(sc), "player.json")
    if sc:
        # 第一次认出客户端: 把老的**单份**存档收编进来(幂等; 作用域目录里已有就跳过)。
        # ★ 搬的目标就是上面 SAVE 指的地方, 所以搬完不用再改路径。
        paths.adopt_flat_save(paths.saves_dir(sc))
    return SAVE, True

DEFAULT = {
    "actor": {"accId": 10000001, "name": "Kleins", "level": 10, "exp": 0,
              "intellect": 120, "diamond": 99999, "gold": 99999999, "copper": 99999999},
    "roles": {"1001": {"level": 1, "exp": 0, "strength": 0, "potential": 0, "ability": 1},
              "1002": {"level": 1, "exp": 0, "strength": 0, "potential": 0, "ability": 1},
              "1003": {"level": 1, "exp": 0, "strength": 0, "potential": 0, "ability": 1},
              "1004": {"level": 1, "exp": 0, "strength": 0, "potential": 0, "ability": 1},
              "1005": {"level": 1, "exp": 0, "strength": 0, "potential": 0, "ability": 1}},
    "items": {"1": 50000000, "3": 50000000, "5": 50000000, "75": 50000000},
    "stages": {},
    "chapters": {},
    "cockpit": {},          # {"<position>": roleId}  驾驶舱摆放
    # 编队: GroupIdx -> {name, members:[[roleId, talentId], ...]}
    # QueueIndex 枚举(客户端硬编码): 1..4 = 第一~四编队, 5 = 无尽, 6 = 锁定队
    "groups": {},
}

# 客户端 QueueIndex: FirstQueue=1 SecondQueue=2 ThirdQueue=3 FourthQueue=4
#                     EndlessQueue=5 LockQueue=6
GROUP_NAMES = {"1": "编队一", "2": "编队二", "3": "编队三", "4": "编队四",
               "5": "", "6": ""}
GROUP_COUNT = 6
MAX_MEMBER = 10


def default_groups():
    g = {}
    for i in range(1, GROUP_COUNT + 1):
        k = str(i)
        g[k] = {"name": GROUP_NAMES.get(k, ""), "members": []}
    # 第一编队预置 5 个测试角色, 打开即可直接开战
    g["1"]["members"] = [[1001, 0], [1002, 0], [1003, 0], [1004, 0], [1005, 0]]
    return g


# ★★★ actor 的三种货币 ↔ item id 的映射（2026-10-17）。
#   来源: 反汇编 `lua_data_center.RefreshBaseInfo`(proto#2) ——
#       M.XiYin = data.Diamond / M.Ge = data.Gold / M.Yin = data.Copper
#   这三个字段随**登录包**(CSLoginRes field 5/6/7)下发, `load()` 里必须让它们
#   和 `items` 保持一致, 否则读 actor 的显示路径永远停在登录那一刻的值
#   （玩家实测: 顶栏一直三个 10000, 充值/兑换都不动）。
ACTOR_CURRENCY_MIRROR = (("diamond", 1), ("gold", 2), ("copper", 3))


def load():
    try:
        with open(SAVE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        for k, v in DEFAULT.items():
            d.setdefault(k, json.loads(json.dumps(v)))
    except Exception:
        d = json.loads(json.dumps(DEFAULT))
    # 编队: 补齐 1..GROUP_COUNT, 缺哪个补哪个(不覆盖已有内容)
    g = d.get("groups") or {}
    if not g:
        g = default_groups()
    else:
        for i in range(1, GROUP_COUNT + 1):
            k = str(i)
            if k not in g:
                g[k] = {"name": GROUP_NAMES.get(k, ""), "members": []}
            g[k].setdefault("name", GROUP_NAMES.get(k, ""))
            g[k].setdefault("members", [])
    # 编队保持"稠密列表"(和原版一致): 去掉空位(roleId=0), 顺序不变。
    # (槽位制实验已整体回退, 见 inject_queue_slot.py --revert)
    for k, gg in g.items():
        mem = gg.get("members") or []
        gg["members"] = [m for m in mem if int(m[0]) != 0]
    d["groups"] = g
    # ★★★ 客户端**没有的**道具一律从背包里清掉（见 CLIENT_MISSING_ITEM_IDS 那段注释）。
    #   为什么必须清: Steam 版客户端没有 5133 ⇒ 服务端发下去 ⇒ 客户端
    #   `logic_store.CreateItem` 查不到配置 ⇒ `item.Config = nil` ⇒ `ChangeData` 抛异常
    #   ⇒ **退出战斗卡死**。
    #   清在**归一化最前面** —— 后面的步骤(合同兑换/月卡/…)都别再看到它。
    #   ★ 不是"下发的时候过滤": 那样存档里会一直留着这个键, 每次下发都得再过滤一遍。
    _it = d.get("items")
    if isinstance(_it, dict):
        _gone = [k for k in _it if k.isdigit() and int(k) in CLIENT_MISSING_ITEM_IDS]
        if _gone:
            for _k in _gone:
                _it.pop(_k)
            save(d)
    _ensure_shop_currency(d)
    _seed_currency_items(d)       # 货币起始额度(只补不存在的键, 见 CURRENCY_SEED)
    # ★★★ 2026-10-17: 把 `actor` 的三种货币**镜像成 item 1/2/3**。
    #
    #   为什么（反汇编 `lua_data_center.RefreshBaseInfo` proto#2 定案）:
    #       M.XiYin = data.Diamond     -- 稀因 ← Diamond
    #       M.Ge    = data.Gold        -- 格   ← Gold
    #       M.Yin   = data.Copper      -- 因   ← Copper
    #   而这三个字段是**登录包**（CSLoginRes field 5/6/7）带过去的。
    #
    #   ★ `apply_level_up` 里那句"客户端根本不读 gold/diamond/copper"只对
    #     `CSGameData.MoneyList` 成立（那条路只认 ERDT_* 五个 key）；
    #     **登录包这条路是读的** —— 玩家报「充值后主页稀因不增加 / 顶栏一直三个
    #     10000」就是因为 items 变了、actor 没变，读 actor 的显示路径永远是老值。
    #
    #   ⇒ 每次 load() 都对齐一次, 两边永远不会分叉。只在真的不同时才写盘。
    _actor = d.setdefault("actor", {})
    _items_now = d.get("items") or {}
    _mirrored = False
    for _akey, _iid in ACTOR_CURRENCY_MIRROR:
        _want = int(_items_now.get(str(_iid), 0))
        if _actor.get(_akey) != _want:
            _actor[_akey] = _want
            _mirrored = True
    if _mirrored:
        save(d)
    # 主线步骤: 旧存档只有 task_step(领完就把当前步 +1, 最后一步加不动 =>
    # 第 6 步能无限重复领)。补一个 task_step_done 记录"已领到第几步"。
    if "task_step_done" not in d and d.get("task_step"):
        d["task_step_done"] = max(0, int(d["task_step"]) - 1)
        save(d)
    # 新手七日宝箱: 旧代码把「宝箱 id」当成 key 存进 seven_day_got, 现在改成
    # 「finish_point」。不迁移的话 finish_point=5 那一档会被旧的 id=5 挡住。
    if int(d.get("seven_day_got_ver") or 0) < 2:
        d["seven_day_got"] = []
        d["seven_day_got_ver"] = 2
        save(d)
    # 月卡: ① 旧存档的 month_card_end(日期) 迁移成天数模型
    #       ② 没有月卡状态但买过月卡道具的, 先给一张
    #       ③ 按自然日扣减天数
    try:
        old_end = d.pop("month_card_end", None)
        if old_end and not d.get("month_card_days"):
            import datetime as _dt
            try:
                y, m, dd = (int(x) for x in old_end.split("-"))
                left = (_dt.date(y, m, dd) - _dt.date.today()).days
            except Exception:
                left = 0
            if left > 0:
                d["month_card_days"] = left
        # ★★ 2026-10-16 修「首次买月卡给了 60 天」+「买月卡拿到一个月卡礼包道具」：
        #   月卡礼包(item 4029, func=8, use_way=2, bag_not_show=1) 是**发放那一刻就
        #   该开掉**的礼包 —— 它自己就是"打开后获得 30 天月卡"。
        #   旧逻辑只"包里有礼包就补 30 天"、**却不把礼包扣掉** ⇒ 白拿 30 天；
        #   再买一次 = 30(白送) + 30(购买) = 60，就是你看到的现象。
        #   现在：残留礼包一律清掉；只有"真的没有天数"(老存档)才按礼包数折算一次。
        _boxes = int((d.get("items") or {}).get(str(MONTH_CARD_ITEM_ID), 0))
        changed = False
        if _boxes > 0:
            if d.get("month_card_days") is None:
                d["month_card_days"] = MONTH_CARD_DURATION_DAYS * _boxes
                d["month_card_buy_date"] = _today_str()
            d["items"].pop(str(MONTH_CARD_ITEM_ID), None)
            # ★ 清掉了就得告诉客户端"这格没了"，否则背包界面上那张礼包一直挂着
            _PENDING_ITEM_PUSH.append(MONTH_CARD_ITEM_ID)
            changed = True
        if _mc_tick(d):
            changed = True
        if old_end or changed:
            save(d)
    except Exception:
        pass
    # ★ 舍友合同(func=5, AutoUse)自动兑换 —— 见 _auto_use_contracts 注释。
    #   挂在归一化的**最后**, 因为前面几步可能刚补进合同道具。
    #   这样基准档/开箱/邮件/商店/任务奖励所有来源都覆盖到。
    #
    #   ★ role_gain_count 是本次新加的计数。**老存档没有它** —— 已拥有的角色
    #     必须先补成 1(表示"已获得 1 次"), 否则下一次获得会被算成 gain_time=0,
    #     只命中 repeat_gain 第 1 档(只给邂逅之迹 x1), **拿不到专属补充协议**,
    #     因序链照样卡死。(实测踩过)
    gc = d.setdefault("role_gain_count", {})
    _gc_new = False
    for _rk in (d.get("roles") or {}):
        if str(_rk) not in gc:
            gc[str(_rk)] = 1
            _gc_new = True
    if _gc_new:
        save(d)
    try:
        _auto_use_contracts(d)
    except Exception:
        pass
    return d


def save(d):
    with open(SAVE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    write_local_name(d)


# --- 给客户端登录页看的"用户名" ---
# ★ 客户端登录页(账号登录)要显示**本地存档的用户名**, 但那时候它还没连上业务服
#   (登录页在 TCP 之前), 更读不到 player.json。所以服务端把名字**导出一个纯文本文件**,
#   客户端补丁 `_re\inject_login.py` 用 io.open 直接读 —— 比在 Lua 里解析 JSON 可靠得多。
# ★ 没有这一步的话, 登录页显示的是上次记住的账号(用户报的"残留的账户信息")。
LOCAL_NAME_FILE = paths.data_path("KLEINS_LOCAL_NAME_FILE", "state",
                                  "local_name.txt")


def write_local_name(d):
    """把 actor.name 写到 local_name.txt。读不到就写空(客户端有兜底)。"""
    try:
        name = ""
        if isinstance(d, dict):
            name = (d.get("actor") or {}).get("name") or ""
        with open(LOCAL_NAME_FILE, "w", encoding="utf-8") as f:
            f.write(str(name).strip())
        return True
    except Exception:
        return False


def uv(f, v):
    return B.pb_v(f, v)


def bs(f, v):
    return B.pb_b(f, v)


NOW = int(time.time())


# ---------------- cs.CSActorData ----------------
def build_actor(d, acc_id=None):
    """acc_id 非 None 时**覆盖**存档里的 accId 发给客户端。

    ★ 单客户端锁的 8 位码走这里(见 bizserver.lock_code)。存档里的 actor.accId
      是内部 id, **不再发给客户端** —— 客户端的本地存档按 AccountInfo.openId 做键,
      和 accId 无关(实测), 所以换显示号不会动到任何账号数据。
    """
    a = d["actor"]
    r = b""
    r += uv(1, a["accId"] if acc_id is None else int(acc_id))
    r += bs(2, a["name"])
    r += uv(3, a["intellect"])
    # 4: Intellect_CountDown —— 距下一点能源恢复的**秒数**。
    #    以前写死 0 => 客户端拿到的倒计时是 0 => "能源自然恢复"那条整个不动。
    r += uv(4, energy_countdown(d))
    r += uv(5, a["diamond"])
    r += uv(6, a["gold"])
    r += uv(7, a["copper"])
    r += bs(8, uv(1, 1) + uv(2, 0))
    r += uv(10, a["level"])
    r += uv(11, a["exp"])
    r += uv(12, 120)
    r += uv(13, 0)
    return r


# ---------------- cs.CSLoginRes ----------------
def build_login(d, acc_id=None):
    r = b""
    r += bs(1, build_actor(d, acc_id))
    r += uv(2, NOW)
    r += uv(3, 1)
    r += uv(4, 0)
    r += uv(7, 0)
    r += uv(8, NOW)
    r += uv(9, 0)
    r += uv(10, 0)
    return r


# ---------------- cs.CSRoleData ----------------
def build_role(role_id, rd):
    r = b""
    r += uv(1, int(role_id))
    r += uv(2, rd.get("level", 1))
    r += uv(3, rd.get("exp", 0))
    r += uv(4, rd.get("strength", 0))
    r += uv(5, rd.get("potential", 0))
    # 6: TalentList(repeated CSTalent{1:Rank, 2:Step})
    #    客户端 (logic_role_data.UpdateRoleData) 会把这里的 Rank/Step 抄进角色数据。
    #    Step 是"天赋等级", 会被拿去查 dataconfig_talent_cfg(...,'level',Step),
    #    而那张表的 level 从 1 开始 —— 给 0 会查空 => 编队界面 UpdateRoleItem 崩,
    #    表现为名字/品质/等级/命之座全是占位符(图标已画完就崩了)。
    try:
        import cfgdata as _c
        steps = rd.get("talent_steps") or {}
        for rank, _mx in _c.role_ranks(role_id):
            r += bs(6, uv(1, rank) + uv(2, int(steps.get(str(rank), 1))))
    except Exception:
        pass
    r += uv(7, NOW)
    # 8: FriendshipValue(认可度值) —— ★ 2026-10-15 补: 以前**完全没下发这个字段**,
    #    所以认可度恒为 0, 送礼也不涨(客户端显示的就是它)。
    r += uv(8, int(rd.get("friendship") or 0))
    # 9: DefaultTalen(默认天赋阶位) —— 1211 设置, 以前写死 0。
    r += uv(9, int(rd.get("default_talen") or 0))
    # 10: DailyReceiveGiftTimes(今日已送次数) —— 以前写死 0, 每日上限形同虚设。
    r += uv(10, int(rd.get("daily_gift_times") or 0))
    r += uv(12, rd.get("ability", 1))
    # 13: StrengthAward(repeated Uint32Id{1:ID}) —— **已领过奖励的突破档位**。
    #     ★ 2026-10-15 补: 以前完全没下发 => 客户端 logic_role_data.IsCanGetStepAward
    #     拿它跟 StrengthLevel 比, 永远算不出"可领" => 突破奖励按钮/红点全哑。
    for _st in (rd.get("strength_award") or []):
        r += bs(13, uv(1, int(_st)))
    r += uv(14, 0)
    # 15: HandbookAward(bool) —— 图鉴收录奖励是否已领 (false 时客户端亮红点)。
    r += uv(15, 1 if rd.get("handbook_award") else 0)
    r += uv(16, 0)
    r += uv(17, rd.get("skin", 0))       # 当前着装(皮肤)
    return r


def build_role_list(d):
    r = b""
    for rid, rd in d["roles"].items():
        r += bs(2, build_role(rid, rd))
    return r


# ---------------- cs.CSGetStoreRes ----------------
def build_store(d):
    r = b""
    for iid, cnt in d["items"].items():
        r += bs(1, uv(1, int(iid)) + uv(2, int(cnt)) + uv(3, 0))
    return r


# ---------------- cs.CSGetStageListRes ----------------
# CSGetStageListRes { 1:StageList(repeated CSStageScore) 2:LastStage(CSStageBase)
#                     3:ChapterList(repeated CSStageChapter) }
#   CSStageChapter { 1:Type 2:Chapter 3:IsOpen(bool) 4:WeekDay(repeated Uint32Id) }
# 章节表是本地配置(dataconfig_stage_chapter), 但"是否开放"由服务端 IsOpen 决定,
# 所以必须把全部章节都下发成 IsOpen=true, 否则主线只显示第0章。
def build_all_chapters():
    import cfgdata
    r = b""
    seen = set()
    for row in cfgdata.load("dataconfig_stage_chapter").values():
        tp = row.get("type")
        ch = row.get("chapter")
        if not isinstance(tp, int) or not isinstance(ch, int):
            continue
        # ★ 初霜与春风(4011/4012/4013)在这里也冒充主线 —— 客户端那张表被
        #   inject_cfgdump.py 换成 type=1 了，服务端的 ChapterList 必须跟着改，
        #   否则两边对同一章报的类型不一样。
        tp = stage_alias_type(tp, ch)
        if (tp, ch) in seen:
            continue
        seen.add((tp, ch))
        r += bs(3, uv(1, tp) + uv(2, ch) + uv(3, 1))            # IsOpen = true
    return r


def _stage_progress(d):
    """存档里的通关记录 -> ({(tp,ch,mn,mi): score}, {(tp,ch): (max_main_id, score)})

    ★ 为什么必须补全（2026-10-15，用户报"任务里只有一个标签"）:
      客户端 `logic_chapter.IsFinishLevel(type, chapter, main_id, minor_id)` 是拿
      `string.format("%s_%s_%s_%s", ...)` 去本地的 `StageDict` **精确查表**看 `Score > 0`。
      而 `logic_task_main.CheckUnlock()`（= 任务界面 日常/周常/发展 三个页签的显隐开关）
      查的正是 `(1, 1000, 7, 1)` —— 第 1000 章第 7 关。
      玩家存档里只有 `1:1000:13:1`（第 13 关，3 星）：**已经打到第 13 关的人当然打通了
      第 7 关**，可精确查表查不到 `1_1000_7_1` ⇒ 返回 false ⇒ 三个页签全被 SetActive(false)
      ⇒ 「任务里只有一个标签」，周常兑换入口自然也就没了。
    """
    raw, best = {}, {}
    for k, v in (d.get("stages") or {}).items():
        try:
            tp, ch, mn, mi = (int(x) for x in str(k).split(":"))
            sc = int(v)
        except Exception:
            continue
        if sc <= 0:
            continue
        # ★ 初霜与春风: 老存档里可能是真 type=4 / minor 2·3，统一归一到冒充的 1 / 1。
        tp = stage_alias_type(tp, ch)
        mi = stage_alias_minor(tp, ch, mi)
        raw[(tp, ch, mn, mi)] = sc
        key = (tp, ch)
        if mn > best.get(key, (0, 0))[0]:
            best[key] = (mn, sc)
    return raw, best


# ★ 序章/教学关（`dataconfig_stage_minor.id = 31011`, `not_show = True`）。
#   真实账号必然完成过；而「增幅」按钮的解锁门槛
#   `dataconfig_module_unlock_config[1]/[4].unlock_conditions = 31011`
#   查的就是它。见 `build_stage_list` 里那段注释。
PROLOGUE_STAGE = (1, 1000, 1, 1)

# ★★★ 「初霜与春风」冒充主线（2026-10-16 用户拍板）
#
#   背景: 章节 4011/4012/4013（`dataconfig_stage_chapter.act_time_id = 80001`,
#   真 `type = 4`）在这版客户端里**没有任何入口** —— 回合活动只写死
#   Emotion/Carmen/Sleep 三条; 作战页签也是按 type 硬编码的四格
#   (`enum_level_select.ChapterTabType = {MainThird=1, Resource=2, Endless=3, FightAgain=4}`),
#   type=4 的章节只能从各自活动的 UI 进，而 初霜与春风 的活动 UI 压根没有。
#
#   ⇒ 用户拍板: **塞进「主银幕」章节轮盘**。做法是让这三章(和它们的关卡)
#     **在客户端冒充 type=1**:
#       * 客户端: `inject_cfgdump.py` 给 `dataconfig_stage_chapter` /
#         `dataconfig_stage_minor` 里 `chapter ∈ {4011,4012,4013}` 的行换 metatable,
#         让 `row.type` 返回 1（必须在 config 的 filter 之前驯服）。
#       * 服务端(**就是这里**): 1069 下发的 CSStageBase.type 也用 1，
#         并且 `_stage_minor()` 同时认 `(1,4011,m,n)` 和 `(4,4011,m,n)`。
#     两边必须一致 —— 客户端 `logic_chapter.GetLevelData` 是拿
#     `string.format("%s_%s_%s_%s", type, chapter, main, minor)` 去 StageDict 查表的，
#     key 对不上就"点进去没有关卡"。
SWORD_CHAPTERS = (4011, 4012, 4013)     # 初霜与春风 / 唐律剑馆困难 / 唐律剑馆突发
SWORD_TYPE_FROM = 4
SWORD_TYPE_TO = 1                       # 冒充的主线
# ★★★ 2026-10-16 第二版（用户实测报错后补）: **minor_id 也要一起冒充**。
#
#   `minor_id` 的真含义是"**这一章在该活动线里的第几章**"
#   （`model_turn_activity_data.ActivityChapter` 实测:
#     Carmen = {4004:1, 4005:2, 4006:3}、Sleep = {4007:1, 4008:1, 4009:2, 4010:3}）。
#   初霜与春风 同理: 4011 → 1、4012 → 2、4013 → 3。
#   而「主银幕」那条路 `view_level_ui.OpenUI` 是
#     `minor = params.minorId or 1`，且**只**对 minor==2/3 做"取不到关卡就退回 1"，
#     永远不会把 1 升到 2/3 ⇒ 4012/4013 的关卡列表**空的** ⇒ `ScrollToIndex` 越界
#     （客户端 probe.log: `view_level_ui: attempt to index a nil value` +
#       Unity「数组越界！」）⇒ 界面半开 ⇒ 退回主页点不动。
#   ⇒ 客户端把这两章的关卡谎报 minor_id=1，服务端下发也要跟（否则 StageDict 里
#     是 `1_4012_m_2`，客户端查 `1_4012_m_1` 查不到）。
SWORD_MINOR_CHAPTERS = (4012, 4013)     # 这两个的关卡真挂在 minor_id 2 / 3 下


def stage_alias_type(tp, ch):
    """「初霜与春风」的关卡类型 4 -> 1；其它原样返回。

    ★ 只认 `(4, 4011/4012/4013)` —— 情绪/Carmen/Sleep 的 type=4 章节
      （4001~4010）**不动**，它们有自己的活动 UI。
    """
    try:
        tp, ch = int(tp), int(ch)
    except Exception:
        return tp
    if tp == SWORD_TYPE_FROM and ch in SWORD_CHAPTERS:
        return SWORD_TYPE_TO
    return tp


def stage_alias_minor(tp, ch, mi):
    """「唐律剑馆困难/突发」（4012/4013）的关卡 minor_id 2/3 -> 1；其它原样返回。

    ★ 4011（初霜与春风本体）本来就是 minor_id=1，不动。
    """
    try:
        ch, mi = int(ch), int(mi)
    except Exception:
        return mi
    if ch in SWORD_MINOR_CHAPTERS and mi != 1:
        return 1
    return mi


def build_stage_list(d):
    """客户端把 StageList 当成"已解锁关卡集合 StageDict", IsLockLevel/IsLockChapter
    都是 `StageDict[key] == nil`。之前只下发玩家通关记录 => StageDict 为空 =>
    每章第一关都判为"已锁" => 章节列表在第一项就截断 => 只剩下默认章节。
    这里改成一口气下发**全部关卡**(Score=0 表示未通关), 章节/关卡就天然全开。"""
    import cfgdata
    raw, best = _stage_progress(d)
    r = b""
    seen = set()
    for row in cfgdata.load("dataconfig_stage_minor").values():
        tp, ch = row.get("type"), row.get("chapter")
        mn, mi = row.get("main_id"), row.get("minor_id")
        if not all(isinstance(v, int) for v in (tp, ch, mn, mi)):
            continue
        # ★ 初霜与春风: 下发时冒充主线（见 SWORD_CHAPTERS 的注释）
        tp = stage_alias_type(tp, ch)
        mi = stage_alias_minor(tp, ch, mi)
        k = (tp, ch, mn, mi)
        if k in seen:
            continue
        seen.add(k)
        sb = uv(1, tp) + uv(2, ch) + uv(3, mn) + uv(4, mi)   # CSStageBase
        sc = raw.get(k)
        if sc is None:
            # 这一关没记录, 但这章打到更后面了 => 视为已通关(见 _stage_progress)
            bm, bsc = best.get((tp, ch), (0, 0))
            sc = bsc if mn <= bm else 0
        # ★★★ 序章/教学关「1:1000:1:1」特殊处理（2026-10-16 定案）
        #
        #   真实账号**必然**完成过这一关（新手引导里就打了，`not_show=True` 不在关卡列表
        #   里显示）。但我们的基准档把 `stages` 清空了 ⇒ 客户端 `StageDict` 里这关
        #   `Score=0` ⇒ `logic_chapter.IsFinishLevelById(31011)` 返回 false。
        #
        #   而 `31011` 正是**「增幅」按钮的解锁门槛**:
        #       logic_func_unlock.CheckFuncUnlock(1) / (4)
        #         -> dataconfig_module_unlock_config[1 or 4].unlock_conditions = 31011
        #         -> CheckByPassLevel -> logic_chapter.IsFinishLevelById(31011)
        #   `battle_scene.CreateRoleData` 拿它当 `AddRoleItem(.., upLevelUnlock, ..)`，
        #   而 `view_battle_roulette.ShowOptionBtnState` 结尾是
        #       SetActive(Up按钮, GetCsRole(id).UpLevelUnlock == 1)
        #   ⇒ **没过这一关，整个战斗内升级按钮被隐藏**（玩家看到的"按钮消失只有轮廓"）。
        #
        #   ★ 只在这里把它按 1 星下发（**不动存档 `stages`**，玩家还是"一关都没打过"，
        #     奖励/扫荡逻辑完全不受影响）；而且这关 `not_show=True`，界面上看不见。
        if (tp, ch, mn, mi) == PROLOGUE_STAGE and sc <= 0:
            sc = 1
        # ★★ CSStageScore{1:StageBase 2:Score 3:HasChallenge 4:IsLastChallenge
        #                5:IsSweepUnlock 6:RechallengeNum}
        #   扫荡入口看 **field 5** —— 只有三星过才置 1。以前完全没发过 ⇒ 扫荡永远开不了。
        body = bs(1, sb) + uv(2, sc)     # ★ field 1 是**嵌一层**的 CSStageBase
        if sc >= STAGE_STAR_MAX:
            body += uv(5, 1)
        r += bs(1, body)                                     # CSStageScore
    # 起点 = 第一章第一关 (首页"作战"按钮显示的就是它)
    r += bs(2, uv(1, 1) + uv(2, 1000) + uv(3, 1) + uv(4, 1))
    # 全部章节一律开放
    r += build_all_chapters()
    return r


# ---------------- cs.CSStageStartRes (开战) ----------------
# CSStageStartRes { 1:StartData(CSStageStartData) }
#   CSStageStartData { 1:ServerTime(int64) 2:RoleList(repeated CSRoleData)
#                      3:BattleUID(uint64) 4:ReverseTagList(repeated Uint32Id) }
def build_stage_start(d):
    import time as _t
    sd = B.pb_v(1, int(_t.time()))
    for rid, rd in d["roles"].items():
        sd += bs(2, build_role(rid, rd))
    sd += B.pb_v(3, 1)                                        # BattleUID
    return bs(1, sd)


# ---------------- cs.CSGetCockpitRes (驾驶舱) ----------------
def build_cockpit(d):
    r = b""
    for pos, role_id in d.get("cockpit", {}).items():
        r += bs(2, uv(1, int(pos)) + uv(2, int(role_id)))   # CockpitData{Position, RoleID}
    return r


# ---------------- cs.CSGetHeroGroupRes (编队) ----------------
# CSGroupData     { 1:Name(string) 2:GroupIdx(uint32) 3:MaxMemberCount(uint32)
#                   4:MemberList(repeated GroupRoleData) 5:SkillCards(repeated Uint32Id) }
# GroupRoleData   { 1:RoleID(uint32) 2:Talen(uint32) }
# Uint32Id        { 1:ID(uint32) }
def build_hero_group(d):
    """注意: 客户端的 GroupIdx / SlotIdx 都是 **0-based**
    (反汇编 logic_role_select: ReqModifyHeroGroup(uv1-1, uv2-1, ...)),
    所以第 1 个编队的 GroupIdx 必须是 0, 否则客户端 ChangeQueueMember 匹配不到。"""
    r = b""
    for i in range(1, GROUP_COUNT + 1):
        g = d["groups"].get(str(i), {})
        cd = b""
        cd += bs(1, g.get("name", ""))
        cd += uv(2, i - 1)                                   # 0-based GroupIdx
        cd += uv(3, MAX_MEMBER)
        # 槽位 = MemberList 的下标, 所以中间的空位要占位(roleId=0), 末尾的空位裁掉
        mem = list(g.get("members", []))
        while mem and int(mem[-1][0]) == 0:
            mem.pop()
        for m in mem:
            rid, tal = int(m[0]), int(m[1] if len(m) > 1 else 0)
            cd += bs(4, uv(1, rid) + uv(2, tal))
        r += bs(1, cd)
    return r


# ---------------- 角色升级 ----------------
# 客户端把"本次要吃的材料"放在 CSRoleLevelUpReq.UseList 里, 由服务端决定升几级:
#   每级所需经验 = dataconfig_star_levelup[star, level].exp
#   材料经验     = dataconfig_itembase[itemId].args[1] * count
#   等级上限     = dataconfig_star_strengthen[star, strengthStep].max_level
#   金币消耗     = 每级消耗经验 * money_count / 100
def apply_level_up(pd, role_id, use_list):
    import cfgdata
    rd = pd["roles"].setdefault(str(role_id), {
        "level": 1, "exp": 0, "strength": 0, "potential": 0, "ability": 1, "skin": 0})
    star = cfgdata.role_star(role_id)
    step = rd.get("strength", 0) or 0
    maxlvl = cfgdata.max_role_level(star, step)

    gain = sum(cfgdata.item_exp(i) * c for i, c in use_list)
    cum = rd.get("exp", 0) + gain
    lvl = rd.get("level", 1)
    gold = 0
    while lvl < maxlvl:
        cost, mc = cfgdata.level_cost(star, lvl)
        if cost <= 0 or cum < cost:
            break
        cum -= cost
        gold += cost * mc // 100
        lvl += 1
    rd["level"] = lvl
    rd["exp"] = cum

    # ★★★ 2026-10-16 修: 以前扣的是 `actor["gold"]` —— **客户端根本不读那个字段**。
    #   证据（反汇编，不是猜）:
    #     `logic_player_data.SetUpPlayerData`（收到 `CSGameData.MoneyList` 时每条调一次）
    #     只认 5 个 Key: `ERDT_Intellect` / `ERDT_Intellect_CountDown` /
    #     `ERDT_Intellect_Recover` / `ERDT_Exp` / `ERDT_Level`
    #     ⇒ 我们发的 `gold` / `diamond` / `copper` **一个都不匹配, 全被忽略**。
    #   ⇒ 顶栏货币只来自 `ItemList`(item 1=稀因 / 2=格 / 3=因)。
    #
    #   ★★★ 2026-10-17 更正: 上面那句「客户端根本不读 gold/diamond/copper」
    #     **只对 `CSGameData.MoneyList` 这条路成立**。登录包那条是要读的 ——
    #     `lua_data_center.RefreshBaseInfo`(proto#2) 就是:
    #         M.XiYin = data.Diamond / M.Ge = data.Gold / M.Yin = data.Copper
    #     ⇒ 所以我加了「load() 里把 actor 三个字段镜像成 item 1/2/3」
    #       （见上面 _seed_currency_items 后面那段）。两边必须一致,
    #       否则读 actor 的显示路径永远停在登录那一刻的值。
    #   ★ 角色升级消耗的是**「格」(item 2)** —— `logic_role_upgrade` 里那个取消耗的
    #     函数(proto#22)整个函数体就一句 `return lua_data_center.GetGe()`。
    #   ★ 改成扣 `items["2"]` 之后, 回包里的 `ItemList`(`build_gamedata` field 6)
    #     会让顶栏当场刷新; 以前扣 `actor.gold` 玩家完全看不到, 等于白嫖升级。
    it = pd.setdefault("items", {})
    have = int(it.get("2", 0))
    charged = min(gold, have)                   # 客户端的预检已经拦过不够的情况, 这里只兜底
    it["2"] = have - charged
    for i, c in use_list:                       # 扣掉提交的材料
        k = str(i)
        it[k] = max(0, int(it.get(k, 0)) - c)
    save(pd)
    return rd, lvl, charged


# ---------------- 皮肤(更换着装) ----------------
# CSGetRoleSkinInfoRes { 1:SkinIdList(repeated Uint32Id{1:ID}) }
# CSSetRoleSkinReq { 1:RoleId 2:SkinId }
def build_skin_info(pd):
    """CSGetRoleSkinInfoRes { 1: SkinIdList(repeated Uint32Id) }（cmd 1395）

    ★★ 2026-10-16 修「衣装没解锁」：
      以前是「遍历**已拥有角色**，取 `dataconfig_role_config[role].skin_ids`」，
      但实测那个字段**是空的**（role 1001 = `{}`），而 `dataconfig_skin_config`
      客户端是**按角色懒加载**的（我们只 dump 到 4 套皮肤）
      ⇒ 结果只有 `{0}`（默认着装）⇒ 换装界面每个角色都只有一套。

      现在改成：**取 `dataconfig_skin_config` 里已知的全部 skin_id**，不再按
      「已拥有角色」过滤 —— 皮肤解锁这件事在原版就是**纯服务端说了算**，
      跟头像框一个道理（`SkinIdList` 全由服务端给，客户端只照着查配置）。

      ★ 这是一条"越用越全"的路子：以后 dump 到更多皮肤会自动跟着解锁，不用再改代码。
      ★ 想要**全部**衣装，得先让 `dataconfig_skin_config` 完整 —— 见
        `_probe_skin.py` 里的说明（客户端懒加载，或者去解 `Win\\AB` 的配置资源包）。
    """
    import cfgdata
    ids = set([0])                              # 0 = 默认着装
    try:
        ids.update(cfgdata.all_skin_ids())      # ★ 全量（目前能 dump 到的那几套）
    except Exception:
        pass
    for rid in pd["roles"]:
        try:
            ids.update(cfgdata.role_skin_ids(rid))   # 兜底：role_config 里有就用
        except Exception:
            pass
    r = b""
    for sid in sorted(ids):
        r += bs(1, uv(1, sid))
    return r


def set_role_skin(pd, role_id, skin_id):
    rd = pd["roles"].get(str(role_id))
    if rd is None:
        return False
    rd["skin"] = int(skin_id)
    save(pd)
    return True


# ---------------- 抽卡 ----------------
# CSRecruitInfoRes { 1:Pools(repeated RecruitPool) }
#   RecruitPool { 1:PoolType(enum) 2:PoolID 3:PoolName(string)
#                 4:EndTime 5:StartTime 6:Roles(repeated Uint32Id) }
# CSHeroBuyReq { 1:Time 2:PoolID }  ->  CSHeroBuyRes
#   CSHeroBuyRes { 1:RoleList(repeated RecruitRoleData) 2:ItemList(repeated CSGetItem)
#                  3:PoolID 4:GameData(CSGameData) }
#   RecruitRoleData { 1:RoleID 2:GainType(1=重复 2=首次) 3:GainAward(repeated CSItemPair) }
FAR_FUTURE = 4102444800          # 2100-01-01, 让卡池"永不过期"


# ---------------- 卡池分页 (2026-10-14) ----------------
# ★ 为什么要分页: 卡池按钮列表是 LoopScrollRect(回收式), Container 里常驻只有
#   5~6 个格子; 而客户端 ChangePool(卡池下标) 会直接取 uv7[下标] 的按钮对象 ——
#   下标超过格子数就是 nil => "attempt to index a nil value" => 海报空白。
#   实测: 第 5 个卡池以后全崩(用户确认"从第五个卡池以后开始")。
#   原版服务器只下发当前开放的少数卡池, 所以从没暴露这个缺陷。
#   方案(用户选定): 每次只发 5 个, 界面上的「换一批卡池」按钮翻页。
RECRUIT_PAGE_SIZE = 5

# ★★★ 客户端**没有的**卡池 —— 和 CLIENT_MISSING_ITEM_IDS / CLIENT_MISSING_HEAD_ICON_IDS
#     同一类问题（都是"服务端按多个客户端共用的 dump 发 id，客户端按 id 查配置"）。
#
# Steam 版客户端（steam-GL, svn 82743）的 `dataconfig_recruit_pools` **和**
# `dataconfig_recruit_ui_config` **都没有 35..40 这几行**（国服那份有）。
# 服务端照国服那份下发 ⇒ 客户端
#     "prefabs/ui/recruit_ui/" .. paint_type
# 指向的 AB **根本不在客户端包里**。Player.log 实测：
#     AB.没有加载成功：path:prefabs/ui/recruit_ui/cardpool_default_19
#                     abName:2e81aef0_888982f15f0637d9.ab
#     （该 .ab 在 Steam 和国服两份客户端里都不存在）
# ⇒ **卡池面板整块建不出来** ⇒ 而「换一批卡池」按钮**就长在面板预制体里**
# ⇒ 按钮不存在 ⇒ **卡在这一批就再也翻不动**（用户实测「换卡池与还原角色按钮都点不动」）。
#
# ★ 名单来自客户端探针 `_re\recruit_spy.log`：
#     查表兜底: dataconfig_recruit_pools(pool_id=36) 缺行 -> 空壳
#     查表兜底: dataconfig_recruit_ui_config(pool_id=36) 缺行 -> 空壳
#   （35..40 都出现过，491 条记录）
CLIENT_MISSING_POOL_IDS = frozenset({35, 36, 37, 38, 39, 40})


def _recruit_pools():
    """**剔掉客户端没有的卡池**之后的列表 —— 下发和分页都必须走它。

    ★ 不能只在下发时过滤：`recruit_page_count()` 也要用同一个列表，
      否则翻页会翻出空页。
    """
    import cfgdata
    return [p for p in cfgdata.recruit_pools()
            if int(p[0]) not in CLIENT_MISSING_POOL_IDS]


def recruit_page(d):
    return max(0, int(d.get("recruit_page") or 0))


def recruit_page_count():
    n = len(_recruit_pools())
    return max(1, (n + RECRUIT_PAGE_SIZE - 1) // RECRUIT_PAGE_SIZE)


def recruit_page_next(d):
    d["recruit_page"] = (recruit_page(d) + 1) % recruit_page_count()
    save(d)
    return d["recruit_page"]


def build_recruit_info(d=None):
    pools = _recruit_pools()
    page = recruit_page(d) if isinstance(d, dict) else 0
    lo = page * RECRUIT_PAGE_SIZE
    sel = pools[lo:lo + RECRUIT_PAGE_SIZE]
    if not sel:
        sel = pools[:RECRUIT_PAGE_SIZE]
    r = b""
    for pid, ptype, pname, ups, _one, _ten in sel:
        rp = uv(1, ptype) + uv(2, pid) + bs(3, pname)
        rp += uv(4, FAR_FUTURE) + uv(5, 1)
        for rid in ups:
            rp += bs(6, uv(1, rid))
        r += bs(1, rp)
    return r


# ======================================================================
#  角色获得 / 舍友合同 / 抽卡重复补偿        2026-10-15
# ======================================================================
# dataconfig_itembase 里 2001~2065 = 「XX舍友合同」: func=5, args={1: roleId},
# **use_way=2 (AutoUse)**。
#
# ★★ 为什么必须在**服务端**自动兑换:
#   客户端 view_item_use.lua 只对 `use_way == 1` 显示「使用」按钮
#   (反汇编: `TGETS Config.use_way; ISNEN 1 -> 整块 SetActive(false)`),
#   而且它按 func 分派的分支只有 9/10/11。所以舍友合同**永远走不到 1050**,
#   在玩家眼里就是"无用的死道具"。
#   正确处理是**发放时自动转换**:
#       该角色未拥有 -> 直接给角色本身(合同不落背包)
#       已拥有       -> 按 repeat_gain 给补偿(前 5 次重复各给 1 个
#                      该角色的专属补充协议 3001~3065)
#
#   挂点选在 `load()` 的归一化里 —— 这样**所有来源**全覆盖: 基准档、开箱、
#   邮件、商店、任务奖励。任何进到背包的舍友合同都会被兑掉。
ITEM_FUNC_CONTRACT = 5
# ★ 月卡礼包(item 4029, 全局唯一一个 func==8)。args[1] = 天数。
#   `use_way=2` + `bag_not_show=1` —— 它是**发放即开掉**的礼包，不该躺在背包里。
ITEM_FUNC_MONTH_CARD = 8

_ROLE_CFG_CACHE = None
_CONTRACT_CACHE = None
_POTENTIAL_CACHE = None


def _role_cfg():
    """{role_id: dataconfig_role_config 行}"""
    global _ROLE_CFG_CACHE
    if _ROLE_CFG_CACHE is None:
        import cfgdata
        m = {}
        for r in cfgdata.load("dataconfig_role_config").values():
            if isinstance(r, dict) and isinstance(r.get("id"), int):
                m[int(r["id"])] = r
        _ROLE_CFG_CACHE = m
    return _ROLE_CFG_CACHE


def _pair_list(dct):
    """{1:{item_id,item_count}, ...} -> [(item_id, count), ...]"""
    out = []
    if not isinstance(dct, dict):
        return out
    for k in sorted(dct, key=lambda x: int(x) if str(x).isdigit() else 0):
        e = dct[k]
        if not isinstance(e, dict):
            continue
        i = e.get("item_id")
        n = e.get("item_count")
        if isinstance(i, int):
            out.append((int(i), int(n) if isinstance(n, int) else 1))
    return out


def _contract_role_map():
    """{合同道具id: roleId}"""
    global _CONTRACT_CACHE
    if _CONTRACT_CACHE is None:
        m = {}
        for r in _item_by_id().values():
            if int(r.get("func") or 0) != ITEM_FUNC_CONTRACT:
                continue
            a = r.get("args") if isinstance(r.get("args"), dict) else {}
            v = a.get(1, a.get("1"))
            if isinstance(v, int):
                m[int(r["id"])] = int(v)
        _CONTRACT_CACHE = m
    return _CONTRACT_CACHE


# ★ 补充协议道具的 `func` = 7, `args[1]` = 目标角色 id
#   (实测: 3051 露希尼补充协议 func=7 args=[1055]) —— 和舍友合同(func=5)一个套路。
ITEM_FUNC_PROTOCOL = 7

_PROTO_ROLE_CACHE = None


def _protocol_role_item_map():
    """{roleId: 补充协议道具id}"""
    global _PROTO_ROLE_CACHE
    if _PROTO_ROLE_CACHE is None:
        m = {}
        for r in _item_by_id().values():
            if int(r.get("func") or 0) != ITEM_FUNC_PROTOCOL:
                continue
            a = r.get("args") if isinstance(r.get("args"), dict) else {}
            v = a.get(1, a.get("1"))
            if isinstance(v, int):
                m[int(v)] = int(r["id"])
        _PROTO_ROLE_CACHE = m
    return _PROTO_ROLE_CACHE


def _potential_row(role_id, step):
    """dataconfig_role_potential 里 (id=role_id, step) 的那一行。"""
    global _POTENTIAL_CACHE
    if _POTENTIAL_CACHE is None:
        import cfgdata
        m = {}
        for r in cfgdata.load("dataconfig_role_potential").values():
            if isinstance(r, dict):
                m[(r.get("id"), r.get("step"))] = r
        _POTENTIAL_CACHE = m
    return _POTENTIAL_CACHE.get((int(role_id), int(step)))


def potential_max_steps(role_id):
    """这个角色的因序链一共几步(配置里 5 步)。"""
    n = 0
    for s in range(1, 32):
        if _potential_row(role_id, s) is None:
            break
        n = s
    return n


def _repeat_awards(role_id, gain_time):
    """按 dataconfig_role_config[role].repeat_gain 取该 gain_time 档的奖励。

    repeat_gain 形如:
      {"1": {gain_time_begin:0, gain_time_end:0, award_item:{1:{item_id:35,item_count:1}}},
       "2": {gain_time_begin:1, gain_time_end:5, award_item:{1:{item_id:3001,item_count:1},
                                                            2:{item_id:35,item_count:4}}},
       "3": {gain_time_begin:6, gain_time_end:999999999, award_item:{...}}}
    """
    rg = (_role_cfg().get(int(role_id)) or {}).get("repeat_gain")
    if not isinstance(rg, dict):
        return []
    for k in sorted(rg, key=lambda x: int(x) if str(x).isdigit() else 0):
        e = rg[k]
        if not isinstance(e, dict):
            continue
        lo = int(e.get("gain_time_begin") or 0)
        hi = int(e.get("gain_time_end") or 0)
        if lo <= gain_time <= hi:
            return _pair_list(e.get("award_item"))
    return []


def _obtain_role(d, role_id):
    """获得一个角色。返回 (是否首次, [(item_id,count)] 补偿)。

    role_gain_count[roleId] = 累计获得次数; 本次 gain_time = 该计数(0-based)。
    所以第 2 次获得时 gain_time=1 -> 命中 repeat_gain 第 2 档(发专属补充协议)。
    """
    rid = int(role_id)
    k = str(rid)
    roles = d.setdefault("roles", {})
    gc = d.setdefault("role_gain_count", {})
    gain_time = int(gc.get(k, 0))
    first = k not in roles
    if first:
        roles[k] = {"level": 1, "exp": 0, "strength": 0,
                    "potential": 0, "ability": 1, "skin": 0}
    gc[k] = gain_time + 1
    awards = _repeat_awards(rid, gain_time)
    it = d.setdefault("items", {})
    for i, n in awards:
        it[str(i)] = int(it.get(str(i), 0)) + int(n)
    return first, awards


# ★ 残留合同的清理结果要**推给客户端**, 否则背包界面上那张合同一直挂着
#   (服务端删了, 客户端不知道)。bizserver 在每个请求 load() 之后来取一次。
_PENDING_ITEM_PUSH = []


def consume_pending_item_push():
    """取出并清空"待推送的道具 id"列表。"""
    global _PENDING_ITEM_PUSH
    if not _PENDING_ITEM_PUSH:
        return []
    out = _PENDING_ITEM_PUSH
    _PENDING_ITEM_PUSH = []
    return out


# ★★ 2026-10-16 用户要求: 舍友合同换成**新角色**时要弹「获得角色」, 跟抽卡一样。
#   客户端 `logic_role_data.UpdateRoleData` **只在收到 1213 CSMultiRoleNotify 时才会跑**
#   (logic_role.OnMultiRoleNotify -> SetMultiRoleDate -> UpdateRoleData),
#   而弹窗本体是 `logic_show_role_or_skin.TryShowView({roleData={RoleID=id}})`。
#   ⇒ 服务端拿到新角色时必须**主动推 1213**。
#   ★ 抽卡不走这里(draw/build_hero_buy 直接调 _obtain_role, 自己带 RecruitRoleData 弹窗),
#     所以不会重复弹。
_PENDING_ROLE_PUSH = []


def consume_pending_role_push():
    """取出并清空"待推送(新获得)的角色 id"列表。"""
    global _PENDING_ROLE_PUSH
    if not _PENDING_ROLE_PUSH:
        return []
    out = _PENDING_ROLE_PUSH
    _PENDING_ROLE_PUSH = []
    return out


def _note_new_role(rid):
    """记下"这个角色是刚拿到的", 调用方之后会推 1213 让客户端弹窗。"""
    rid = int(rid)
    if rid not in _PENDING_ROLE_PUSH:
        _PENDING_ROLE_PUSH.append(rid)


def _auto_use_contracts(d):
    """把背包里所有「舍友合同」(func=5, AutoUse) 自动兑换掉。

    ★ 角色不进 items —— 客户端通过 CSGameData 的 RoleList 看到它。
    返回 item 形式的补偿列表(给 1052 推送用)。
    ★★ 这已经是**兜底**了: 新发放走 `_grant_items`, 当场就兑、合同不进背包。
       这里负责清掉老存档里已经进去的那些, 并把 id 记进 `_PENDING_ITEM_PUSH`
       让调用方推一条 1052(告诉客户端"这格没了")。
    """
    global _PENDING_ITEM_PUSH
    it = d.get("items") or {}
    cmap = _contract_role_map()
    if not cmap or not it:
        return []
    out, changed = [], False
    for iid_s in list(it.keys()):
        try:
            iid = int(iid_s)
        except Exception:
            continue
        rid = cmap.get(iid)
        if rid is None:
            continue
        n = int(it.get(iid_s) or 0)
        if n <= 0:
            continue
        for _ in range(n):
            _first, awards = _obtain_role(d, rid)
            out.extend(awards)
            if _first:
                _note_new_role(rid)      # ★ 新角色 -> 调用方推 1213 弹窗
        del it[iid_s]                    # 合同本身不落背包
        if iid not in _PENDING_ITEM_PUSH:
            _PENDING_ITEM_PUSH.append(iid)
        changed = True
    if changed:
        save(d)
    return out


# ---------------------------------------------------------------------------
# ★★ 2026-10-16 「舾装补给」这类开箱道具(func=4) —— **故意不做任何自动开启**。
#
#   背景（查「箱子只有详情、打不开」时查清的）:
#     * 客户端**不显示「使用」按钮**，因为这类道具 `use_way=2`（"发放即使用"类型）。
#       对照: 私域通讯 `use_way=1` → 有按钮。
#     * 所以它天生就不是给玩家手动点的。原版是靠**发放时那一刻自动开**。
#
#   但用户 2026-10-16 拍板: **不要自动开** ——
#     这批是**内测纪念品**，让它们**原样发进背包留着**就好
#     （基准档照发，见 `_fix_baseline_roles.py` 的"道具清单"那条）。
#   ⇒ 因此 `_add_items` 里**刻意没有** func=4 的分支，`load()` 也不清它们。
#     `use_items()` 里那条 func=4 分支保留着 —— 万一将来有别的途径用到了，
#     它能按 `dataconfig_item_drop` 正确开出来（含嵌套子包）。
# ---------------------------------------------------------------------------


def build_role_potential_res(d, role_id, level):
    """CSRolePotentialRes{1:ID 2:PotentialLevel 3:GameData}"""
    return (uv(1, int(role_id)) + uv(2, int(level))
            + bs(3, build_gamedata(d)))


def role_potential_up(d, role_id, is_common):
    """因序链(潜能)升一级。返回 (新的 PotentialLevel, 错误码, 消耗列表)。

    ★ 补充协议 (3001~3065 专属 / 68 通用) 是 use_way=0 (CanNotUse),
      **只能被这条命令消耗** —— 客户端不给它们「使用」按钮。
      isCommon 决定用哪套材料: 真 -> general_cost(通用68 x3),
                                假 -> exclusive_cost(专属 x1)。
    """
    rid = int(role_id)
    rk = str(rid)
    roles = d.get("roles") or {}
    if rk not in roles:
        return 0, RET_ITEM_NOT_ENOUGH, []
    cur = int(roles[rk].get("potential") or 0)
    if cur >= potential_max_steps(rid):
        return cur, RET_ITEM_NOT_ENOUGH, []
    row = _potential_row(rid, cur + 1)
    if row is None:
        return cur, RET_ITEM_NOT_ENOUGH, []
    need = _pair_list(row.get("general_cost" if int(is_common) else "exclusive_cost"))
    if not need:
        return cur, RET_ITEM_NOT_ENOUGH, []
    it = d.setdefault("items", {})
    for i, n in need:
        if int(it.get(str(i), 0)) < n:
            return cur, RET_ITEM_NOT_ENOUGH, []
    for i, n in need:
        it[str(i)] = int(it.get(str(i), 0)) - n
    roles[rk]["potential"] = cur + 1
    save(d)
    return cur + 1, 0, need


# ======================================================================
#  角色养成: 突破 / 能力 / 天赋 / 默认天赋 / 图鉴      2026-10-15
# ======================================================================
# 命令 (protocol_generated\cs_role_pb.lua):
#   1018 CSRoleStrengthReq{1:ID}                 -> 1019 {1:ID 2:StrengthLevel 3:GameData}
#   1244 CSRoleStrengthenAwardReq{1:RoleId 2:StrengthenLevel}
#                                                -> 1245 {1:ItemList 2:GameData}
#   1242 CSRoleAbilityLevelUpReq{1:RoleId}       -> 1243 {1:GameData}
#   1030 CSRoleTalenUnlockReq{1:RoleId 2:Rank}   -> 1031 {1:GameData}
#   1211 CSRoleDefaultTalenReq{1:RoleId 2:Rank}  -> 1212 (空)
#   1265 CSRoleHandbookAwardReq{1:RoleId}        -> 1266 {1:ItemList 2:GameData}
#
# ★★ 客户端事实 (反汇编 view_role_up_step_ui / view_role_up_ability_ui /
#    view_role_up_rank_ui / logic_role_atlas / logic_role_orig):
#   1. 这 6 个响应处理器**形状完全一样**: 只读 `GameData` 字段 ->
#      game_data.AnalysisGameData() 统一入账。所以服务端只要把改动塞进
#      CSGameData{RoleList, ItemList, MoneyList} 就够, 不用逐字段回。
#   2. 扣什么由**客户端**从配置表读并自己先校验: 不够就只会弹一句提示
#      (JinShengItemNotEnough / LackOfGe), **请求根本不发**。
#      => 服务端必须照**同一张表**扣, 否则两边不一致 => 请求被拒 => 玩家卡死。
#   3. 突破成功客户端会 dispatchCmd(ROLE_PROMOTE, ID) 刷界面;
#      突破奖励 / 图鉴奖励会用 ItemList 弹「获得物品」窗。
#   4. 到顶时按钮回调直接 return: 能力 AbilityLevel==4、天赋 step==4、突破满档。
#
# ★★ 货币: item id **1=稀因 / 2=格 / 3=因**。
#
# 【2026-10-15 实证 —— 别再去动 actor】
#   客户端 `logic/store_ui/logic_store.UpdateStoreData`(proto#13) 收到 1052 后,
#   遍历 `ItemNotify.ItemList`, 按 id 分流:
#       id == EMT_Diamond(1) -> lua_data_center.SetXiYin(Count)
#       id == EMT_Copper (2) -> lua_data_center.SetGe  (Count)      <= 格
#       id == EMT_Gold   (3) -> lua_data_center.SetYin (Count)      <= 因
#   (枚举值 EMAIL 自 `cs_type_pb.lua`: EMT_Init=0/EMT_Diamond=1/EMT_Copper=2/EMT_Gold=3。
#    ★ 名字很反直觉: **Copper 是格、Gold 是因**, 按名字猜必翻车。)
#   ⇒ **货币的权威值就是背包里的这三件道具**, 客户端自己反推 lua_data_center。
#     `build_actor` 的 diamond/gold/copper 三个字段**不是**货币源头。
#   ⇒ 所以这里**只碰 items**, 不去写 actor(以前写 actor.gold 当"格"是错的)。
CURRENCY_ITEM_IDS = (1, 2, 3)          # 稀因 / 格 / 因


def _have_item(d, iid):
    """手上某种道具的数量。货币也走背包 —— 见上面 CURRENCY_ITEM_IDS 的注释。"""
    return int((d.get("items") or {}).get(str(int(iid)), 0) or 0)


def _pay_items(d, need):
    """扣 need=[(item_id,count)]。不够 -> 一点不扣并返回 False。"""
    for i, n in need:
        if _have_item(d, i) < int(n):
            return False
    it = d.setdefault("items", {})
    for i, n in need:
        it[str(int(i))] = max(0, int(it.get(str(int(i)), 0)) - int(n))
    return True


# ---------------- 突破 (1018) ----------------
def role_strength_up(d, role_id):
    """突破一档。消耗 = dataconfig_role_strengthen[id, StrengthLevel+1].cost_item_list。

    ★ step 0 是初始态(cost 为空), 真正花钱的是 step >= 1 ——
      客户端 SureUpBtnClick 读的正是 `StrengthLevel + 1` 那一行。
    返回 (新的 StrengthLevel, 错误码, 消耗列表)。
    """
    import cfgdata
    rid = int(role_id)
    rk = str(rid)
    roles = d.get("roles") or {}
    if rk not in roles:
        return 0, RET_ITEM_NOT_ENOUGH, []
    cur = int(roles[rk].get("strength") or 0)
    if cur >= cfgdata.max_role_step(rid):
        return cur, RET_ITEM_NOT_ENOUGH, []
    row = cfgdata.strengthen_row(rid, cur + 1)
    if row is None:
        return cur, RET_ITEM_NOT_ENOUGH, []
    need = _pair_list(row.get("cost_item_list"))
    if not need or not _pay_items(d, need):
        return cur, RET_ITEM_NOT_ENOUGH, []
    roles[rk]["strength"] = cur + 1
    save(d)
    return cur + 1, 0, need


# ---------------- 突破奖励 (1244) ----------------
def role_strengthen_award(d, role_id, step):
    """领某一突破档的奖励 = dataconfig_role_strengthen[id, step].award_item_list。

    ★ CSRoleData.StrengthAward (字段 13, repeated Uint32Id) = "已领过的档位",
      客户端 logic_role_data.IsCanGetStepAward 拿它跟 StrengthLevel 比来算红点。
    返回 ([(item_id,count)], 错误码)。
    """
    import cfgdata
    rid, st = int(role_id), int(step)
    rk = str(rid)
    roles = d.get("roles") or {}
    if rk not in roles:
        return [], RET_ITEM_NOT_ENOUGH
    got = [int(x) for x in (roles[rk].get("strength_award") or [])]
    if st in got:
        return [], RET_ITEM_NOT_ENOUGH          # 已经领过
    if int(roles[rk].get("strength") or 0) < st:
        return [], RET_ITEM_NOT_ENOUGH          # 还没突破到那一档
    row = cfgdata.strengthen_row(rid, st)
    if row is None:
        return [], RET_ITEM_NOT_ENOUGH
    gain = _grant_items(d, row.get("award_item_list"))
    got.append(st)
    got.sort()
    roles[rk]["strength_award"] = got
    save(d)
    return gain, 0


# ---------------- 能力升级 (1242) ----------------
def role_ability_up(d, role_id):
    """能力升级。消耗 = dataconfig_ab_skill_levelup[id, AbilityLevel].cost_item_list。

    ★ 客户端 AbilityLevelBtnClick 读的是 `当前 AbilityLevel` 那一行(不是 +1),
      且 `AbilityLevel == 4` 时直接 return => 上限 4, 新角色从 1 开始。
    返回 (新的 AbilityLevel, 错误码, 消耗列表)。
    """
    import cfgdata
    rid = int(role_id)
    rk = str(rid)
    roles = d.get("roles") or {}
    if rk not in roles:
        return 1, RET_ITEM_NOT_ENOUGH, []
    cur = int(roles[rk].get("ability") or 1)
    if cur >= cfgdata.max_ability_level():
        return cur, RET_ITEM_NOT_ENOUGH, []
    row = cfgdata.ability_row(rid, cur)
    if row is None:
        return cur, RET_ITEM_NOT_ENOUGH, []
    need = _pair_list(row.get("cost_item_list"))
    if not need or not _pay_items(d, need):
        return cur, RET_ITEM_NOT_ENOUGH, []
    roles[rk]["ability"] = cur + 1
    save(d)
    return cur + 1, 0, need


# ---------------- 天赋升阶 (1030) ----------------
def role_talen_unlock(d, role_id, rank):
    """天赋升一步。消耗 = dataconfig_role_talen_levelup[id, rank, step].cost_item_list。

    ★ 存档 talent_steps[rank] = 当前步数, 默认 1 (build_role 下发字段 6 用的就是它)。
    ★ 那张表 2026-10-15 才补进 cfgdump 的 PROACTIVE, 键是 0 基还是 1 基当时没验过
      —— 查不到"当前步数"就退一步试"当前步数 + 1"。都没有就返回失败(界面表现为
      "暂不可升"), **不要崩**。
    返回 (新的 step, 错误码, 消耗列表)。
    """
    import cfgdata
    rid, rrank = int(role_id), int(rank)
    rk = str(rid)
    roles = d.get("roles") or {}
    if rk not in roles:
        return 0, RET_ITEM_NOT_ENOUGH, []
    steps = roles[rk].setdefault("talent_steps", {})
    cur = int(steps.get(str(rrank), 1))
    if cur >= cfgdata.max_talen_step(rid, rrank):
        return cur, RET_ITEM_NOT_ENOUGH, []
    row = (cfgdata.talen_levelup_row(rid, rrank, cur)
           or cfgdata.talen_levelup_row(rid, rrank, cur + 1))
    if row is None:
        return cur, RET_ITEM_NOT_ENOUGH, []
    need = _pair_list(row.get("cost_item_list"))
    if not need or not _pay_items(d, need):
        return cur, RET_ITEM_NOT_ENOUGH, []
    steps[str(rrank)] = cur + 1
    save(d)
    return cur + 1, 0, need


# ---------------- 默认天赋 (1211) ----------------
def role_default_talen(d, role_id, rank):
    """设置角色默认天赋阶位 -> CSRoleData 字段 9 DefaultTalen。返回错误码。"""
    rk = str(int(role_id))
    roles = d.get("roles") or {}
    if rk not in roles:
        return RET_ITEM_NOT_ENOUGH
    roles[rk]["default_talen"] = int(rank)
    save(d)
    return 0


# ---------------- 图鉴收录奖励 (1265) ----------------
# 客户端 (logic_role_atlas) 只用 CSRoleData.HandbookAward (字段 15, bool) 算红点:
#   HandbookAward == false -> 有红点; 领完置 true。
# ★ 收录奖励**没有专门的配置表** —— 翻遍 cfg 目录也没有"图鉴奖励"那张表,
#   所以这里给一份固定奖励(因 x50)。要改就改这个常量。
HANDBOOK_AWARD = [(3, 50)]


def role_handbook_award(d, role_id):
    """图鉴收录奖励。返回 ([(item_id,count)], 错误码)。"""
    rk = str(int(role_id))
    roles = d.get("roles") or {}
    if rk not in roles:
        return [], RET_ITEM_NOT_ENOUGH
    if roles[rk].get("handbook_award"):
        return [], RET_ITEM_NOT_ENOUGH
    gain = _grant_pairs(d, HANDBOOK_AWARD)
    roles[rk]["handbook_award"] = True
    save(d)
    return gain, 0


# ---------------- 响应构造 ----------------
def build_role_strength_res(d, role_id, step):
    """CSRoleStrengthRes{1:ID 2:StrengthLevel 3:GameData}"""
    return (uv(1, int(role_id)) + uv(2, int(step))
            + bs(3, build_gamedata(d)))


def build_role_gamedata_res(d):
    """{1:GameData} —— 能力升级 1243 / 天赋升阶 1031 用这个形状。"""
    return bs(1, build_gamedata(d))


def build_role_item_award_res(d, gain):
    """CSRoleStrengthenAwardRes / CSRoleHandbookAwardRes
       {1:ItemList(repeated CSItemPair{1:ID 2:Count}) 2:GameData}"""
    r = b""
    for i, n in gain:
        r += bs(1, uv(1, int(i)) + uv(2, int(n)))
    r += bs(2, build_gamedata(d))
    return r


# ======================================================================
#  烹饪 / 百馐餐区        2026-10-15 (P1)
# ======================================================================
# 1207 CSGetLearnedFoodsReq(空) -> 1208 CSGetLearnedFoodsRes{1: FoodId(repeated Uint32Id)}
# 1166 CSCookingReq{1:FoodID 2:RoleID 3:CookTimes} -> 1167 CSCookingRes
#        {1:SpecialCook 2:SpecialItem(repeated CSItemPair) 3:FoodID 4:FoodNums}
# 1209 CSLearnFoodNotify{1:FoodId}   —— 服务端主动推(学会菜谱), 客户端无对应请求
#
# dataconfig_cook: {food_id, food_level, cost_item_list{...}, chef, chef_func}
#   **food_id 就是那道菜的道具 id** (itembase type=3 sub_type=4), 共 16 条。
#
# ★★ 为什么必须服务端做:
#   登录时 enter_necessary_infos 就发 1207。不回 1208 => 客户端"已学会菜谱"
#   表恒空 => GetUnlockMenu() 返回空 => **菜单是空的, 根本选不了菜** =>
#   突破要的食物永远产出不了 => 整个养成卡死。
#   (客户端进烹饪界面本身**不发任何请求**, 只读本地缓存, 所以卡点在登录那一次。)
COOK_SPECIAL_NONE = 0        # EnmSpecialCook: 0=None 1=返还材料 2=额外产出 3=特殊产出

_COOK_CACHE = None


def _cook_rows():
    """{food_id: 配方行}"""
    global _COOK_CACHE
    if _COOK_CACHE is None:
        import cfgdata
        m = {}
        for r in cfgdata.load("dataconfig_cook").values():
            if isinstance(r, dict) and isinstance(r.get("food_id"), int):
                m[int(r["food_id"])] = r
        _COOK_CACHE = m
    return _COOK_CACHE


def learned_foods(d=None):
    """已学会的菜谱 id 列表。★ 假服务端**全解锁** —— 否则菜单恒空。"""
    return sorted(_cook_rows())


def build_learned_foods_res(d=None):
    """CSGetLearnedFoodsRes{1: FoodId} —— ★ 元素是包一层的 Uint32Id{1:ID}。"""
    r = b""
    for fid in learned_foods(d):
        r += bs(1, uv(1, int(fid)))
    return r


def cook(d, food_id, role_id=0, times=1):
    """做菜。返回 (body, gain, spent, err)。err==0 表示成功。

    消耗 dataconfig_cook[food_id].cost_item_list 里的食材, 产出 food_id 本身。
    SpecialCook 固定 0(无额外产出) —— 原服的"特殊产出"概率无从考证,
    先按最朴素的方式实现; 以后要加就按 chef_func 掷骰子。
    """
    fid = int(food_id)
    times = max(1, int(times or 1))
    row = _cook_rows().get(fid)
    if row is None:
        return b"", [], [], RET_ITEM_NOT_ENOUGH
    cost = _pair_list(row.get("cost_item_list"))
    it = d.setdefault("items", {})
    for i, n in cost:
        if int(it.get(str(i), 0)) < n * times:
            return b"", [], [], RET_ITEM_NOT_ENOUGH
    for i, n in cost:
        it[str(i)] = int(it.get(str(i), 0)) - n * times
    it[str(fid)] = int(it.get(str(fid), 0)) + times
    try:
        bump_daily(d, "cook")          # 每日任务 50019「在百馐餐区烹饪1次」
    except Exception:
        pass
    save(d)
    body = uv(1, COOK_SPECIAL_NONE) + uv(3, fid) + uv(4, times)
    return body, [(fid, times)], [(i, n * times) for i, n in cost], 0


# ======================================================================
#  角色认可度 / 送礼        2026-10-15 (P2)
# ======================================================================
# 1152 CSGiftToRoleReq{1:RoleID 2:GiftID} -> 1153 CSGiftToRoleRes{1:GiftLevel 2:GameData}
# 1214 CSGiftToPlayerNotify{1:GiftList(repeated CSItemPair)}  —— 可选, 客户端只转发事件
#
# ★★ 「礼物列表」**没有独立协议**: 客户端 logic_store.GetAllGift() 是从**背包**里
#    按 itembase `type==5 and sub_type==1` 过滤出来的。所以"礼物信息不全"的实质
#    是**背包里没有礼物道具** —— 认可度礼物 12010~12100 商店不卖、无掉落、无邮件,
#    正常渠道一件都拿不到 => 列表恒空 => 显示「无礼物可送」。
#    ⇒ 已把这 10 件加进基准档(_make_baseline.py)。
#
# ★ 数值来源:
#   · 每日送礼上限 = dataconfig_dict[110].int_param (=10)
#   · 偏好档位     = dataconfig_gift_like[roleId].args[1..3] = {gift_level 1/2/3, gift_ids[]}
#   · 升级阈值     = dataconfig_role_recognized[level].recognized_exp (200/500/1000/5000)
#   · **送一次加多少认可度: 没有配置表** —— 原服硬编码, 已停无从考证。下面的
#     GIFT_FAVOR 是我们定的(偏好越高加得越多)。要调就改这三个数。
GIFT_ITEM_TYPE = 5
GIFT_ITEM_SUB_TYPE = 1
GIFT_FAVOR = {1: 100, 2: 60, 3: 30}     # 按 gift_like 的 gift_level(1 = 最喜欢)
GIFT_FAVOR_OTHER = 10                   # 不在偏好列表里的礼物

_GIFT_IDS_CACHE = None
_GIFT_LIKE_CACHE = None
_GIFT_LIMIT_CACHE = None


def _gift_ids():
    """itembase 里 type==5 && sub_type==1 的道具 id 集合(= 客户端认的"礼物")。"""
    global _GIFT_IDS_CACHE
    if _GIFT_IDS_CACHE is None:
        s = set()
        for r in _item_by_id().values():
            if int(r.get("type") or 0) == GIFT_ITEM_TYPE \
               and int(r.get("sub_type") or 0) == GIFT_ITEM_SUB_TYPE:
                s.add(int(r["id"]))
        _GIFT_IDS_CACHE = s
    return _GIFT_IDS_CACHE


def _gift_like():
    """{role_id: {gift_id: gift_level}}"""
    global _GIFT_LIKE_CACHE
    if _GIFT_LIKE_CACHE is None:
        import cfgdata
        m = {}
        for r in cfgdata.load("dataconfig_gift_like").values():
            if not isinstance(r, dict) or not isinstance(r.get("id"), int):
                continue
            per = {}
            args = r.get("args") if isinstance(r.get("args"), dict) else {}
            for k in sorted(args, key=lambda x: int(x) if str(x).isdigit() else 0):
                e = args[k]
                if not isinstance(e, dict):
                    continue
                lv = e.get("gift_level")
                for kk in sorted((e.get("gift_ids") or {}),
                                 key=lambda x: int(x) if str(x).isdigit() else 0):
                    gid = (e.get("gift_ids") or {})[kk]
                    if isinstance(gid, int) and isinstance(lv, int):
                        per[int(gid)] = int(lv)
            m[int(r["id"])] = per
        _GIFT_LIKE_CACHE = m
    return _GIFT_LIKE_CACHE


def gift_daily_limit():
    """每日送礼上限(dataconfig_dict[110]), 读不到就给 10。"""
    global _GIFT_LIMIT_CACHE
    if _GIFT_LIMIT_CACHE is None:
        n = 10
        try:
            import cfgdata
            for r in cfgdata.load("dataconfig_dict").values():
                if isinstance(r, dict) and r.get("id") == 110:
                    v = r.get("int_param")
                    if isinstance(v, int) and v > 0:
                        n = v
                    break
        except Exception:
            pass
        _GIFT_LIMIT_CACHE = n
    return _GIFT_LIMIT_CACHE


def _reset_gift_daily(d):
    """跨天就把所有角色的今日送礼次数清零。"""
    today = _today_str()
    if d.get("gift_date") == today:
        return
    d["gift_date"] = today
    for rd in (d.get("roles") or {}).values():
        if isinstance(rd, dict):
            rd["daily_gift_times"] = 0
    save(d)


def gift_level_of(role_id, gift_id):
    """这份礼物对该角色是第几档偏好(1 最喜欢)。不在列表里给 0。"""
    return int(_gift_like().get(int(role_id), {}).get(int(gift_id), 0))


def gift_to_role(d, role_id, gift_id):
    """送一份礼物。返回 (gift_level, 加的认可度, 错误码)。错误码 0 = 成功。"""
    rid, gid = int(role_id), int(gift_id)
    rk = str(rid)
    roles = d.get("roles") or {}
    if rk not in roles:
        return 0, 0, RET_ITEM_NOT_ENOUGH
    if gid not in _gift_ids():
        return 0, 0, RET_ITEM_NOT_ENOUGH
    _reset_gift_daily(d)
    rd = roles[rk]
    if int(rd.get("daily_gift_times") or 0) >= gift_daily_limit():
        return 0, 0, RET_ITEM_NOT_ENOUGH          # 今日次数用完
    it = d.setdefault("items", {})
    if int(it.get(str(gid), 0)) <= 0:
        return 0, 0, RET_ITEM_NOT_ENOUGH          # 背包里没有
    lv = gift_level_of(rid, gid)
    delta = int(GIFT_FAVOR.get(lv, GIFT_FAVOR_OTHER))
    it[str(gid)] = int(it.get(str(gid), 0)) - 1
    rd["friendship"] = int(rd.get("friendship") or 0) + delta
    rd["daily_gift_times"] = int(rd.get("daily_gift_times") or 0) + 1
    save(d)
    return lv, delta, 0


def build_gift_res(d, role_id, level):
    """CSGiftToRoleRes{1:GiftLevel 2:GameData}"""
    return uv(1, int(level)) + bs(2, build_gamedata(d))


def build_gift_notify(d, gain):
    """CSGiftToPlayerNotify{1:GiftList(repeated CSItemPair)}"""
    r = b""
    for i, n in (gain or []):
        r += bs(1, uv(1, int(i)) + uv(2, int(n)))
    return r


# ======================================================================
#  舍友宿舍 / 公寓房间        2026-10-15 (P3)
# ======================================================================
# 1180 CSGetAllRoomDataReq(空) -> 1181 CSGetAllRoomDataRes
#        {1:FloorList(repeated FloorDataBase{1:FloorIdx 2:RoomList(repeated RoomDataBase)})
#         2:UnlockFloor(repeated Uint32Id)}
#      RoomDataBase{1:HostRoleID 2:MoveInTime(已入住秒数) 3:RoomIdx}
# 1172 CSAssignApartmentRoomReq{1:HostRoleID 2:FloorIdx 3:RoomIdx} -> 1173 空
# 1178 CSGetFloorRoomDataReq{1:FloorIdx} -> 1179   ★ 客户端**完全没用这条**(只在 pb 里定义)
# 1176 CSAskIntoRoomReq{1:FloorIdx 2:RoomIdx} -> 1177
#        {1:HostRoleID 2:MoveInTime 3:GiftList(repeated CSItemPair)}
# 1195 CSUnlockFloorNotify{1:UnlockFloor}   服务端推
#
# ★★ 关键事实(全部反汇编核实):
#   · **每层 10 个房间**, RoomIdx **1..10** —— 由走廊 prefab 的门数决定
#     (view_role_room_ui proto#21 有 `if RoomIdx == 10 then 十位画1` 的特判;
#      落点 RolePos1..5 x2)。**RoomIdx=0 是"在走廊"的特殊值**。
#   · 服务端**必须发 UnlockFloor**, 否则 view_select_floor_ui 点哪层都弹「未开启」。
#   · 空房**不要**放进 RoomList(放了会被当成有人住); 房间数少于 10 完全不崩。
#   · ★ 绝不能给 RoomIdx>10 的房间 —— 退出房间时 `GameObject.Find('RolePosN')`
#     返回 nil 会空引用崩。
#   · MoveInTime 是**已入住秒数**(不是时间戳), 客户端 `day = ceil(MoveInTime/86400)`。
ROOM_MAX_FLOOR = 6
ROOM_MAX_PER_FLOOR = 10


def _rooms(d):
    return d.setdefault("rooms", {})


def unlocked_floors(d):
    """已解锁楼层(dataconfig_room_unlock 按等级/章节判定)。"""
    import cfgdata
    lv = int((d.get("actor") or {}).get("level") or 1)
    out = []
    try:
        for r in cfgdata.load("dataconfig_room_unlock").values():
            if not isinstance(r, dict):
                continue
            fl, need = r.get("floor"), r.get("level")
            if isinstance(fl, int) and isinstance(need, int) and lv >= need:
                out.append(fl)
    except Exception:
        pass
    return sorted(set(out))


def _room_move_in_secs(v):
    """存档里的 move_in 是**时间戳**, 对外要发**已入住秒数**。"""
    t0 = int((v or {}).get("move_in") or 0)
    if t0 <= 0:
        return 0
    return max(0, int(time.time()) - t0)


def build_all_room_data(d):
    """CSGetAllRoomDataRes{1:FloorList 2:UnlockFloor}"""
    uf = unlocked_floors(d)
    rooms = _rooms(d)
    by_floor = {}
    for k, v in rooms.items():
        try:
            fl, rm = (int(x) for x in str(k).split(":"))
        except Exception:
            continue
        rid = int((v or {}).get("role") or 0)
        if rid <= 0 or not (1 <= rm <= ROOM_MAX_PER_FLOOR):
            continue
        by_floor.setdefault(fl, []).append((rm, rid, _room_move_in_secs(v)))
    r = b""
    for fl in uf:
        fb = uv(1, fl)
        for rm, rid, mi in sorted(by_floor.get(fl) or []):
            fb += bs(2, uv(1, rid) + uv(2, mi) + uv(3, rm))   # RoomDataBase
        r += bs(1, fb)                                        # FloorDataBase
    for fl in uf:
        r += bs(2, uv(1, fl))                                 # ★ Uint32Id{1:ID}
    return r


def assign_room(d, role_id, floor_idx, room_idx):
    """1172 分配/换房。成功返回 True。

    一个角色只能住一间: 先把他从旧房摘掉。目标房原有住客也一并挤走。
    """
    rid, fl, rm = int(role_id), int(floor_idx), int(room_idx)
    if rid <= 0 or not (1 <= fl <= ROOM_MAX_FLOOR) or not (1 <= rm <= ROOM_MAX_PER_FLOOR):
        return False                                  # ★ 越界的房间号会让客户端崩
    if fl not in unlocked_floors(d):
        return False
    rooms = _rooms(d)
    for k in list(rooms.keys()):
        if int((rooms.get(k) or {}).get("role") or 0) == rid:
            del rooms[k]
    rooms["%d:%d" % (fl, rm)] = {"role": rid, "move_in": int(time.time())}
    save(d)
    return True


def build_room_enter_res(d, floor_idx, room_idx):
    """CSAskIntoRoomRes{1:HostRoleID 2:MoveInTime 3:GiftList}

    ★ GiftList 是「按入住天数累积的房间陈设」, 客户端每次进房都用本次响应**重建**,
      本地不留存。现在先回空 —— 房间会显示角色自带的那几件
      (客户端另外读 dataconfig_role_personal_item)。等要做"住久了多东西"再补。
    """
    key = "%d:%d" % (int(floor_idx), int(room_idx))
    v = _rooms(d).get(key)
    if not v:
        return uv(1, 0) + uv(2, 0)
    return uv(1, int(v.get("role") or 0)) + uv(2, _room_move_in_secs(v))


def build_floor_room_res(d, floor_idx):
    """1179 —— 客户端没用这条, 先回一个空的 FloorData 保证不报错。"""
    return bs(1, uv(1, int(floor_idx)))


def build_multi_role_notify(d, role_ids):
    """CSMultiRoleNotify{1: RoleData(repeated CSRoleData)}

    ★ 2026-10-15: 换装(1397)之后必须推这条。
      客户端 logic_role.OnMultiRoleNotify -> SetMultiRoleDate -> logic_role_data.UpdateRoleData,
      角色数据(SkinId 是字段 17)就是这么更新的。
      不推的话: CSSetRoleSkinRes 回来时界面会刷一次, 但那一刻本地角色数据还是**旧的**
      => 角色界面里的 3D 模型还是原来那套衣服, 要回大厅再进才重新读数据。
      (这是客户端的数据时序问题, 服务端主动推一份新数据是最干净的解法。)
    """
    r = b""
    for rid in (role_ids or []):
        rd = (d.get("roles") or {}).get(str(int(rid)))
        if rd is not None:
            r += bs(1, build_role(int(rid), rd))
    return r


def draw(pd, pool_id, times):
    """按卡池配置抽 times 次, 新角色入库; 返回 [(roleId, gainType), ...]

    保底规则(用户给定, 原服已停, 概率由本服实现):
      * 限定(UP)卡池: 70 抽保底出"当期限定SSR 或 任一常驻SSR";
                       140 抽必定出当期限定SSR, 然后保底清零;
                       **所有限定卡池共享同一个保底计数**。
      * 常驻卡池: 70 抽保底出常驻SSR, 然后保底清零。
      * 非保底抽: 基础 SSR 概率 2%(角色稀有度取 dataconfig_role_config.star, 4 为最高)。
    """
    import cfgdata
    import random
    SSR_STAR = 4
    SSR_RATE = 0.02
    pools = {p[0]: p for p in cfgdata.recruit_pools()}
    p = pools.get(int(pool_id))
    ptype = p[1] if p else 1
    ups = [r for r in (p[3] if p else []) if isinstance(r, int)]
    one_cost = p[4] if p else 1
    ten_cost = p[5] if p else 10
    ids = [r for r, _w in cfgdata.pool_roles(pool_id)]
    if not ids:
        ids = cfgdata.all_role_ids() or [1001]
    ssr = [r for r in ids if cfgdata.role_star(r) >= SSR_STAR]
    if not ssr:
        ssr = [r for r in cfgdata.all_role_ids() if cfgdata.role_star(r) >= SSR_STAR] or ids
    non_ssr = [r for r in ids if r not in ssr] or ids

    rec = pd.setdefault("recruit", {})
    rec.setdefault("up_pity", 0)
    rec.setdefault("normal_pity", 0)
    limited = bool(ups) and ptype == 2          # 2 = EnmRecruitPoolType.Up

    def new_role(rid):
        pd["roles"][str(rid)] = {"level": 1, "exp": 0, "strength": 0,
                                 "potential": 0, "ability": 1, "skin": 0}

    out = []
    for _ in range(max(1, times)):
        if limited:
            n = rec["up_pity"] + 1
            if n >= 140:                                    # 硬保底: 必出当期UP
                rid = ups[0] if ups else random.choice(ssr)
            elif n >= 70:                                   # 软保底: UP 或常驻
                rid = random.choice(ups) if (ups and random.random() < 0.5) \
                    else random.choice(ssr)
            elif random.random() < SSR_RATE:
                rid = random.choice(ssr)
            else:
                rid = random.choice(non_ssr)
            rec["up_pity"] = 0 if cfgdata.role_star(rid) >= SSR_STAR else n
        else:
            n = rec["normal_pity"] + 1
            if n >= 70:
                rid = random.choice(ssr)
            elif random.random() < SSR_RATE:
                rid = random.choice(ssr)
            else:
                rid = random.choice(non_ssr)
            rec["normal_pity"] = 0 if cfgdata.role_star(rid) >= SSR_STAR else n

        # ★ 用 _obtain_role 统一处理: 首次给角色本身, 重复按 repeat_gain 给补偿
        #   (前 5 次重复各给 1 个该角色的专属补充协议 3001~3065)。
        first, awards = _obtain_role(pd, rid)
        out.append((rid, 2 if first else 1, awards))

    cost = ten_cost if times >= 10 else one_cost * max(1, times)
    pd["items"]["75"] = max(0, pd["items"].get("75", 0) - cost)
    save(pd)
    return out


def build_hero_buy(pd, pool_id, draws):
    """CSHeroBuyRes{1:RoleList(repeated RecruitRoleData) 3:PoolID 4:GameData}

    RecruitRoleData{1:RoleID 2:GainType 3:GainAward(repeated CSItemPair)}

    ★★ 2026-10-15: 以前**完全不写 GainAward** ⇒ 抽到重复角色只标了 GainType=1,
      但一条奖励都没有 —— 玩家永远拿不到补充协议, 因序链就卡死了。
      客户端 view_multi/single_recruit_ui 只会遍历 GainAward 拼「获得物品」窗,
      重复补偿**全在服务端算**(repeat_gain 只出现在配置解析器里, Lua 侧无逻辑)。
    """
    r = b""
    for e in draws:
        rid, gain = e[0], e[1]
        awards = e[2] if len(e) > 2 else []
        ent = uv(1, rid) + uv(2, gain)
        for i, n in (awards or []):
            ent += bs(3, uv(1, int(i)) + uv(2, int(n)))
        r += bs(1, ent)
    r += uv(3, pool_id)
    r += bs(4, build_gamedata(pd))
    return r


def reset_roles(pd):
    """把角色数据重置回 5 个测试角色 (1001..1005)。"""
    pd["roles"] = {k: {"level": 1, "exp": 0, "strength": 0, "potential": 0, "ability": 1}
                   for k in ("1001", "1002", "1003", "1004", "1005")}
    save(pd)
    return pd


# ---------------- 模块解锁 (2026-10-15 补) ----------------
# CSGetUnlockModuleReq(1203, 空) -> CSGetUnlockModuleRes(1204) { 1: ModuleList(repeated UINT32ID) }
# CSIsModuleUnlockReq(1205){1:ModuleID} -> CSIsModuleUnlockRes(1206){1:IsUnlock}
# CSModuleUnlockNotify(1202) { 1: ModuleList } —— 服务端主动推
#
# 客户端 logic/module_unlock/logic_module_unlock.lua 的判定:
#     IsModuleUnlock(id) = defaultUnLockAll or table.contains(ModuleUnlockData, id)
#     QueryModuleIsUnlockWithTips(id) 同条件, 不满足就弹「功能未开放」
#   ModuleUnlockData 只能由服务端这条 ModuleList 喂 —— 我们以前**完全没实现**,
#   所以只能靠客户端补丁把 defaultUnLockAll 强开兜底。
#
# ★ 为什么页签只剩主线: view_task_ui(proto#1, 注册在 TASK_UPDATE 上) 最后三句是
#       SetActive(tabs[1].btn, logic_task_main.CheckUnlock())   -- 主线
#       SetActive(tabs[3].btn, ...)                             -- 周常
#       SetActive(tabs[4].btn, ...)                             -- 发展
#   而 CheckUnlock() = logic_module_unlock.QueryModuleIsUnlock(1034)。
#   把模块喂全, 这条链就不用再指望客户端补丁了。
#
# ★★ 2026-10-15 修: 不能只照抄 dump.cs 的 EnmModuleUnlockType！
#   那个枚举只到 1028, 但客户端实际用到的 id 还有 1029~1039:
#     1030 = 邮件   (view_home_ui proto#57)   ← 用户报"重置后邮件锁上"
#     1038 = 公告   (view_home_ui proto#56)   ← 用户报"邮件左边那个也锁上"
#     1033 = 签到箱 / 1034 = 任务页签 / 1035 = 私信 / 1037 = 再战 / 1039 = 首充
#     1029 = 返利   (logic_rebate)
#   → 全部扫过一遍(41 个引用 QueryModuleIsUnlock 的文件), 实际范围 1000~1039。
#   这里发 0 + 1000~1099, 留足余量; 多发的 id 客户端只是塞进一个 list, 无害。
MODULE_UNLOCK_IDS = ([0] + list(range(1000, 1100)))


def build_module_unlock_res(d=None):
    """CSGetUnlockModuleRes{1: ModuleList} —— ★ 每个元素是包一层的 Uint32Id{1:ID}。"""
    r = b""
    for mid in MODULE_UNLOCK_IDS:
        r += bs(1, uv(1, int(mid)))
    return r


def build_module_is_unlock_res(d=None, module_id=0):
    """CSIsModuleUnlockRes{1: IsUnlock} —— 单查一律放行(假服务端不需要卡功能)。"""
    return uv(1, 1)


# ---------------- 签到 / 月卡 ----------------
# CSLoginSignInfoReq(1086) -> CSLoginSignInfoRes(1087) { 1: DaySignData }
# DaySignData { 1:TodaySigned(enum) 2:Date(CSDate{2:Month,3:Times}) 3:DayList(CSSignDayData[])
#               4:DropId 5:MonthCardDays 6:MonthCardAwardStatus(enum) }
# CSSignDayData { 1:Day 2:Signed(enum) 7:ItemList(CSItemPair[]) }
#
# ★ 月卡购买弹窗(view_pop_buy_month_card_ui.InitData) 直接吃这份数据:
#       local v = logic_sign.GetSignInfo()      -- 就是 DaySignData
#       ... v.MonthCardAwardStatus / v.MonthCardDays
#   我们一直回空 body => GetSignInfo() 返回 nil => "attempt to index a nil value" => 月卡弹窗崩。
MONTH_CARD_DAYS = 30

# ---------------- 月卡状态 (2026-10-14 补) ----------------
# 月卡的完整机制(用户说明 + dataconfig_item_drop 实证):
#   购买(充值点 418007, Param=[4029]) -> 立即发 item_drop 30045 = 稀因 x300
#                                      -> 同时**开启 30 天的每日领取**
#                                         item_drop 30033 = item3 x80 + item63 x1
#   (item 63 是 func=3 的代币, 80 + 10 = 用户记的"每天 90 因")
MONTH_CARD_ITEM_ID = 4029             # itembase 里**唯一** func==8 的道具(月卡礼包)
MONTH_CARD_DURATION_DAYS = 30
MONTH_CARD_NOW_DROP = 30045           # 立即获得(item_drop: 稀因 x300)
MONTH_CARD_DAILY_DROP = 30033         # 每日获得: item_drop(3 x80 + 63 x1)
MONTH_CARD_DAILY_ITEM = 1             # (旧的自定规则, 已不用)
MONTH_CARD_DAILY_COUNT = 90
# EnmAwardStatus 的取值(按协议里枚举的声明顺序):
#   0 = NOTFINISH(未完成)  1 = FINISHEDNOTGET(完成待领)  2 = ALLREADYGOT(已领取)
# 实证: view_sign_ui 用 `MonthCardAwardStatus == 1` 决定能不能领;
#       view_pop_buy_month_card_ui 用 `== ALLREADYGOT(2)` 决定显示"已领取"。
AWARD_NOTFINISH, AWARD_CAN_GET, AWARD_ALLREADYGOT = 0, 1, 2

# 月卡模型(2026-10-14 按用户要求**重写**, 不再追原实现):
#   · 每买一张月卡 -> 存档里天数 +30, 并记录购买日
#   · 每过一个自然日 -> 天数 -1
#   · 某个自然日持有月卡天数 > 0 -> 当天**普通签到多给 90 稀因**
# 存档字段:
#   month_card_days         剩余天数(int)
#   month_card_buy_date     最近一次购买日 "YYYY-MM-DD"
#   month_card_decay_date   最近一次按自然日扣减的日期(避免一天扣多次)


def _mc_days(d):
    return max(0, int(d.get("month_card_days") or 0))


def _mc_tick(d):
    """按经过的自然日扣减天数。返回 True 表示发生了扣减。"""
    import datetime as _dt
    today = _dt.date.today()
    last = d.get("month_card_decay_date")
    if not isinstance(last, str) or not last:
        d["month_card_decay_date"] = today.isoformat()
        return False
    try:
        y, m, dd = (int(x) for x in last.split("-"))
        lastd = _dt.date(y, m, dd)
    except Exception:
        d["month_card_decay_date"] = today.isoformat()
        return False
    elapsed = (today - lastd).days
    if elapsed <= 0:
        return False
    d["month_card_days"] = max(0, _mc_days(d) - elapsed)
    d["month_card_decay_date"] = today.isoformat()
    return True


def _mc_days_left(d):
    """月卡剩余天数(先按自然日扣减, 再返回)。<=0 表示没有月卡。"""
    _mc_tick(d)
    return _mc_days(d)


def _mc_add(d, days=MONTH_CARD_DURATION_DAYS):
    """购买/续期: 剩余天数 +30, 并记录购买日。"""
    d["month_card_days"] = _mc_days_left(d) + int(days)
    d["month_card_buy_date"] = _today_str()
    return d["month_card_days"]


def _mc_extend(d, days=MONTH_CARD_DURATION_DAYS):
    """兼容旧调用名。"""
    return _mc_add(d, days)


def _grant_drop(d, drop_id):
    """按 dataconfig_item_drop 的某一行发放, 返回 [(item_id, count)]。"""
    import cfgdata
    gain = []
    try:
        for row in cfgdata.load("dataconfig_item_drop").values():
            if not isinstance(row, dict) or row.get("id") != int(drop_id):
                continue
            args = row.get("args")
            if isinstance(args, dict):
                _pairs = []
                for k in sorted(args):
                    e = args[k]
                    if not isinstance(e, dict):
                        continue
                    i = e.get("item_id")
                    n = e.get("item_count") or 0
                    if isinstance(i, int) and n:
                        _pairs.append((int(i), int(n)))
                gain.extend(_add_items(d, _pairs))     # ★ 走统一发放(舍友合同会兑成角色)
            break
    except Exception:
        pass
    return gain


def _today_str():
    import time as _t
    tm = _t.localtime()
    return "%04d-%02d-%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday)


def _day_sign_data(d, signed=None):
    import cfgdata
    import time as _t
    tm = _t.localtime()
    # ★ TodaySigned 必须给 1 才会触发领取, 给 0 则"点高亮格子什么都不发生"。
    #   但**给 1 会循环**(打开就自动领, 领完还是 1 就再领) —— 所以要按天记录:
    #   今天还没领 -> 1(可领); 已领 -> 0。
    if signed is None:
        signed = 0 if d.get("sign_date") == _today_str() else 1
    if SIGN_FORCE_TODAY_SIGNED is not None:
        signed = int(SIGN_FORCE_TODAY_SIGNED)              # ★ 诊断用, 见上面注释
    ds = uv(1, signed)                                     # TodaySigned
    # CSDate{2:Month, 3:Times}: **Times 是 0-based 的"已过天数"**, 客户端显示时 +1。
    # (实测发 14 会高亮成 15) 所以今天要发 tm_mday - 1。
    ds += bs(2, uv(2, tm.tm_mon) + uv(3, tm.tm_mday - 1))  # CSDate
    try:
        # dataconfig_day_signed 是 366 行(按 月×日), 只取**当月**的那些。
        # 字段是 award_item_list(不是 item_list)。
        rows = [r for r in cfgdata.load("dataconfig_day_signed").values()
                if r.get("month") == tm.tm_mon and isinstance(r.get("day"), int)]
        rows.sort(key=lambda r: r["day"])
        for row in rows:
            dd = uv(1, row["day"]) + uv(2, 0)              # Day, Signed
            for p in _item_pairs(row.get("award_item_list")):
                dd += bs(7, p)
            ds += bs(3, dd)
    except Exception:
        pass
    # ---- 月卡 ----
    # ★★ 2026-10-15 修: 月卡那份的状态**必须用 mc_sign_date 判**, 不能拿 sign_date 顶。
    #   以前共用 sign_date, 于是"先签到、当天再买月卡"会被算成"已领取" =>
    #   客户端两个自动领取门控全都不触发 => 当天月卡奖励直接丢失:
    #     · view_sign_ui proto#2 : if TodaySigned == 1 or MonthCardAwardStatus == 1 then SendLoginSignReq()
    #     · logic_sign   proto#8 : 同上条件, 另外点亮主界面签到入口的红点
    #   现在两者解耦: 月卡哪天领过记在 mc_sign_date 上。
    left = _mc_days_left(d)
    ds += uv(5, left)                                       # MonthCardDays(剩余天数)
    if left <= 0:
        ds += uv(6, AWARD_NOTFINISH)                        # 没有月卡
    elif d.get("mc_sign_date") == _today_str():
        ds += uv(6, AWARD_ALLREADYGOT)                      # 今天那份月卡奖励已领
    else:
        ds += uv(6, AWARD_CAN_GET)                          # 今天可领
    return ds


def build_sign_info(d):
    """CSLoginSignInfoRes{1:DaySignData}"""
    return bs(1, _day_sign_data(d))


def sign_in(d):
    """CSLoginSignReq(1247) -> CSLoginSignRes(1248)
       { 1:ItemList(CSItemPair[]) 2:DaySignData 3:MonthCardItemList(CSItemPair[]) }
    领取"今天"那份签到奖励。今天 = 当月 + 当日(以服务端本地时间为准)。"""
    import cfgdata
    import time as _t
    tm = _t.localtime()
    today = _today_str()

    # ---- 普通签到那份: **只在今天还没领过时才发** ----
    #   ★ 2026-10-15: 以前无条件发, 靠客户端"已领就不再发 1247"兜底。现在月卡那份
    #     会**单独**触发一次 1247(见下), 所以这里必须自己判, 否则同一天会领两份。
    gain = []
    if d.get("sign_date") != today:
        try:
            for row in cfgdata.load("dataconfig_day_signed").values():
                if row.get("month") == tm.tm_mon and row.get("day") == tm.tm_mday:
                    gain.extend(_add_item_pairs(d, _item_pairs(row.get("award_item_list"))))
                    break
        except Exception:
            pass
        d["sign_date"] = today        # 记下今天已领, 避免打开界面就无限重复领

    # ---- 月卡的每日那份(字段 3 MonthCardItemList) ----
    # ★ 2026-10-14 按用户确认改成**游戏原始设定**:
    #   月卡弹窗的「每日获得」显示什么就发什么 —— 也就是 dataconfig_item_drop 30033:
    #       item 3 x80  +  item 63 x1 (中型能源恢复装置)
    #   (之前我自作主张写成"90 稀因", 与弹窗预览对不上。)
    # ★★ 2026-10-15 修: **按 mc_sign_date 独立去重**, 与 sign_date 解耦。
    #   否则"先签到 -> 当天买月卡"时这一份永远发不出来(客户端以为已领)。
    #   这一份可以**单独**触发 1247: 客户端在 TodaySigned==0 但
    #   MonthCardAwardStatus==1 时也会自动发请求(view_sign_ui proto#2)。
    mc_gain = []
    if _mc_days_left(d) > 0 and d.get("mc_sign_date") != today:
        mc_gain = _grant_drop(d, MONTH_CARD_DAILY_DROP)
        d["mc_sign_date"] = today
    save(d)
    return {"gain": gain, "mc": mc_gain}


# ★★ 诊断开关 (2026-10-15): 1248 里**带不带 ItemList**。
#   客户端拿到 ItemList 会去弹"获得物品"窗(走 uimgr.ShowView + LoopScrollRect)。
#   用户实测: 重置存档后签到 → 弹窗/领取这条路**客户端内部挂死**(详见 docs 08 §8.40)。
#   1087(只带 DaySignData)一切正常 —— 所以先把 ItemList 关掉做二分。
#   ★ 关掉不影响奖励: 物品由 sign_in() 发到存档, 再走 1052 CSItemNotify 推给客户端。
SIGN_SEND_POPUP = True

# ★★ 二分开关 (2026-10-15): 签到领取(1247) 的回包用哪个命令号回。
#   None  = 正常回 1248 -> 客户端会走 CSLoginSignRes 的处理逻辑(proto#6);
#   19001 = 客户端**没注册过**这个号, 回包被直接丢弃 => 那段处理逻辑整段不执行。
#   用途: 客户端收到 1248 后会卡死, 但服务端**紧接着还推了一条 1052 CSItemNotify**。
#   把回复改成没人认领的号, 就能把"处理 1248"和"处理那条推送"这两件事分开:
#     · 改用 19001 之后**不卡** -> 问题在 CSLoginSignRes 的处理逻辑里;
#     · 改用 19001 之后**照样卡** -> 问题在那条 1052 推送(或更早的环节)上。
SIGN_REPLY_RES_CMD = None

# ★★ 二分开关 2 (2026-10-15): 强制 TodaySigned 的值(None = 不干预)。
#   实测: sign_date 为空(TodaySigned=1) -> 进签到页就卡死;
#         sign_date 是今天(TodaySigned=0) -> 一切正常。
#   而"把 1248 整包丢掉(回包命令号改成没人认领的 19001)照样卡"已证明卡死**不在**
#   处理领取回包那一段。所以现在验另一半: 是不是客户端**只要看到 TodaySigned==1**
#   就会卡(自动领取协程 / 今日高亮那段), 跟领取请求本身无关。
#   设成 0 => 客户端既不自动领取也不高亮, 但仍然会收到 1087 的正常签到页数据。
SIGN_FORCE_TODAY_SIGNED = None

# ★★ 二分开关 3 (2026-10-15): 签到领取(1247) 之后要不要推 `1052 CSItemNotify`。
#   【已破案, 保留开关只为以后复查】
#   真凶**不是**这条推送本身, 而是**基准存档给不可堆叠道具发了 100000 个**:
#     客户端 logic_store.ChangeData(proto#22) 对 is_stack~=1 && Expired<=0 的物品
#     走 `for i = 1, Count do table.insert(展示列表, {...}) end`, 而 ChangeData 的
#     调用方 PrepareShowData(proto#17) 会对**背包里每一件**物品调一次。
#     75 件不可堆叠 x 100000 = 750 万张 Lua 表 => 客户端卡死。
#   1052 处理函数(proto#13)末尾**无条件**调 PrepareShowData, 所以任何推送都会炸;
#   而 1048 登录下发走 SetStoreData 不经过它 —— 这就是"能进游戏、一领签到就死"的原因。
#   修复: _make_baseline.py 里不可堆叠的一律给 1 (见该文件注释)。
SIGN_PUSH_ITEM_NOTIFY = True

# ★★ 二分开关 4 (2026-10-15): 这条推送里**发哪些字段**。
#   `CSItemNotify{1: ItemList(repeated CSStoreItem) 2: AddItemList(repeated CSGetItem)}`
#     · 字段1 CSStoreItem{1:ID 2:Count 3:Expired} —— 客户端 logic_store proto#13 就是靠它
#       更新背包(找不到就 CreateItem 新建条目);
#     · 字段2 CSGetItem  {1:ID 2:Count 3:FirstTime} —— **全项目没有一处 Lua 读它**
#       (grep 过所有非 _pb.lua), 是死数据。
#   所以怀疑面只在字段1。这里先只发字段1(绝对数量), 用现成的 build_item_counts。
SIGN_PUSH_FIELDS = "abs"        # "abs" = 只字段1 ; "both" = 字段1+2(原来的 build_item_notify)


def build_sign_res(d, gain):
    """gain 是 sign_in() 的返回值: {"gain": 签到物品, "mc": 月卡每日物品}。
    也兼容旧式"直接传物品列表"。"""
    if isinstance(gain, dict):
        items, mc_items = gain.get("gain") or [], gain.get("mc") or []
    else:
        items, mc_items = gain or [], []
    r = b""
    if SIGN_SEND_POPUP:
        for i, n in items:
            r += bs(1, uv(1, i) + uv(2, n))      # CSItemPair
    # 注意: 不能写死 signed=1 —— _day_sign_data 的语义是"**还能不能领**":
    # sign_in 已经写了 sign_date, 自动判断就会给出 0(已领), 客户端才不会循环领取。
    r += bs(2, _day_sign_data(d))                # DaySignData
    if SIGN_SEND_POPUP:
        for i, n in mc_items:
            r += bs(3, uv(1, i) + uv(2, n))      # MonthCardItemList
    return r


# ---------------- cs.CSGameData (存档快照) ----------------
def build_gamedata(d):
    r = b""
    a = d["actor"]
    for k, v in (("gold", a["gold"]), ("diamond", a["diamond"]), ("copper", a["copper"])):
        r += bs(1, bs(1, k) + bs(2, str(v)))                 # CSKeyValue
    for rid, rd in d["roles"].items():
        r += bs(5, build_role(rid, rd))
    for iid, cnt in d["items"].items():
        r += bs(6, uv(1, int(iid)) + uv(2, int(cnt)) + uv(3, 0))
    return r


# 充值档位 —— 服务的充值点配置表没 dump 到, 按用户确认的档位写死。
#   稀因 = 道具 id 1;  人民币元 = 稀因数 / 20  =>  分 = 稀因数 * 5
#   (120→¥6, 600→¥30, 1960→¥98, 3960→¥198, 6560→¥328, 12960→¥648)
# 首充双倍"永远有效": 这些数已经是双倍后的值, 每次购买都按它发。
RECHARGE_TIERS = {
    418001: 120,
    418002: 600,
    418003: 1960,
    418004: 3960,
    418005: 6560,
    418006: 12960,
}


def _recharge_point_rows():
    """dataconfig_recharge_point 按 recharge_point_id 建索引。
    ★ 2026-10-14 才 dump 到这张表 —— 之前所有价格/参数都是猜的。
    每个充值点有多个平台行(相同 recharge_point_id, 不同 sub_plat_type),
    优先取 bingkolo 的。"""
    import cfgdata
    out = {}
    for row in cfgdata.load("dataconfig_recharge_point").values():
        if not isinstance(row, dict):
            continue
        rp = row.get("recharge_point_id")
        if not isinstance(rp, int):
            continue
        plat = row.get("sub_plat_type") or ""
        score = 2 if plat.startswith("bingkolo") else (1 if plat else 0)
        old = out.get(rp)
        if old is None or score > old[0]:
            out[rp] = (score, row)
    return {k: v[1] for k, v in out.items()}


_RP_ROWS = None


def _recharge_point_row(rp):
    global _RP_ROWS
    if _RP_ROWS is None:
        try:
            _RP_ROWS = _recharge_point_rows()
        except Exception:
            _RP_ROWS = {}
    return _RP_ROWS.get(int(rp))


def recharge_price_fen(rp):
    """价格(分)。优先用配置表里的真实 cost —— 原来只有 418001~418006 有值,
    礼包商城的 418007~418012 / 728001~728004 / 20001~20026 全都被算成 0。"""
    row = _recharge_point_row(rp)
    if row is not None and isinstance(row.get("cost"), int):
        return int(row["cost"])
    n = RECHARGE_TIERS.get(int(rp))
    return n * 5 if n else 0


def recharge_params(rp):
    """RechargePointInfo.Param (字段 9, repeated Uint32Id{1:ID})。

    ★ 这是"月卡弹窗崩溃 + 预览为空"的根因(反汇编 view_pop_buy_month_card_ui
      InitData = proto#1 实证):
        0064  TGETS R1 R1 'Param'
        0066  TGETB R6 R1 #1            ; Param[1]
        0067  TGETS R6 R6 'ID'          ; Param 为 nil => attempt to index a nil value
        0068  getConfigDataByID('dataconfig_itembase','id', Param[1].ID)   -> R2
        0078  TGETS R3 R2 'func'        ; 只有 func == 8 才走月卡内容分支
        0085  TGETS R7 R2 'args' -> R2.args[3]
        0092  TGETS R8 R2 'args' -> R2.args[4]
        0093  getConfigDataByID('dataconfig_item_drop','id', args[3] / args[4])
        0099  ChangeContent(...)        ; 这才是「立即获得 / 每日获得」的预览
     全客户端 func == 8 的道具只有一个: itembase id=4029, args={3:30045, 4:30033}。
     418007(小月卡, ¥30) 的 param 正是 [4029] —— 与配置表完全一致。"""
    row = _recharge_point_row(rp)
    if row is None:
        return []
    p = row.get("param")
    if isinstance(p, dict):
        return [p[k] for k in sorted(p) if isinstance(p[k], int)]
    if isinstance(p, list):
        return [x for x in p if isinstance(x, int)]
    return []


# ---------------- 商店 ----------------
# CSGetShopListRes { 1:ShopList(repeated CSSHOP) }
# CSGetShopRes     { 1:Shop(CSSHOP) }
#   CSSHOP      { 1:Type(uint32)  2:LabelList(repeated CSShopLabel) }
#   CSShopLabel { 1:LabelID(uint32)  2:ItemList(repeated CSShopItem) }
#   CSShopItem  { 1:Id 2:Count 3:MaxCount 4:IsDiscount
#                 5:Cost(repeated CSItemPair) 6:DiscountCost(repeated CSItemPair)
#                 9:CanBuy(bool) 10:BuyEndTm(int64) 11:IsBought(bool) }
# CSShopBuyReq { 1:Type 2:LabelID 3:Id 4:Count } -> CSShopBuyRes
#
# ★ 商品「剩余次数」= MaxCount - Count。客户端买一次会把本地 Count 加 1，
#   所以 MaxCount 给小了（比如配置里的 count_limit=1）买完就判售罄、商品被挪到
#   列表末尾 = 看起来"消失"。2026-10-15 用户要求统一显示 **99999**。
#   ★ 服务端**本来就不校验限购**（shop_buy 只管够不够钱），所以放大它是纯表现修复。
SHOP_MAX_COUNT = 99999
#   CSShopBuyRes { 1:Type 2:LabelID 3:Id 4:Count
#                  5:ItemList(repeated CSGetItem) 6:ExtraItemList(repeated CSGetItem) }
#
# 数据来源: dataconfig_shop_base(大类) -> dataconfig_shop_label(标签) -> dataconfig_shop_goods(商品)
FAR_FUTURE_SHOP = 4102444800     # 2100-01-01, 让商品"永不过期"


def _item_pairs(u_log_a):
    """[{'item_id':75,'item_count':1}, ...] -> [编码好的 CSItemPair, ...]
    注意: Cost/DiscountCost 是 repeated 字段, 每个 pair 必须各占一次 field 5/6,
          不能拼成一坨。所以这里返回列表, 由调用方逐个包。"""
    out = []
    if not isinstance(u_log_a, dict):
        return out
    for k in sorted(u_log_a):
        v = u_log_a[k]
        if isinstance(v, dict) and isinstance(v.get("item_id"), int):
            cnt = v.get("item_count")
            if not isinstance(cnt, int):
                cnt = 1
            out.append(uv(1, int(v["item_id"])) + uv(2, cnt))
    return out


def _shop_types():
    """要下发的商店大类 = `dataconfig_shop_base.type` ∪ `dataconfig_shop_label.shop_type`。

    ★★ 为什么必须并上 shop_label 那一列: **活动商店的大类不在 shop_base 里**!
      `dataconfig_shop_base` 只有 1~6(常规商店), 而活动商店是
      101/102(卡门) · 103(寝取) · 104 · 105(情绪) · 201~204 ——
      它们**只出现在 `dataconfig_shop_label.shop_type`**。
      ⇒ 只发 6 个大类的话, 客户端 `logic_shop.ShopData[101]` 是 **nil**。

      ★ 实证: `logic_emotion_shop.GetShopData()`(proto#3) 里硬编码
        `logic_shop.GetShopData(101)` —— 拿不到就是**空列表**,
        正是用户报的「非等张共鸣兑换商店什么也没有」。
        (注意它读的是 **101**, 不是 105; 卡门/情绪共用同一套标签名
         「生命重塑/思想萌芽/构件再造」, 服务端这边 101~105 都发就都覆盖到了。)

    ★ `_build_cshop` 的警告仍然成立: 只能下发本地表里**真实存在的**
      (shop_type, label_id) 组合 —— 这里来的每个 type 都能在 shop_label 查到行;
      没有 label 的大类(如 type=1 今日声讯)会退化成空壳(IsOpen=false), 不会崩。
    """
    import cfgdata
    out = []
    for row in cfgdata.load("dataconfig_shop_base").values():
        t = row.get("type")
        if isinstance(t, int) and t not in out:
            out.append(t)
    for row in cfgdata.load("dataconfig_shop_label").values():
        t = row.get("shop_type")
        if isinstance(t, int) and t not in out:
            out.append(t)
    return sorted(out)


def _shop_goods_by_id():
    """dataconfig_shop_goods 的**行主键不是 id**: 行 251 的 id 其实是 772。
    而 shop_label.goods 里存的是 id 字段, 所以必须按 id 建索引。"""
    import cfgdata
    idx = {}
    for r in cfgdata.load("dataconfig_shop_goods").values():
        i = r.get("id")
        if isinstance(i, int):
            idx[i] = r
    return idx


def _build_shop_item(gid):
    g = _shop_goods_by_id().get(int(gid))
    if not isinstance(g, dict):
        return None
    disc = _item_pairs(g.get("discount_item_list"))
    cost = _item_pairs(g.get("origin_item_list"))
    if not cost:
        cost = disc
    rp = g.get("recharge_point")
    # ★ 人民币商品的 origin_item_list 是空的(价格是钱不是道具)。而客户端
    #   ClickBtnShopItem(proto#23) 会**无条件**读 Data.Cost[1].ID,
    #   为空 => nil 崩 => "点了没效果"。
    #   给个占位花费: ID=道具1(稀因) 当图标, Count=**人民币价格(元)** ——
    #   注入会把 params.IsRMB 强制置假, 于是 ChangeCostIcon 走 else 分支,
    #   售价标签直接显示 Data.Cost[1].Count。
    if not cost:
        yuan = 1
        if isinstance(rp, int) and rp > 0:
            fen = recharge_price_fen(rp)
            if fen:
                yuan = int(fen / 100)
        cost = [uv(1, 1) + uv(2, yuan)]
    item = uv(1, int(gid))
    # ★ Count/MaxCount 的语义(反汇编 logic_shop proto#26 实证):
    #     售罄 = (MaxCount - Count == 0)   (MaxCount==0 走"不限购"分支, 永不售罄)
    #     界面上那个「剩余 N」显示的就是 MaxCount - Count。
    #
    # ★★ 2026-10-15 用户要求: 剩余**统一显示 99999**。
    #   以前非人民币商品发的是配置的 count_limit(共 63 个是 1), **人民币商品硬编码 1**；
    #   客户端买一次就把本地 Count 加 1 => MaxCount-Count==0 => 判售罄 =>
    #   售罄项会被挪到列表末尾 => 表现就是「买完商品短暂消失」(用户实测, 见日志 1124)。
    #   ⇒ 现在一律发 Count=0 + MaxCount=SHOP_MAX_COUNT。
    #
    # ★ 人民币商品以前只敢给 MaxCount=1, 是怕购买弹窗里 `math.floor([42]/[36])`
    #   (MaxCount~=0 才执行) 崩。但看清楚: 那条崩溃在 **POP_BUY_ITEM** 里, 而人民币
    #   商品被我们强制走 **POP_BUY_MONTH_CARD**(GoodsType=2, 见下面 rpi 的注释) ——
    #   月卡弹窗没有数量选择器, 压根不读 MaxCount 做除法。
    #   而且 MaxCount=1 本身也已经 ~=0, 走的是同一段代码。⇒ 放大到 99999 不引入新风险。
    item += uv(2, 0)                                    # Count (已购 0)
    item += uv(3, SHOP_MAX_COUNT)                       # MaxCount => 显示「剩余 99999」
    item += uv(4, 1 if disc else 0)                     # IsDiscount
    for p in cost:
        item += bs(5, p)
    for p in disc:
        item += bs(6, p)
    if isinstance(rp, int) and rp > 0:
        # 人民币商品: 客户端 proto#22 在没计费点时直接 "BuyRequest--未配置计费点" 放弃
        item += uv(8, rp)                               # RechargePointID
        # ★ GoodsType(字段 8) 必须给 2:
        #   ClickBtnShopItem 的分流是
        #       IsRMB 且 RechargePointInfo.GoodsType == 2 -> POP_BUY_MONTH_CARD
        #       其余                                      -> POP_BUY_ITEM
        #   而 POP_BUY_ITEM 对人民币商品有缺陷: ChangeCostIcon 的 RMB 分支会跳走,
        #   永远不写 parent_local[42], 随后 InitData 的 math.floor([42]/[36]) 拿到 nil
        #   => 弹窗初始化中断 => 一碰数量控件就引擎级崩溃。
        # ★ GoodsType 必须给 2 —— 让人民币商品走 POP_BUY_MONTH_CARD。
        #   实测**普通弹窗对人民币商品是无解的**(两条路都崩):
        #     IsRMB=true  -> ChangeCostIcon 跳过 else 分支, parent_local[42] 永远 nil
        #                    => InitData 结尾 math.floor([42]/[36]) 崩
        #     IsRMB=false -> 走 else, 但它要读 Config.origin_item_list[1].item_id
        #                    而人民币商品这个字段是空的 => "attempt to index a nil value"
        #   POP_BUY_MONTH_CARD 才是设计给人民币商品的弹窗。
        rpi = (uv(1, rp) + uv(2, rp)
               + uv(7, recharge_price_fen(rp))          # Cost: 价格(分), 客户端 /100
               + uv(8, 2))                              # GoodsType = 2 -> 月卡弹窗
        # ★ Param(字段 9, repeated Uint32Id{1:ID}) —— 缺了它月卡弹窗 InitData
        #   会在 Param[1].ID 上 "attempt to index a nil value",
        #   弹窗画到一半就断 => "小月卡没预览、确定无效、充值点都点不动"。
        #   补上之后: Param[1]=4029 -> itembase(4029).func==8
        #             -> args[3]=30045/args[4]=30033 -> dataconfig_item_drop
        #             -> ChangeContent() 填出「立即获得 / 每日获得」预览。
        _params = recharge_params(rp)
        if not _params:
            # ★ 稀因那几档(418001~418006)配置里的 param 是**空**的,
            #   而月卡弹窗 InitData 会读 `Param[1].ID` —— 空表取 [1] = nil
            #   => "attempt to index a nil value" => 弹窗开不出来
            #   => 表现就是"充值界面的商品点不动"(用户实测)。
            #   填一个**非 func==8** 的占位(道具 3): itembase(3).func 不是 8,
            #   于是 InitData 跳过月卡内容分支, 弹窗正常打开、可以购买。
            _params = [3]
        for pid in _params:
            rpi += bs(9, uv(1, int(pid)))
        item += bs(12, rpi)

    # RefreshTm(字段 7): 客户端 CheckItemRefresh(定时器每秒跑) 的判定是
    #     Config.refresh_type 为真 且 RefreshTm - (now - GetTime) >= 0 才算"还新鲜",
    #     否则 -> GetRefreshShopInfo + SendGetShopListReq 重新拉整张列表。
    #   原来没填(默认 0) => 每秒都判定"该刷新" => 1117 一秒刷一次,
    #   列表被反复重建 => 滚动时表现为"动态排序不正常"。
    item += uv(7, FAR_FUTURE_SHOP)                      # RefreshTm (永不过期)
    item += uv(9, 1)                                    # CanBuy
    item += uv(10, FAR_FUTURE_SHOP)                     # BuyEndTm
    item += uv(11, 0)                                   # IsBought
    return item


def _build_shop_item_from_item_row(row):
    """dataconfig_shop_item 的某一行 -> CSShopItem"""
    gid = row.get("id")
    if not isinstance(gid, int):
        return None
    disc = _item_pairs(row.get("discount_item_list"))
    cost = _item_pairs(row.get("origin_item_list")) or disc
    item = uv(1, gid)
    item += uv(2, 1)                                    # Count
    item += uv(3, 0)                                    # MaxCount
    item += uv(4, 1 if disc else 0)                     # IsDiscount
    for p in cost:
        item += bs(5, p)
    for p in disc:
        item += bs(6, p)
    item += uv(9, 1)                                    # CanBuy
    item += uv(10, FAR_FUTURE_SHOP)                     # BuyEndTm
    item += uv(11, 0)                                   # IsBought
    return item


def _shop_item_rows(shop_type):
    import cfgdata
    rows = [r for r in cfgdata.load("dataconfig_shop_item").values()
            if r.get("type") == shop_type]
    rows.sort(key=lambda x: (x.get("sort_weight") or 0, x.get("id") or 0))
    return rows


def _build_cshop(shop_type):
    """按大类拼一个 CSSHOP。没有标签时返回 None。

    ★ 关键约束(反汇编 logic_shop proto#16 实证):
        客户端对每个标签做
            LabelConfig = getConfigDataByTwoID(
                'dataconfig_shop_label', 'shop_type', <Type>, 'label_id', <LabelID>)
        也就是说它按 **(shop_type, label_id) 组合** 去查本地表。
        查不到 => LabelConfig = nil => 之后 GetRefreshShopInfo / CheckItemRefresh
        (定时器驱动) 会 "attempt to index field 'LabelConfig' (a nil value)" 疯狂报错。
        **所以只能下发本地表里真实存在的 (Type, LabelID) 组合。**
      dataconfig_shop_label 里 shop_type 只有 2~6 和 101~105/201~204 ——
      没有 1, 所以 shop_base 的 type=1 不能下发(发了必崩)。
    """
    import cfgdata
    labels = []
    for row in cfgdata.load("dataconfig_shop_label").values():
        if row.get("shop_type") != shop_type:
            continue
        lid = row.get("label_id")
        if not isinstance(lid, int):
            continue
        labels.append((lid, row))
    if not labels:
        return None
    r = uv(1, int(shop_type))
    for lid, row in sorted(labels):
        goods = row.get("goods")
        if not isinstance(goods, dict):
            continue
        gl = b""
        for k in sorted(goods):
            gid = goods[k]
            if not isinstance(gid, int):
                continue
            it = _build_shop_item(gid)
            if it is not None:
                gl += bs(2, it)
        r += bs(2, uv(1, lid) + gl)
    return r


def build_shop_list(d):
    """所有 shop_base 的大类都要发 —— 省略的话客户端 ShopData[Type] 是 nil。
    没有标签的大类(type=1 今日声讯)发 **空 LabelList**:
    这样 IsOpen=false, 而且标签循环 0 次, 不会去读 LabelConfig(那里会崩)。"""
    r = b""
    for t in _shop_types():
        c = _build_cshop(t)
        if c is None:
            c = uv(1, int(t))              # 空壳: Type 有了, 没有 LabelList
        r += bs(1, c)
    return r


def build_shop(d, shop_type):
    c = _build_cshop(int(shop_type))
    if c is None:
        c = uv(1, int(shop_type))          # 空壳, 至少 Type 对
    return bs(1, c)


def build_shop_support_award(d):
    """CSShopSupportAwardInfoRes: 客户端在大厅反复轮询(实测 14 次)。给全 0 即可。"""
    return uv(1, 0) + uv(2, 0) + uv(3, 0) + uv(4, 0)


def _find_shop_goods(shop_type, label_id, goods_id):
    """按 Type/LabelID/Id 定位商品配置, 找不到返回 None"""
    import cfgdata
    for row in cfgdata.load("dataconfig_shop_label").values():
        if row.get("shop_type") != shop_type or row.get("label_id") != label_id:
            continue
        g = row.get("goods")
        if isinstance(g, dict):
            for k in sorted(g):
                if g[k] == goods_id:
                    return _shop_goods_by_id().get(goods_id)
    # 回退: dataconfig_shop_item (type=1 这类没有标签的大类)
    for row in _shop_item_rows(shop_type):
        if row.get("id") == goods_id:
            return row
    return None


def shop_buy(d, shop_type, label_id, goods_id, count):
    """扣 Cost、发 award_item_list。

    返回 **(发放的 [(itemId, count)], 是否成功, 付费的 itemId 列表)**。
    ★ 第三个返回值是 2026-10-15 加的: 调用方必须把**付掉的道具**也推一条 1052,
      否则客户端手里的数量不变(见下面 _pay_items 处的注释)。
    """
    g = _find_shop_goods(shop_type, label_id, goods_id)
    if not isinstance(g, dict):
        return [], False, []
    count = max(1, int(count or 1))

    disc = _item_pairs(g.get("discount_item_list"))
    cost = _item_pairs(g.get("origin_item_list")) or disc
    # CSItemPair 是 (field1=ID, field2=Count) 的 varint 串, 这里重新解出来算钱
    pay = []
    for p in cost:
        i, n = 0, 0
        for f, w, v in B.pb_fields(p):
            if f == 1:
                i = v
            elif f == 2:
                n = v
        if i:
            pay.append((int(i), int(n) * count))

    # ★★ 2026-10-15 修「购买稀因商品没消耗稀因」:
    #   以前这里只查/只扣 d["items"], 而 _ensure_shop_currency 每次 load() 又会把
    #   items["1"](稀因)/items["3"] 补回保底 10000 => 买完下次读档就白买了。
    #   现在统一走 _have_item()/_pay_items(): 货币(1 稀因/2 格/3 因)在 actor 上有
    #   权威值, 背包里也有一份, 两边一起算、一起扣。
    #   ★ 但光扣还不够 —— 客户端只有收到 1052 才会更新本地缓存(见 build_item_notify
    #     的注释), 而 1121 之前只推了 **买到的**、没推 **花掉的** ⇒ 数字不动。
    #     paid 这个返回值就是给调用方补推用的。
    if not _pay_items(d, pay):
        return [], False, []

    # ★★ 走统一发放: 舍友合同会在**发放那一刻**换成角色/补充协议(见 _add_items)。
    #   以前这里是自己塞 items 的 ⇒ 商店买的合同直接进背包, 用户实测
    #   「情绪商店里卖的那个（芙斯忒）还是领到舍友合同」。
    gain = _add_item_pairs(d, _item_pairs(g.get("award_item_list")), times=count)
    # ★ 月卡礼包(item 4029) 的天数**已经在 `_add_items` 里加过了**
    #   （它现在"发放即开掉"，不进背包、也不出现在 gain 里）。
    #   这里以前还有一句 `if MONTH_CARD_ITEM_ID in gain: _mc_add(30)` —— 2026-10-16
    #   删掉，否则和 `_add_items` 里那份**重复加**，就是「首购 60 天」的另一半成因。
    save(d)
    return gain, True, [i for i, _n in pay]


def _shop_goods_by_recharge_point():
    idx = {}
    for g in _shop_goods_by_id().values():
        rp = g.get("recharge_point")
        if isinstance(rp, int):
            idx.setdefault(rp, g)
    return idx


def buy_check(d, recharge_point):
    """人民币商品: CSBuyCheckReq{1:RechargePoint} 只带一个充值点 id,
    服务端据此在 shop_goods 里反查商品并直接发放(离线环境没法真的付钱)。"""
    rp = int(recharge_point)
    # 稀因充值档: 配置里是占位, 按 RECHARGE_TIERS 发(首充双倍永远有效)
    if rp in RECHARGE_TIERS:
        n = RECHARGE_TIERS[rp]
        d["items"]["1"] = int(d["items"].get("1", 0)) + n
        save(d)
        return [(1, n)]
    g = _shop_goods_by_recharge_point().get(rp)
    if not isinstance(g, dict):
        return []
    gain = []
    for key in ("award_item_list", "first_buy_award_item_list"):
        gain.extend(_add_item_pairs(d, _item_pairs(g.get(key))))   # ★ 统一发放
    # ★ 月卡礼包(item 4029) 的天数已在 `_add_items` 里加过（发放即开掉），
    #   这里不再重复加 —— 见 2250 附近的注释和「首购 60 天」的成因说明。
    if gain:
        save(d)
    return gain


def build_item_notify(d, gain):
    """CSItemNotify(1052):
        1:ItemList    (repeated CSStoreItem{1:ID 2:Count 3:Expired})  —— **绝对数量**
        2:AddItemList (repeated CSGetItem  {1:ID 2:Count 3:FirstTime}) —— 增量

    ★ 客户端只在收到这条**推送**时才会更新自己的本地道具缓存 —— 顶部货币区就是读它的。
      服务端直接改存档而不推这条, 玩家看到的就是
      "买了月卡稀因没变"(2026-10-14 实测确认: 直到抽卡触发另一次刷新数字才跳)。
      两条都发, 覆盖客户端两种读法。

    ★★ 陷阱 (2026-10-15): 下面有 `if n == 0: continue` —— **增量为 0 的条目会被整条
       跳过**。所以想把"被扣掉的道具"同步给客户端的正确写法是
           build_item_counts(d, ids)
      而不是 `build_item_notify(d, [(id, 0), ...])` —— 后者**一个字节都不会推**。
      多处调用点(1022 因序链 / 1018 突破 / 商店付费)都踩过这个坑。

    ★★ 2026-10-15 事故修复 —— 这里原来是 `r = b""` + 循环里 `r += ...`:
        **O(n²)**。开一个随机礼包(9999 抽 × 每抽 ≤10 件 => gain 能有 10 万条)时,
        10 万次对大字节串做 `+=` 要拷几十 GB => 进程满核卡死、内存涨到 5.9 GB、
        每条心跳都要 1 秒(用户实测「转圈」「网络超时」)。
        py-spy 抓到两个线程都停在这个函数里。
        现在: (1) 先按 item_id **合并**增量(10 万条 -> 十来个);
              (2) 用 list 攒片段 + `b"".join`, 拼接降到 **O(n)**。
    """
    agg = {}
    order = []
    for i, n in gain:
        i = int(i)
        if i not in agg:
            agg[i] = 0
            order.append(i)
        agg[i] += int(n)
    parts = []
    it = d.get("items") or {}
    for i in order:
        n = agg[i]
        if n == 0:
            continue
        # ★★ 绝对数量**绝不能拿增量 n 当兜底**。消耗掉的物品会被移出仓库,
        #   `it.get(str(i), n)` 这时候就返回 n = -1 ⇒ 客户端把 -1 当 uint64 读
        #   ⇒ 显示 18446744073709551615(2^64-1)。用户实际看到的是
        #   「使用进修助力包后它没消失, 数量变成了 1844…0737亿」(2026-10-15)。
        #   不在仓库里 = 数量就是 **0**。
        key = str(i)
        if key in it:
            total = int(it[key])
        else:
            total = n if n > 0 else 0
        parts.append(bs(1, uv(1, i) + uv(2, total) + uv(3, 0)))   # CSStoreItem: 绝对数量
        # ★ 只报**增加**。`CSGetItem.Count` 是 uint32, 负数本来就表示不了;
        #   而且负数会掉进 write_varint 的算术移位死循环(2026-10-15 事故)。
        #   消耗靠 1 号字段(绝对数量)传达即可 —— 所以调用方传 `-n` 也是安全的。
        if n > 0:
            parts.append(bs(2, uv(1, i) + uv(2, n) + uv(3, 0)))   # CSGetItem: 增量
    return b"".join(parts)


def build_item_counts(d, ids):
    """只发**绝对数量**(CSItemNotify 的 1 号字段), 不带增量。

    用途: 客户端因为历史 bug 记住了错误的数量(例如那个 2^64-1 的幽灵礼包),
    服务端这边明明已经没有了 ⇒ 客户端还能点"使用", 点了又什么都没发生。
    推一条真实数量回去, 让客户端**自愈**(数量归零 => 那个礼包从背包里消失)。
    """
    it = d.get("items") or {}
    parts = []
    for i in ids:
        # ★★ 2026-10-16: **同时容忍 `(item_id, count)` 这种对**。
        #   之前扫荡/打关的掉落推送把 `[(id, n), ...]` 直接传进来了, `int(元组)`
        #   抛 TypeError ⇒ 1095 的回包虽然发出去了, 但**推送那行炸掉 ⇒ 连接被关** ⇒
        #   客户端被迫重连 ⇒ 玩家看到"领完奖励转圈加载一会"。
        #   这类错最容易悄无声息地掐断连接, 所以这里直接容忍, 别再让它炸。
        if isinstance(i, (tuple, list)):
            i = i[0]
        i = int(i)
        parts.append(bs(1, uv(1, i) + uv(2, int(it.get(str(i), 0))) + uv(3, 0)))
    return b"".join(parts)


def build_buy_success(d, recharge_point, gain, order_id="local-order-1"):
    """CSBuySuccessNotify{1:CpOrderId 2:OrderId 3:RechargePoint 4:Price 5:ItemList 6:ExtraItemList}
    客户端拿它当"支付成功"信号去刷 UI(它的常量表里没有 ItemList), 物品由服务端直接发。"""
    r = bs(1, "cp-local") + bs(2, order_id) + uv(3, int(recharge_point)) + uv(4, 0)
    for i, n in gain:
        r += bs(5, uv(1, i) + uv(2, n) + uv(3, 0))
    return r


def build_shop_buy(d, shop_type, label_id, goods_id, count, gain):
    r = uv(1, int(shop_type)) + uv(2, int(label_id)) + uv(3, int(goods_id)) + uv(4, int(count))
    for iid, n in gain:
        r += bs(5, uv(1, iid) + uv(2, n) + uv(3, 0))
    return r


# 商店的 cost 会用到各种货币道具(实测有 34 等玩家默认没有的 id)。
# 缺哪个补哪个, 只补不削 => 商店可以正常买。
#
# ★ 2026-10-15 用户要求「背包道具给 10, 要不然商城没意义」——
#   这个保底当时是 100000, 会让 12 件集市货币(含 稀因/因/邂逅之影/邂逅之迹/
#   援外协议/募集积分/原始指针/密道频谱/演绎数据/梦境琥珀/残影香华)照样是 10 万,
#   等于把那条要求废掉一半。降到 10000, 和三币口径一致:
#   买得起东西, 但不再是无限。
SHOP_CURRENCY_FLOOR = 10000


def _shop_cost_ids():
    ids = set()
    for g in _shop_goods_by_id().values():
        for lst in (g.get("origin_item_list"), g.get("discount_item_list")):
            for p in _item_pairs(lst):
                for f, w, v in B.pb_fields(p):
                    if f == 1:
                        ids.add(int(v))
    return ids


# ======================================================================
#  回合活动 (turn_activity) 的时间窗         2026-10-15
# ======================================================================
# 客户端把三条线的「活动时间 id」**写死在代码里**
# (`model_turn_activity_data.LimitActivityTimeId`, KTAB 实测):
#       Emotion = 20001, Carmen = 50001, Sleep = 70001
# 而它们**不在 `dataconfig_activity_time` 里** —— 那张表的 time_id 是
# 1000xx~5000xx 段(是另一套排期)。这三个值其实是 `dataconfig_activity.time`:
#       活动 id=1 -> 20001(Emotion) / id=3 -> 50001(Carmen) / id=6 -> 70001(Sleep)
#
# ★★ 客户端怎么挑「当前活动」(`logic_activity_time.GetCurTurnLimitActivityTimInfo`,
#    proto#16 反汇编实证): **返回列表里第一个 `TurnActivityType == 1` 的条目**
#    (`LimitActivityTimeType = {EndlessLevel=1, EmotionActivity=2, LimitTimeTask=3,
#      SignActivity=4, CarmenActivity=5, SleepActivity=6}`)。
# 然后 `view_home_ui.RefreshActivityState` 拿这条的 `TimeID` 去比
# {20001, 50001, 70001}: 都不是 -> `GetCurActivityType()` 返回 nil -> 走 else 分支
# -> carmen / sleep / emotion **三个入口全部 SetActive(false)**
# -> 主页左下角只剩 prefab 自带的 "COMING SOON"。
#
# 之前 1401 只遍历 `dataconfig_activity_time`(里面 type=1 的行 time_id 是 1000xx),
# 所以选中的永远是 1000xx ⇒ 永远不匹配 ⇒ 活动入口永远点不出来。
#
# ⇒ 修法: 把回合活动的时间 id **放在列表最前面**、`ActivityType` 填 **1**,
#   这样它一定是「第一个 type==1 的条目」。
TURN_ACTIVITY_TIME_ID = 50001        # 默认线 = Carmen(幕前序曲, 章节 4004/4005/4006)
TURN_ACTIVITY_TIME_ID_ALT = 70001    # Sleep(伴好梦入眠, 4007~4010)

# ★ 三条线(客户端的 LimitActivityTimeId 写死的就是这三个 id)。
#   名字取自客户端 `ActivityName` 表 + lang_zh.json:
#     20001 = toText(18294) = 非等张共鸣(Emotion)
#     50001 = toText(18295) = 幕前序曲(Carmen)
#     70001 = 伴好梦入眠(Sleep)
#   ⚠️ Emotion 那条**没有 ActivityChapter 数据**, 切过去大概率是空白页。
TURN_ACTIVITY_LINES = (
    (50001, "幕前序曲", "卡门线 (Carmen)"),
    (70001, "伴好梦入眠", "寝取线 (Sleep)"),
    (20001, "非等张共鸣", "情绪线 (Emotion) — 无章节数据, 可能是空白页"),
)

# ★★ 当前活动线**热读 `_re\turn_activity.json`** —— 改完不用重打包,
#    由公告页的「活动切换」按钮(HTTP /switchact)改写。
TURN_ACTIVITY_JSON = paths.data_path("KLEINS_TURN_ACT_JSON", "state",
                                     "turn_activity.json")


def turn_activity_time_id():
    """当前活动线的时间 id。读不到/值非法就退回默认 50001, 绝不抛。

    ★ 候选线路的表在 `turn_activity.json` 的 `lines` 里(公告页切换按钮也读它),
      这里**只校验是个正整数** —— 想加线路不用改代码。
    """
    try:
        with open(TURN_ACTIVITY_JSON, "r", encoding="utf-8-sig") as f:
            v = int(json.load(f).get("time_id"))
        if v > 0:
            return v
    except Exception:
        pass
    return TURN_ACTIVITY_TIME_ID
LT_OPEN_A = 1                        # ActivityStatus.Open_A
LT_TYPE_ENDLESS = 1                  # LimitActivityTimeType.EndlessLevel
LT_FAR = 4102444800                  # 2030-01-01, 当作"永不过期"
# ★★ 时段起点必须是**过去**。第一版把 PartAStart 也设成 2030 年, 结果
#    "现在 < 起点" ⇒ 界面里部分功能显示「活动已经结束」(用户实测)。
#    `IsInLimitActivityAPartTime`/`BPartTime` 只看 `State`(State==Open_A/Open_B),
#    但**显示用的起止时间**是拿 PartA/B 的四个时间戳去比的 ⇒ 起点得在现在之前。
LT_PAST = 1                          # 1970-01-01, 已经过去了
LT_TYPE_EMOTION = 2                  # LimitActivityTimeType.EmotionActivity
LT_TYPE_SLEEP = 6                    # LimitActivityTimeType.SleepActivity
# ★★★ 2026-10-16: 「小窝(XiaoWo)」活动的时间 id = **30001**（= `dataconfig_activity[2].time`）
#   这是**活动任务 UI 的唯一入口**！链路（全部反汇编实证）:
#     `view_home_ui.ClickBtnActivityTask`（proto#40）= `QueryModuleIsUnlockWithTips(1033)`
#       然后 `uimgr.ShowView(UICfg.ACTIVITY_TASK)`  ← = view_task_type_activity_ui
#     主页那个按钮 `EventsGroup/XiaowoBtn` 的显隐:
#       `logic_task_main.IsUnlockXiaoWo()`      = QueryModuleIsUnlock(1034)
#       `logic_task_main.IsInXiaoWoActivityTime()` = **local[13]**
#     而 local[13] 只有两个地方写:
#       `logic_task_main.AllServiceDateOK`(proto#12):
#            `local t = logic_activity_time.GetLimitActivityTimInfo(30001)`
#            `local[13] = t ~= nil and t.ActivityStartTime_A <= now <= t.ActivityEndTime_B`
#       `LimitedTimeActStateChangeNotify`(proto#4): `TimeId == 30001` 时按 `State ~= 0`
#   ⇒ **1402 里必须有 TimeID=30001 那一条**，否则 `GetLimitActivityTimInfo(30001)` 返回 nil
#     ⇒ 小窝按钮永远隐藏 ⇒ 活动任务界面（和那个全清宝箱）**根本打不开**。
#   ★ 任务界面（任务按钮）里**没有「活动」页签** —— `view_task_ui.Init`(proto#0) 只实例化
#     主线/日常/周常/发展四个页，`view_task_type_activity_ui` 虽然被 require 进来但没人 ShowView。
#     ⇒ 之前找不到「活动页签」就是这个原因，不是 bug。
LT_TYPE_LIMIT_TASK = 3               # LimitActivityTimeType.LimitTimeTask（给小窝这种兜底）
# ★★★ 1348 的 ActivityStatus 和 1402 的 State 用的是**两套不同的枚举**, 别混!
#   1348 `CSActivityTimeInfo.ActivityStatus` -> 客户端 `logic_activity_time.ActivityState`
#        = { Close = 1, Open = 0 }        ← ★★ Open 是 **0**!
#   1402 `CSLimitedTimeInfo.State`       -> 客户端 `LimitActivityState`
#        = { Close = 0, Open_A = 1, Close_A = 2, Open_B = 3 }   ← Open_A 是 1
#   客户端判定就一句 `return entry.ActivityStatus == ActivityState.Open`
#   ⇒ 1348 填 1 = Close ⇒ 所有 time_mode==1 的章节全判"已结束"
#     (踩过: 一直填 1, 叙事曲/活动关卡弹「情绪活动已经结束」, 查了一整轮)
AT_OPEN = 0                          # ★ 0 = Open, 不是 1!


def build_limited_time_info():
    """CSLimitedTimeInfoRes { 1: LimitedTimeInfo(repeated CSLimitedTimeInfo) }

    CSLimitedTimeInfo { 1:TimeId 2:State 3:PartAStartTime 4:PartAEndTime
                        5:PartBStartTime 6:PartBEndTime 7:ActivityType }
    ★ 回合活动那条必须**排在第一个**(客户端只认第一个 type==1 的)。
    """
    import cfgdata
    r = b""


    def entry(tid, atype):
        # 起点在过去、终点在很远的未来 ⇒ "进行中"
        return B.pb_b(1, B.pb_v(1, int(tid))
                      + B.pb_v(2, LT_OPEN_A)
                      + B.pb_v(3, LT_PAST) + B.pb_v(4, LT_FAR)
                      + B.pb_v(5, LT_PAST) + B.pb_v(6, LT_FAR)
                      + B.pb_v(7, int(atype)))

    # ① 回合活动(第一条, 必须是 type=1)。当前是哪条线从 turn_activity.json 热读。
    cur = turn_activity_time_id()
    r += entry(cur, LT_TYPE_ENDLESS)
    # ② 原来那套排期表, 原样保留(其它活动还指着它)
    for row in cfgdata.load("dataconfig_activity_time").values():
        tid = row.get("time_id")
        if not isinstance(tid, int) or tid == cur:
            continue
        r += entry(tid, int(row.get("activity_type", 1) or 1))
    # ③ ★ 另外两条回合线也要发, 但**必须排在 cur 后面**(不能抢走"第一条 type==1")。
    #    为什么发: 章节表里 `time_mode == 2` 的章节(4001~4003 / 4021~4023, time_id=20001)
    #    走的是 `logic_activity_time.IsInLimitActivityAPartTime(time_id)` ——
    #    **查的就是这张 1402 表**(`logic_chapter.IsLockChapterAndInTime` 反汇编实证),
    #    查不到就判"已结束"、章节点不进去。类型填各自真实枚举, 免得被
    #    `GetCurTurnLimitActivityTimInfo`(取第一条 type==1)当成当前活动抢走。
    _extra = {50001: LT_TYPE_ENDLESS, 20001: LT_TYPE_EMOTION, 70001: LT_TYPE_SLEEP}
    # ★★★ 2026-10-16: **只补 30001（小窝/XiaoWo）**，不要整批补 `dataconfig_activity[].time`！
    #
    #   为什么需要 30001: `logic_task_main.AllServiceDateOK`(proto#12) 拿
    #   `GetLimitActivityTimInfo(30001)` 判小窝按钮显不显示，而小窝按钮是
    #   **活动任务界面（ACTIVITY_TASK）的唯一入口**（见上面的长注释）。
    #
    #   ★★★ 为什么**不能**整批补（我 2026-10-16 踩过，用户实测"回大厅卡死"）:
    #   `dataconfig_active_box` 有 7 行**活动宝箱**，其中四行是
    #   `time_type = 2` ⇒ 走 **1402** 的 `GetLimitActivityTimInfo`:
    #       id=2 time_id=60001  ui_name=view_active_fight_again
    #       id=4 time_id=30002  ui_name=view_active_dream      ← 就是 ACTIVITY_TASK 那个箱子
    #       id=7 time_id=80001  ui_name=view_active_sword_sign
    #   （`model_active_box_data.PrepareActiveBoxData` proto#5 实证）
    #   我一补进 60001/30002/80001，这三个箱子立刻 IsOpen=true，
    #   而它们的实现**我们一个都没做** ⇒
    #     `active_box_manager.GetAllRedDotState`(proto#8) 回调
    #     `logic_box_task.GetRedState` ⇒ `logic_task_main.TaskList[4][5]` 是 nil
    #     ⇒ `#nil` 抛异常 ⇒ **回大厅时主页初始化中断 ⇒ 什么也点不动**
    #   （客户端 probe.log: `logic_box_task: in function 'GetRedState'`
    #     `attempt to get length of upvalue '' (a nil value)`）
    #
    #   ⇒ 只补 30001；那些 time_id 对应的功能做出来了再逐个加回来。
    _extra[30001] = LT_TYPE_LIMIT_TASK      # 小窝(XiaoWo) —— 活动任务界面的入口
    _extra.pop(cur, None)
    for tid in sorted(_extra):
        r += entry(tid, _extra[tid])
    return r


def _chapter_extra_time_ids(have):
    """章节表里 `time_mode == 1`、但排期表没有的 time_id(保持出现顺序)。

    ★ `logic_chapter.IsLockChapterAndInTime`(proto#53)反汇编实证:
        time_mode == 1 -> logic_activity_time.IsInActivityTime(time_id)   -- 查 1348
        time_mode == 2 -> IsInLimitActivityAPartTime(time_id)             -- 查 1402
      而 `IsInActivityTime` 查不到**直接返回 false** ⇒ 该章节被当成"活动已结束"、进不去。
      另一条路 `model_turn_activity_data.GetChapterUnlockData`(proto#22)更狠:
      `GetActivityTimInfo(time_id)` 返回 nil 就把章节的 Open/Close 都设成
      `GetBusinessServerTime() - 86400` ⇒ **显示"昨天就结束了"**。
    ⇒ 凡是章节要用的 time_id, 1348 里**必须**有一条, 否则那个章节永远进不去。
    """
    import cfgdata
    out, seen = [], set()
    for row in cfgdata.load("dataconfig_stage_chapter").values():
        if row.get("time_mode") != 1:
            continue
        tid = row.get("time_id")
        if not isinstance(tid, int) or tid in have or tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def build_activity_time_info():
    """CSGetActivityTimeInfoRes { 1: CSActivityTimeInfo(repeated) }

    CSActivityTimeInfo { 1:TimeID 2:ActivityType 3:ActivityStatus
                         4:ActivityStartTime 5:ActivityEndTime }
    返回 (body, 条目数)。
    ★ 起点在过去、终点在很远的未来 ⇒ 一律"进行中"。
    ★ 除了排期表, 还要补上**章节引用但排期表没有**的 time_id。
    """
    import cfgdata
    r = b""
    have = set()

    def entry(tid, atype):
        return B.pb_b(1, B.pb_v(1, int(tid))
                      + B.pb_v(2, int(atype))
                      + B.pb_v(3, AT_OPEN)
                      + B.pb_v(4, LT_PAST) + B.pb_v(5, LT_FAR))

    for row in cfgdata.load("dataconfig_activity_time").values():
        tid = row.get("time_id")
        if not isinstance(tid, int) or tid in have:
            continue
        have.add(tid)
        r += entry(tid, int(row.get("activity_type", 1) or 1))
    extra = _chapter_extra_time_ids(have)
    for tid in extra:
        have.add(tid)
        r += entry(tid, 1)          # ★ 类型填 1(排期表里最常见的值)
    return r, len(have), extra


# ======================================================================
#  非等张共鸣 (Emotion) —— 记忆分析活动        2026-10-16
# ======================================================================
# ★ 这条线在客户端里**不是** turn_activity 那套 UI, 而是 `logic/event_emotion/`,
#   走另外三条命令(我们以前**一条都没回**, 所以页面是死的):
#     1381 CSEmotionActInfoReq      -> 1382 {1:TimeId 2:AnalysedList 3:FixModule}
#     1371 CSEmotionActAnalysisReq  -> 1372 {1:ItemList(repeated CSGetItem)}
#     1373 CSEmotionActFixModuleReq -> 1374 {1:StageList(repeated CSStageScore)}
#
# ★★ 双保险 —— 少一个都不行:
#   ① 1382 的 `TimeId` 必须是 **20001**(客户端的 LimitActivityTimeId.Emotion);
#   ② **1402 里也必须有一条 20001** —— `view_event_emotion_home_ui.OnEmotionActInfoRes`
#      (proto#24 反汇编)拿到 TimeId 后**立刻**
#      `logic_activity_time.GetLimitActivityTimInfo(TimeId)`(查 1402),
#      查不到就 `BackToUIStackBottom()` + 弹 **18206「活动已结束」**。
#      (20001 已经在 build_limited_time_info 里补上了, 见那边的 ③)
EMOTION_TIME_ID = 20001


def _emotion_analysed(d):
    """已分析过的节点 id 列表(存存档)。"""
    v = d.get("emotion_analysed")
    if not isinstance(v, list):
        v = []
        d["emotion_analysed"] = v
    return v


def build_emotion_info(d):
    """1382 CSEmotionActInfoRes { 1:TimeId 2:AnalysedList(repeated Uint32Id) 3:FixModule }"""
    r = uv(1, EMOTION_TIME_ID)
    for i in _emotion_analysed(d):
        try:
            r += bs(2, uv(1, int(i)))
        except Exception:
            continue
    if d.get("emotion_fix_module"):
        r += uv(3, 1)
    return r


def emotion_analyse(d, node_id):
    """分析一个节点。返回 (发放列表 [(id,count)], 是否成功, 说明)。

    ★ 节点表 `dataconfig_act_emotion_analysis`:
      id / pre_stage_id / next_stage_id(链式) / award_item_list / analysis_text_id
    ★ 同一个节点**只发一次**, 重复请求不重复发奖。
    """
    import cfgdata
    done = _emotion_analysed(d)
    try:
        node_id = int(node_id)
    except Exception:
        return [], False, "node_id 不是数字"
    if node_id in done:
        return [], False, "该节点已分析过(不重复发奖)"
    row = cfgdata._by_id("dataconfig_act_emotion_analysis").get(node_id)
    if row is None:
        return [], False, "没有这个分析节点 id=%s" % node_id
    gain = _grant_items(d, row.get("award_item_list"))
    done.append(node_id)
    return gain, True, ""


def build_emotion_analysis_res(gain):
    """1372 CSEmotionActAnalysisRes { 1:ItemList(repeated CSGetItem{1:ID 2:Count 3:FirstTime}) }"""
    r = b""
    for iid, cnt in (gain or []):
        r += bs(1, uv(1, int(iid)) + uv(2, int(cnt)))
    return r


def emotion_fix_module(d):
    """1373 -> 1374。原版是"修复模块"打完解锁, 这里直接标记完成。

    ★ 客户端 `logic_event_emotion.OnGetEmotionActFixModuleRes`(proto#8) 收完会
      **再发一次 1381**(重新拉状态), 所以 1374 的 StageList 发空也不影响。
    """
    d["emotion_fix_module"] = True
    return b""


def _ensure_shop_currency(d):
    try:
        ids = _shop_cost_ids()
    except Exception:
        return
    it = d.setdefault("items", {})
    for i in ids:
        # ★★ 货币(1 稀因 / 2 格 / 3 因)不在这儿"补货" ——
        #   每次 load() 都补一次等于**买什么都不要钱**(买完扣掉, 下次读档又满)。
        #   2026-10-15 用户报的「购买稀因商品没消耗稀因」就是这么来的。
        if int(i) in CURRENCY_ITEM_IDS:
            continue
        if int(it.get(str(i), 0)) < SHOP_CURRENCY_FLOOR:
            it[str(i)] = SHOP_CURRENCY_FLOOR


# ★★ 货币的"起始额度" —— 只在**键完全不存在**时补一次(历史存档迁移)。
#   ★ 这和"每次读档补货"是两码事: 花到 0 的键仍然存在(值为 0), 不会再被补上,
#     所以花钱照样花得掉。2026-10-15 用户报「点稀因商品时稀因和因突然归零」,
#     根因是基准档只给了「格」、而我又把 load() 里的货币保底撤了 ⇒ items["1"]/
#     ["3"] 整个键不存在 ⇒ 客户端 logic_store 里查不到 ⇒ 顶栏直接显示 0。
# ★★ 2026-10-17 用户拍板: 三种货币**统一 10000**（原来格是 1,000,000）。
#   起因: `actor` 里那三个字段（`diamond`/`gold`/`copper`）会随登录包发出去，
#   客户端**在收到第一份完整 `ItemList` 之前会先把它们显示出来** ——
#   两边对不上时玩家会看到"顶栏数字突然跳一下"
#   （实测: 格 10000 -> 1000000，用户报「因和稀因变动会导致格的错误变动」）。
#   统一成 10000 就永远不会跳。★ 同 `baseline.json` 的 save.items。
CURRENCY_SEED = {1: 10000, 2: 10000, 3: 10000}


def _seed_currency_items(d):
    it = d.setdefault("items", {})
    changed = False
    for i, n in CURRENCY_SEED.items():
        if str(i) not in it:
            it[str(i)] = n
            changed = True
    return changed


# ★★ 2026-10-15 角色养成补全: 「格」的额度**写在基准档里**（_options.gold），
#   这里不做任何"每次读档补货"。
#   原因: 突破 / 能力升级的消耗第 4 项固定是「格」(item 2), 单档就要 2~15 万,
#   基准档原来给 10000 根本不够; 但如果是 load() 时补货, 就等于
#   **养成永远花不掉钱** —— 和用户报的「购买稀因商品没消耗稀因」是同一类坑。


# ==================== 任务 / 成就 (2026-10-14) ====================
# 协议 (protocol_generated\cs_task_pb.lua + cs_common_pb.lua):
#   CSGetTaskTypeListReq {}                 -> CSGetTaskTypeListRes {1:TypeList(repeated CSTaskType)}
#   CSGetTaskListReq {1:TaskType 2:Param}   -> CSGetTaskListRes  {1:TypeData(CSTaskType)}
#   CSGetTaskAwardReq {1:AwardType 2:Param} -> CSGetTaskAwardRes
#        {1:AwardType 2:Param 3:AwardFlag 4:ItemList(repeated CSGetItem) 5:TaskTypeData}
#   CSTaskNotify {1:TaskList(repeated CSTaskData)}      <- 服务端主动推(1105)
#   CSTaskType {1:TaskType 2:TaskList(repeated CSTaskData) 3:NewHand 4:Daily
#               5:SevenDay 6:ActivityTag 7:ActivityAwardFlag}
#   CSTaskData {1:ID 2:TaskType 3:AwardFlag 5:Progress 6:FinishTime}
#   CSTaskProgress {1:Count 2:FinishCount}
#   CSTaskNewHand  {1:Step 2:AwardFlag}
#   CSTaskDaily    {1:SupplyPoint 2:AwardList(repeated CSTaskDailyAward{1:SupplyPoint 2:AwardFlag})}
#
# 客户端事实 (反汇编 logic_task_main / logic_challenge / view_task_type_*):
#   * logic_task_main KTAB#19 = {nil,1,2,6,8,12} —— 1111 的类型表**只认**这几种
#   * TaskType: 1=主线(新手) 2=日常 4=荣誉 5=成就 6=发展 8=活动 12=周常
#   * AwardFlag: 0=未完成 1=可领取 2=已领取
#   * Progress 在界面上显示成 "Count/FinishCount"
#   * 主线按 dataconfig_new_hand_task[step].taskList 分组, 客户端按 MainTaskInfo.Step
#     过滤出 TaskList[1], 再按本地浏览步 SetStepTaskList() 重建 mainTaskList
#   * DailyTaskInfo.SupplyPoint = 累计供电点, AwardList = 宝箱阈值表
#     (阈值表 = dataconfig_day_task_reward)
#   * 成就(logic_challenge) 自己发 1062 TaskType=5/4, 用 dataconfig_achievement 分组
#   * 领奖: AwardType 4=主线步骤(Param=Step) / 2=日常(Param=任务ID; 2=一键全领)
#           / 3=日常积分宝箱 / 1=单个主线任务
#   * 客户端在 1105 CSTaskNotify 上注册了 RefreshTaskList, 是**唯一**能刷新列表的推送
TASK_TYPE_MAIN = 1
TASK_TYPE_DAILY = 2
TASK_TYPE_NEWBIE = 3
TASK_TYPE_HONOR = 4
TASK_TYPE_ACHIEVE = 5
TASK_TYPE_DEVELOP = 6
TASK_TYPE_ACTIVITY = 8
TASK_TYPE_WEEK = 12

# ★ 1111 回的**任务类型表**。客户端 `logic_task_main.GetTaskTypeListRes`(proto#11)
#   只把 `TaskType` 命中本地白名单的类型存进 TaskList, 而那个白名单是
#   **{1, 2, 6, 8, 12}** —— 我们以前少发了 8(活动), 活动任务那块就一直进不去。
#   8 在 dataconfig_task_base 里有 184 行。
TASK_TYPE_LIST = [1, 2, 6, 8, 12]
DAILY_RECEIVE_ALL = 2               # 日常"一键领取"约定的 Param

# 新手七日任务的"剩余时长"(秒)。用户要求: 始终存在、无时间限制, 直到奖励领完。
# logic_newbie_task.IsInActivityTime() = `LeftTime - (now - 收到时的 now) > 0`,
# 而 GetTaskListRes 又要求 `LeftTime ~= 0` 才肯装数据 —— 所以只能给一个"足够大"
# 的秒数(100 年, 仍在 uint32 内)。副作用: 界面上会显示"剩余 36500 天"。
NEWBIE_LEFT_TIME = 3153600000

AWARD_TYPE_TASK = 1                 # 单个任务
AWARD_TYPE_DAILY = 2                # 日常(任务ID / 2=全领)
AWARD_TYPE_DAILY_BOX = 3            # 日常积分宝箱
AWARD_TYPE_MAIN_STEP = 4            # 主线步骤
AWARD_TYPE_NEWBIE_BOX = 5           # 新手七日进度宝箱 (data->GetProgressAward)
# ★★★ 活动任务「一键全领」（2026-10-16 新增）
#   证据: `logic/event_emotion/view_event_emotion_task_ui.lua` 的 `ClickReceiveAll`
#   (proto#12) 整个函数体就是 `logic_task_main.GetTaskAward(6, 1)`。
#   ⇒ AwardType=6, Param 看着像"活动组"，但**别指望它**（点一次可能就是 1），
#     所以 Param 能对上 `dataconfig_activity.id` 就只领那一组，否则领全部活动任务。
AWARD_TYPE_ACTIVITY_ALL = 6
# ★★★ 活动任务「全清宝箱」（2026-10-16 新增）
#   证据: `logic/task_ui/view_task_type_activity_ui.lua`
#     * `GetCompletedAward`(proto#10) 整个函数体 = `logic_task_main.GetTaskAward(7, 2)`
#       —— **Param 硬编码 2**（就是「活动」页签那组，见 view_task_type_activity_ui
#       的 tag=2）。
#     * `UpdateLeft`(proto#12) 也硬编码 `dataconfig_activity_finish_all_task` 的
#       **id=2**，把 `item_id/item_count` 画成宝箱；`ActivityAwardFlag[2] == 2`
#       → 打上 mask、熄灯（= 已领取）。
#   配置表只有 **id=2 和 id=5** 两行，奖励都是「通用补充协议(item 68) x2」。
AWARD_TYPE_ACTIVITY_FINISH_ALL = 7
# ★★★ 客户端把「活动」这一组写死成 **tag 2**，而且有**两处**:
#   1. `view_task_type_activity_ui.GetTaskList`(proto#8) 开头就是
#        `local t = logic_task_main.TaskList[4][2]` 然后 `for i = 1, #t`
#      —— **`#nil` 会直接抛异常**! 所以 `TaskList[4][2]` **必须在下发时就存在**，
#      不能等界面自己去要（它 `Init` 里先读列表、请求是之后才发的）。
#      ※ 小窝按钮(主页 EventsGroup/XiaowoBtn) 开的就是这个界面 —— 没有它，
#        界面 `Init` 中途抛异常 ⇒ 半开 ⇒ **返回键也失效**（用户 2026-10-16 实测）。
#   2. `view_task_type_activity_ui.UpdateLeft`(proto#12) /
#      `GetCompletedAward`(proto#10) 也用 2（全清宝箱）。
#   ⇒ 1111(任务类型表) 里 type=8 那条**必须带 tag=2**（见 build_task_type_list）。
ACTIVITY_TAB_TAG = 2
ACTIVITY_FINISH_ALL_TAG = ACTIVITY_TAB_TAG   # 同一个 2，客户端两处都写死

# 日志里给人看的名字（bizserver 打 `领任务奖励 AwardType=.. ` 时用）
AWARD_TYPE_LABEL = {
    1: "(单个任务)",
    2: "(一键领取某类)",
    3: "(日常积分宝箱)",
    4: "(主线步骤)",
    5: "(新手进度宝箱)",
    6: "(活动任务一键全领)",
    7: "(活动全清宝箱)",
}

_TASK_ROWS = None
_TASK_BY_ID = None


def _task_rows():
    """dataconfig_task_base 按 type 分组(各自按 id 升序)。"""
    global _TASK_ROWS
    if not _TASK_ROWS:
        import cfgdata
        g = {}
        for r in cfgdata.load("dataconfig_task_base").values():
            if not isinstance(r, dict):
                continue
            t, i = r.get("type"), r.get("id")
            if not isinstance(t, int) or not isinstance(i, int):
                continue
            g.setdefault(t, []).append(r)
        for t in g:
            g[t].sort(key=lambda r: r["id"])
        _TASK_ROWS = g
    return _TASK_ROWS


def _task_by_id():
    global _TASK_BY_ID
    if not _TASK_BY_ID:
        import cfgdata
        _TASK_BY_ID = {int(r["id"]): r
                       for r in cfgdata.load("dataconfig_task_base").values()
                       if isinstance(r, dict) and isinstance(r.get("id"), int)}
    return _TASK_BY_ID


def _task_flags(d):
    return d.setdefault("tasks", {})


def task_flag(d, tid):
    """某任务的领取状态。

    ★ 2026-10-15: 日常(type=2)和发展(type=6)改成**按真实条件**判:
      没达标就是"未完成", 达标才"可领取"。其余类型仍是老行为(默认可领)。
      日常任务每天清零(见 _daily)。
    """
    tid = int(tid)
    row = _task_by_id().get(tid)
    if row is not None and int(row.get("type") or 0) == TASK_TYPE_DAILY:
        if _daily_claimed(d, tid):
            return AWARD_ALLREADYGOT
        cur, need = daily_progress(d, tid)
        return AWARD_CAN_GET if cur >= need else AWARD_NOTFINISH
    if row is not None and int(row.get("type") or 0) == TASK_TYPE_DEVELOP:
        # 领过就是领过; 没领过则按真实条件判(舍友等级会涨, 不能只看有没有记录)
        if _task_flags(d).get(str(tid)) == AWARD_ALLREADYGOT:
            return AWARD_ALLREADYGOT
        cur, need = develop_progress(d, row)
        return AWARD_CAN_GET if cur >= need else AWARD_NOTFINISH
    if row is not None and int(row.get("type") or 0) == TASK_TYPE_HONOR:
        # ★★ 荣誉（2026-10-16 新增）—— `dataconfig_task_base` 里 type=4 的那 35 行
        #   （id 200001..220006，和 `dataconfig_honor` 的 id 完全一致，award_item_list 为空
        #   —— 也就是说"荣誉"就是**没有道具奖励的任务**，荣誉本身就是奖品）。
        #
        #   ★ 默认给 AWARD_ALLREADYGOT(2)：客户端两处都只认这个值 ——
        #     `view_honor_select_ui.PrepareSelectHonorData` 会**跳过 AwardFlag ~= 2 的**，
        #     `view_honor_ui.GetData` 也按它累计 collectedCount ⇒ 给 1(可领取) 等于没解锁。
        #   ★ 想只留一部分：往存档 `tasks` 里写 {"200001": 0} 覆盖即可（0 未完成/1 可领/2 已获得）。
        v = _task_flags(d).get(str(tid))
        return AWARD_ALLREADYGOT if v is None else int(v)
    v = _task_flags(d).get(str(tid))
    return AWARD_CAN_GET if v is None else int(v)


def _set_task_flag(d, tid, flag):
    tid = int(tid)
    row = _task_by_id().get(tid)
    if row is not None and int(row.get("type") or 0) == TASK_TYPE_DAILY:
        st = _daily(d)
        got = set(int(x) for x in st["got"])
        if int(flag) == AWARD_ALLREADYGOT:
            got.add(tid)
        else:
            got.discard(tid)
        st["got"] = sorted(got)
        return
    _task_flags(d)[str(tid)] = int(flag)


def _add_items(d, pairs):
    """把 [(item_id, count)] 加进背包 —— **所有发放道具的地方都必须走这里**。

    ★★ 2026-10-16 用户要求: **舍友合同(func=5) 在"发放的那一刻"就兑现** ——
       直接给角色本身(已拥有则按 repeat_gain 给专属补充协议/迹),
       **合同本身绝不进背包**; 返回的也是**实际发到手的东西**(给 1052 推送/回包用)。

       ★ 为什么必须"一处收口": 以前除了 `_grant_items`, 还有 **6 处**自己
         `d["items"][str(i)] = ...` 直接塞背包(商店 1121 / 商店另一条 / 开箱 /
         dataconfig_item_drop / 签到 / 周常兑换), 全绕过了兑换 ⇒
         情绪商店买的芙斯忒到手还是"舍友合同"(用户实测)。
       ★ 以前只在 `load()` 里兜底兑: 服务端换了但**没告诉客户端**, 背包界面上
         那张合同一直挂着。兜底还在(清老存档), 但新发放一律走这里。
    """
    out = []
    if not pairs:
        return out
    cmap = _contract_role_map()
    for i, n in pairs:
        try:
            i, n = int(i), int(n)
        except Exception:
            continue
        if n <= 0:
            continue
        rid = cmap.get(i) if cmap else None
        if rid is not None:
            for _ in range(n):
                _first, _awards = _obtain_role(d, rid)
                out.extend(_awards)
                if _first:
                    # ★ 第 1 次拿到 -> 记下来, 调用方推 1213 让客户端弹「获得角色」
                    _note_new_role(rid)
            continue
        # ★★ 2026-10-16 用户要求: **月卡礼包(func=8) 也"发放那一刻就开掉"** ——
        #   它就是个礼包(args[1]=天数), 不该在背包里躺着。以前直接塞进 items:
        #   玩家背包里攒了一堆看不见的礼包, 而 `load()` 那条"有礼包就补 30 天"的
        #   兜底会被反复触发 ⇒ 「首次买月卡给了 60 天」。
        #   ⇒ 不进 items, 也不进 out(它不是"到手道具")。天数由这里加。
        _row = _item_by_id().get(i)
        if _row is not None and int(_row.get("func") or 0) == ITEM_FUNC_MONTH_CARD:
            _a = _row.get("args") if isinstance(_row.get("args"), dict) else {}
            _d = _a.get(1, _a.get("1"))
            _mc_add(d, (int(_d) if isinstance(_d, int) and _d > 0
                        else MONTH_CARD_DURATION_DAYS) * n)
            continue
        # ★★ 2026-10-16 用户拍板: 「舾装补给」这类开箱道具(func=4, use_way=2)
        #   **不要自动开** —— 客户端对 use_way=2 不显示「使用」按钮（它是"发放即使用"
        #   类型），但那批是**内测纪念品**，用户要求**原样发进背包留着**。
        #   ⇒ 这里刻意**不加** func=4 分支。（`use_items` 里那条 func=4 还留着，
        #     它现在是"万一被别的方式用到也能开"的兜底，正常流程走不到。）
        it = d.setdefault("items", {})
        it[str(i)] = int(it.get(str(i), 0)) + n
        out.append((i, n))
    return out


def _add_item_pairs(d, plist, times=1):
    """`_item_pairs()` 那种 protobuf 字节列表 -> `_add_items()`。"""
    pairs = []
    for p in (plist or []):
        i = n = 0
        for f, w, v in B.pb_fields(p):
            if f == 1:
                i = v
            elif f == 2:
                n = v
        if i:
            pairs.append((int(i), int(n or 1) * max(1, int(times))))
    return _add_items(d, pairs)


def _grant_items(d, by_id_list):
    """按 dataconfig 的 award_item_list(形如 {'1':{'item_id':x,'item_count':n}})
    发道具, 返回**实际到手**的 [(item_id, count)]。
    ★ 真正干活的是 `_add_items`(舍友合同在那里被换成角色)。
    """
    pairs = []
    if isinstance(by_id_list, dict):
        for k in sorted(by_id_list, key=lambda x: int(x) if str(x).isdigit() else 0):
            e = by_id_list[k]
            if not isinstance(e, dict):
                continue
            i, n = e.get("item_id"), e.get("item_count")
            if not isinstance(i, int):
                continue
            pairs.append((int(i), int(n) if isinstance(n, int) else 1))
    return _add_items(d, pairs)


# ---------------- 主线 ------------
NEW_HAND = None


def _new_hand_steps():
    """{step: row} (dataconfig_new_hand_task)"""
    global NEW_HAND
    if not NEW_HAND:
        import cfgdata
        NEW_HAND = {}
        for r in cfgdata.load("dataconfig_new_hand_task").values():
            if isinstance(r, dict) and isinstance(r.get("step"), int):
                NEW_HAND[int(r["step"])] = r
    return NEW_HAND


def _max_main_step():
    s = _new_hand_steps()
    return max(s) if s else 1


def main_task_step(d):
    """主线当前(最高解锁)步骤。"""
    return int(d.get("task_step") or 1)


def _main_step_done(d):
    """已领取过的最高步骤 (0 = 一个都没领)。"""
    return int(d.get("task_step_done") or 0)


def _main_step_flag(d, step):
    """步骤奖励状态: 领过的=已领取, 当前步骤=可领取, 后面的=未完成。

    ★ 不能只看 `step < 当前步骤` —— 最后一步(max)领完时 step 没法再加 1,
      标志位会一直停在"可领取", 于是能无限重复领(用户实测: 第 6 步领了 4 次)。
      必须单独把"已领到第几步"落盘。"""
    s = int(step)
    if s <= _main_step_done(d):
        return AWARD_ALLREADYGOT
    return AWARD_CAN_GET if s == main_task_step(d) else AWARD_NOTFINISH


# ---------------- 日常 ------------
def _daily_supply(d):
    """累计供电点 = 已领取的日常任务 supply_point 之和。"""
    total = 0
    for row in _task_rows().get(TASK_TYPE_DAILY, []):
        if task_flag(d, row["id"]) == AWARD_ALLREADYGOT:
            sp = row.get("supply_point")
            if isinstance(sp, int):
                total += sp
    return total


def _daily_max_supply():
    """所有日常任务的 supply_point 之和(= 一天能拿到的供电点上限)。"""
    return sum(int(r.get("supply_point") or 0)
               for r in _task_rows().get(TASK_TYPE_DAILY, []))


def _daily_boxes():
    """日常积分宝箱阈值表 -> [(supply_point, row)] (dataconfig_day_task_reward)。

    ★ 这张表只能等客户端跑起来后由 inject_cfgdump 导出。在它出现之前必须回
      **至少一条**(哪怕没有奖励)—— 客户端 view_task_type_daily_ui.GetDailyAwardList
      会直接 LEN(DailyTaskInfo.AwardList), 空/缺字段会崩。占位阈值用"一天能拿到的
      供电点总和", 不是编的奖励, 只是让进度条有个刻度。"""
    import cfgdata
    out = []
    for r in cfgdata.load("dataconfig_day_task_reward").values():
        if not isinstance(r, dict):
            continue
        pt = None
        # ★ 阈值是 finish_point(3/6/9/12/15); 同行的 supply_point 恒为 3, 是
        #   "这一档要涨几个点"(客户端 SetGroupItemState 用它画图标), 不是阈值。
        for key in ("finish_point", "need_point", "need_supply", "supply_point",
                    "point", "id"):
            v = r.get(key)
            if isinstance(v, int):
                pt = v
                break
        if pt is not None:
            out.append((pt, r))
    out.sort(key=lambda x: x[0])
    if not out:
        out = [(_daily_max_supply(), None)]
    return out


def _daily_box_claimed(d):
    """已领取的宝箱阈值集合。"""
    return set(int(x) for x in (d.get("daily_box_got") or []))


def build_daily_ext(d):
    """CSTaskDaily {1:SupplyPoint 2:AwardList}"""
    r = uv(1, _daily_supply(d))
    got = _daily_box_claimed(d)
    for pt, _row in _daily_boxes():
        r += bs(2, uv(1, int(pt))
                + uv(2, AWARD_ALLREADYGOT if int(pt) in got else
                       (AWARD_CAN_GET if _daily_supply(d) >= int(pt)
                        else AWARD_NOTFINISH)))
    return r


# ---------------- 通用构造 ------------
def build_task_data(d, row):
    """CSTaskData {1:ID 2:TaskType 3:AwardFlag 5:Progress 6:FinishTime}"""
    tid = int(row["id"])
    flag = task_flag(d, tid)
    total = row.get("finish_total")
    total = int(total) if isinstance(total, int) and total > 0 else 1
    prog = task_progress(d, row)
    if prog is not None:
        # ★ 日常/发展: 下发**真实进度**, 客户端会显示 "3/5" 这种
        cur, _need = prog
    else:
        cur = total if flag != AWARD_NOTFINISH else 0
    r = uv(1, tid) + uv(2, int(row.get("type") or 0)) + uv(3, flag)
    r += bs(5, uv(1, cur) + uv(2, total))          # Progress{Count, FinishCount}
    if flag == AWARD_ALLREADYGOT:
        r += uv(6, NOW)
    return r


def _activity_row(act_id):
    """dataconfig_activity 的一行(按 id)。"""
    import cfgdata
    for r in cfgdata.load("dataconfig_activity").values():
        if isinstance(r, dict) and r.get("id") == int(act_id):
            return r
    return None


def _activity_task_ids(act_id):
    row = _activity_row(act_id)
    if not row:
        return None
    tl = row.get("task_list")
    if not isinstance(tl, dict):
        return None
    return set(int(v) for v in tl.values() if isinstance(v, int))


_ACT_FINISH_ALL = None


def activity_finish_all_row(tag):
    """`dataconfig_activity_finish_all_task[id == tag]`（没有就 None）。

    ★ 只有 **id=2 / id=5** 两行（各是「通用补充协议 x2」）。
    """
    global _ACT_FINISH_ALL
    if _ACT_FINISH_ALL is None:
        import cfgdata
        _ACT_FINISH_ALL = {
            int(r["id"]): r
            for r in cfgdata.load("dataconfig_activity_finish_all_task").values()
            if isinstance(r, dict) and isinstance(r.get("id"), int)}
    return _ACT_FINISH_ALL.get(int(tag))


def activity_all_claimed(d, tag):
    """该组活动的「全清宝箱」领过没有。存 `d["activity_all_got"] = [tag, ...]`。"""
    try:
        return int(tag) in set(int(x) for x in (d.get("activity_all_got") or []))
    except Exception:
        return False


def build_sevenday(d, tids):
    """CSNewHandSevenDay {1:DayTasks 2:Progress 3:AwardStatus 4:LeftTime 5:Status}

    ★ leftTime 必须非 0 —— logic_newbie_task.GetTaskListRes 里
      `if LeftTime ~= 0 then ... else a2=false; return end`, 给 0 就整个不装数据。
    ★ Status 是 EnumActivityStatus: **Activity_Open = 0**, Activity_Close = 1
      (il2cpp_out\\dump.cs 里的 proto 枚举真实值, 别按名字顺序猜)。
    ★ Progress 的刻度 = "已完成的新手任务数", 阈值来自 dataconfig_sevenday_progress
      的 finish_point(5/10/15/20/25/30/36); 宝箱用 AwardType=5 领
      (logic_newbie_task.GetProgressAward(finish_point) -> GetTaskAwardReq(5, finish_point))
      —— **Param 是 finish_point, 不是宝箱的 id** (2026-10-15 在这上面翻过车)。"""
    r = b""
    day_map = _sevenday_days(tids)          # [(day, [tid, ...]), ...]
    for day, ids in day_map:
        dt = uv(1, int(day))
        for t in ids:
            dt += bs(2, uv(1, int(t)))      # Uint32Id{1:ID}
        r += bs(1, dt)
    done = sum(1 for t in tids
               if task_flag(d, t) != AWARD_NOTFINISH)
    total = len(tids) or 1
    r += bs(2, uv(1, done) + uv(2, total))          # Progress{Count, FinishCount}
    # ★ seven_day_got 里存的是 **finish_point**(和客户端点击时传的 Param 同一个口径),
    #   不是宝箱 id。这里以前拿 id 比, 于是 id=5(=finish_point 25 那一档)永远显示"已领取"。
    got = set(int(x) for x in (d.get("seven_day_got") or []))
    for pid, point, _row in _sevenday_boxes():
        if int(point) in got:
            fl = AWARD_ALLREADYGOT
        elif done >= int(point):
            fl = AWARD_CAN_GET
        else:
            fl = AWARD_NOTFINISH
        r += bs(3, uv(1, int(pid)) + uv(2, fl))     # CSAwardStatus{Id: 宝箱id, AwardFlag}
    r += uv(4, NEWBIE_LEFT_TIME)            # LeftTime = 100 年(等效"无时间限制")
    r += uv(5, 0)                           # Status = Activity_Open
    return r


def _sevenday_days(tids):
    """新手七日任务的分天表 (dataconfig_sevenday_tasks: {day, task_ids})。
    表没 dump 出来时按 id 顺序均分成 7 天(占位, 只为让客户端有东西渲染)。"""
    import cfgdata
    out = {}
    for r in cfgdata.load("dataconfig_sevenday_tasks").values():
        if not isinstance(r, dict):
            continue
        day = r.get("day")
        tl = (r.get("task_ids") or r.get("task_list") or r.get("taskList"))
        if isinstance(day, int) and isinstance(tl, dict):
            out.setdefault(day, []).extend(
                int(v) for v in tl.values() if isinstance(v, int))
    if out:
        return sorted(out.items())
    if not tids:
        return []
    n = max(1, (len(tids) + 6) // 7)
    chunked = [tids[i:i + n] for i in range(0, len(tids), n)]
    return [(i + 1, chunked[i]) for i in range(len(chunked))]


def _sevenday_progress(d):
    """新手七日「累计点数」= 已完成(已领)的新手任务数。

    client 侧的刻度就是它: build_sevenday 把它当 Progress.Count 下发,
    宝箱的 AwardFlag 也是拿它跟 finish_point 比出来的 —— 两边必须同一个口径。"""
    return sum(1 for r in _task_rows().get(TASK_TYPE_NEWBIE, [])
               if task_flag(d, r["id"]) != AWARD_NOTFINISH)


def _sevenday_boxes():
    """dataconfig_sevenday_progress -> [(id, finish_point, row)]"""
    import cfgdata
    out = []
    for r in cfgdata.load("dataconfig_sevenday_progress").values():
        if not isinstance(r, dict):
            continue
        i = r.get("id")
        p = r.get("finish_point")
        if isinstance(i, int) and isinstance(p, int):
            out.append((i, p, r))
    out.sort(key=lambda x: x[0])
    return out


def build_task_type(d, ttype, activity_tag=0, only_ids=None):
    """CSTaskType —— 一种任务类型的全部状态。"""
    ttype = int(ttype)
    r = uv(1, ttype)
    rows = _task_rows().get(ttype, [])
    if only_ids:
        rows = [x for x in rows if int(x["id"]) in only_ids]
    for row in rows:
        r += bs(2, build_task_data(d, row))
    if ttype == TASK_TYPE_MAIN:
        step = main_task_step(d)
        r += bs(3, uv(1, step) + uv(2, _main_step_flag(d, step)))
    elif ttype == TASK_TYPE_DAILY:
        r += bs(4, build_daily_ext(d))
    elif ttype == TASK_TYPE_NEWBIE:
        r += bs(5, build_sevenday(d, [int(x["id"]) for x in rows]))
    if ttype == TASK_TYPE_ACTIVITY:
        # ★ ActivityTag 绝不能是 0/nil: logic_task_main.SetTaskList 的类型 8 分支直接
        #   `TaskList[4][ActivityTag] = {}`, nil 会 "table index is nil" 把整个
        #   SetTaskList 打断(连其它类型一起完蛋)。
        tag = int(activity_tag) or 1
        r += uv(6, tag)
        # ★★★ ActivityAwardFlag = 「这个 tag 的全清宝箱领了没」:
        #   客户端 `SetTaskList` 把它存进 `ActivityAwardFlag[ActivityTag]`,
        #   `view_task_type_activity_ui.UpdateLeft` 就认 **== 2** 为已领取
        #   (打 mask / 熄灯 / 点宝箱改成看详情)。以前永远发 1(可领取),
        #   所以领完也不变灰。2=已领取, 1=未领取。
        r += uv(7, AWARD_ALLREADYGOT if activity_all_claimed(d, tag)
                else AWARD_CAN_GET)
    return r


def build_task_type_list(d):
    """CSGetTaskTypeListRes {1:TypeList(repeated CSTaskType)}

    ★★★ type=8(活动) 这条**必须带 tag=2** —— 原因见 `ACTIVITY_TAB_TAG` 的注释:
      `logic_task_main.GetTaskTypeListRes`(proto#11) 对白名单里的每个类型都会
      `SetTaskList(它)`，而活动界面 `view_task_type_activity_ui.Init` 一上来就读
      `logic_task_main.TaskList[4][2]` 取 `#`。以前这里 `activity_tag` 默认成了 1
      ⇒ `TaskList[4][2]` 是 nil ⇒ 小窝按钮进去的「活动任务」界面初始化抛异常
      ⇒ 界面半开、**返回键失效**（用户 2026-10-16 实测）。
    """
    r = b""
    for t in TASK_TYPE_LIST:
        if t == TASK_TYPE_ACTIVITY:
            r += bs(1, build_task_type(
                d, t, activity_tag=ACTIVITY_TAB_TAG,
                only_ids=_activity_task_ids(ACTIVITY_TAB_TAG)))
            continue
        r += bs(1, build_task_type(d, t))
    return r


def build_task_list(d, ttype, param=0):
    """CSGetTaskListRes {1:TypeData}"""
    ttype = int(ttype)
    if ttype == TASK_TYPE_ACTIVITY:
        ids = _activity_task_ids(param)
        row = _activity_row(param)
        tag = int(row.get("tag") or 1) if row else 1
        return bs(1, build_task_type(d, ttype, activity_tag=tag, only_ids=ids))
    return bs(1, build_task_type(d, ttype))


def honor_ids():
    """全部荣誉 id（`dataconfig_task_base` 里 type=4 的 35 行，升序）。"""
    return [int(r["id"]) for r in _task_rows().get(TASK_TYPE_HONOR, [])]


def build_honor_notify(d):
    """CSTaskNotify{1:TaskList(repeated CSTaskData)} —— **只带 35 条荣誉**。

    ★★ 为什么荣誉要靠"推送"来引导（反汇编 `logic_challenge` 得到的链路）:

      `logic_challenge` 只注册了 **`ECV_CSTaskNotify`**(1105) 一个监听,
      它的 `RefreshDataList`(proto#3) 收到通知后:

        ① `if uv2(成就列表) == nil` 或 `if uv0(荣誉列表) == nil` →
              `GetAllData()` → 发 `CSGetTaskListReq(5)`，然后 **return**
        ② 否则遍历通知里 `TaskType ∈ {4,5}` 的条目;
           遇到 **`TaskType==4 且 AwardFlag==2`** 的 → `GetTaskListReq(4)`，然后 **return**

      而 `GetTaskListReq` 的回包处理器 `GetTaskListRes`(proto#9) 才会调
      `SetTaskList(TypeData)` —— **那才是唯一填充荣誉列表的地方**
      （proto#6: `ttype==4` 时清空并重建 uv0，逐条 insert，
       每条要能在 `dataconfig_task_base` 里查到且 `disable==0`）。

      ⇒ 所以一条通知不够: 第一条只是把客户端"催去"要成就列表(类型 5);
        等类型 5 的回包把 uv2 建起来之后，**再推一条**才会触发 `GetTaskListReq(4)`。
        两次推送分别在 bizserver 的心跳和 1062(TaskType=5) 回包之后。
    """
    return build_task_notify(d, honor_ids())


def build_task_notify(d, tids):
    """CSTaskNotify {1:TaskList(repeated CSTaskData)} —— 只带变了的那几条。"""
    idx = _task_by_id()
    r = b""
    for tid in tids:
        row = idx.get(int(tid))
        if row is not None:
            r += bs(1, build_task_data(d, row))
    return r


# ---------------- 每日任务「真实条件」(2026-10-15) ----------------
# 条件文案是从本地化表里读出来的(docs §8.23)。对应关系:
#   func=1  登录1次                      func=3  通关任意关卡 N 次
#   func=5  完成 N 次舍友进修            func=7  完成 N 次舍友访募
#   func=8  在驾驶舱和舍友互动 N 次      func=11 在沙多集市购买任意商品1次
#   func=15 在百馐餐区烹饪1次            func=30 在离岸贸区线上交易/线下采购
#   func=31 在战术回顾室回顾/协助教学    func=41 在沙多集市收取援外协议1次
#
# ★ 服务端目前只观测得到 3 类(登录 / 通关关卡 / 集市购买); 其余 7 类的玩法
#   (舍友进修、访募、驾驶舱互动、烹饪、离岸贸区、战术回顾室、援外协议)还没实现,
#   所以那些任务现在**永远显示 0/N**。等对应玩法做出来后, 只要在它的事件处理里
#   加一句 `playerdata.bump_daily(d, "role_train")` 就自动接上。
DAILY_TASK_RULES = {
    50001: ("login", 1),            # 登录1次
    50005: ("stage_win", 1),        # 通关任意关卡 N 次
    50006: ("stage_win", 5),
    50007: ("stage_win", 10),
    50009: ("shop_buy", 1),         # 在沙多集市购买任意商品1次
    50002: ("role_train", 1),       # 完成 N 次舍友进修 (玩法未实现)
    50003: ("role_train", 3),
    50004: ("role_train", 5),
    50014: ("role_visit", 1),       # 完成 N 次舍友访募 (玩法未实现)
    50015: ("role_visit", 3),
    50016: ("role_visit", 5),
    50017: ("cockpit_talk", 1),     # 在驾驶舱和舍友互动 (玩法未实现)
    50018: ("cockpit_talk", 3),
    50019: ("cook", 1),             # 在百馐餐区烹饪 (玩法未实现)
    50010: ("trade_online", 1),     # 在离岸贸区线上交易 (玩法未实现)
    50011: ("trade_offline", 1),    # 在离岸贸区线下采购 (玩法未实现)
    50012: ("trade_offline", 2),
    50013: ("trade_offline", 3),
    50020: ("review", 1),           # 在战术回顾室回顾 (玩法未实现)
    50021: ("assist_teach", 1),     # 在战术回顾室协助教学 (玩法未实现)
    50008: ("aid_protocol", 1),     # 在沙多集市收取援外协议 (玩法未实现)
}

_ROLE_CFG = None


def _daily(d):
    """当日任务状态 {date, cnt:{key:次数}, got:[已领任务id]} —— 跨天自动清零。"""
    st = d.get("daily")
    if not isinstance(st, dict) or st.get("date") != _today_str():
        st = {"date": _today_str(), "cnt": {}, "got": []}
        d["daily"] = st
    st.setdefault("cnt", {})
    st.setdefault("got", [])
    return st


def bump_daily(d, key, n=1):
    """记一次事件(登录/通关/购买/…)。★ 以后做宿舍/养成/做菜时在事件处调它。"""
    if not key or n <= 0:
        return
    st = _daily(d)
    st["cnt"][str(key)] = int(st["cnt"].get(str(key), 0)) + int(n)


def daily_progress(d, tid):
    """返回 (当前次数, 需要次数)。没登记规则的任务 -> (0, 1)。"""
    rule = DAILY_TASK_RULES.get(int(tid))
    if rule is None:
        return 0, 1
    key, need = rule
    cur = int(_daily(d)["cnt"].get(str(key), 0))
    return cur, int(need)


def _daily_claimed(d, tid):
    return int(tid) in set(int(x) for x in _daily(d)["got"])


def _role_cfg():
    global _ROLE_CFG
    if not _ROLE_CFG:
        import cfgdata
        _ROLE_CFG = {int(r["id"]): r for r in cfgdata.load("dataconfig_role_config").values()
                     if isinstance(r, dict) and isinstance(r.get("id"), int)}
    return _ROLE_CFG


def develop_progress(d, row):
    """发展任务(type=6): 「N 名品质为 Q 的舍友达到 L 级」。

    finish_args: {3:等级, 7:品质}, finish_total = 需要的名数。
    ★ 品质就是 dataconfig_role_config 的 **`star`**(1=N 2=R 3=SR 4=SSR) ——
      那张表里没有 `quality` 字段。"""
    a = row.get("finish_args") or {}
    # ★ finish_args 是用 `[1] = ...` 写的, 解析出来是**整数键**;
    #   用字符串键查会全取到 0(踩过)。
    def _arg(k):
        v = a.get(k, a.get(str(k)))
        return int(v) if isinstance(v, int) else 0
    try:
        lv, q = _arg(3), _arg(7)
    except Exception:
        return 0, 1
    need = int(row.get("finish_total") or 1)
    if lv <= 0:
        return 0, need
    cnt, cfgs = 0, _role_cfg()
    for rid, rd in (d.get("roles") or {}).items():
        c = cfgs.get(int(rid))
        if c is None or int(c.get("star") or 0) != q:
            continue
        if int(rd.get("level") or 0) >= lv:
            cnt += 1
    return min(cnt, need), need


def task_progress(d, row):
    """任务进度 -> (cur, need); 不适用真实条件的返回 None(沿用"全解锁"老行为)。"""
    t = int(row.get("type") or 0)
    if t == TASK_TYPE_DAILY:
        return daily_progress(d, int(row["id"]))
    if t == TASK_TYPE_DEVELOP:
        return develop_progress(d, row)
    return None


# ---------------- 领奖 ------------
def _claim_one(d, row, gain):
    # ★ 2026-10-15: 必须**只放行"可领取"**。以前只挡 AWARD_ALLREADYGOT,
    #   于是"一键领取"会把没达标的日常任务(舍友进修那种)一起发出去 ——
    #   加了真实条件之后这就是个漏洞。
    if task_flag(d, row["id"]) != AWARD_CAN_GET:
        return False
    _set_task_flag(d, row["id"], AWARD_ALLREADYGOT)
    gain.extend(_grant_items(d, row.get("award_item_list")))
    return True


def claim_task_award(d, award_type, param):
    """处理 CSGetTaskAwardReq。

    返回 (items, changed_tids, scope_type):
      items        -> [(item_id, count)] 本次发的道具
      changed_tids -> 需要推 CSTaskNotify 的任务 id
      scope_type   -> 回包 TaskTypeData 用哪个类型 (None = 不带)
    """
    award_type, param = int(award_type or 0), int(param or 0)
    gain, changed, scope = [], [], None

    # ① 单个任务(主线/日常/其它类型)
    row = _task_by_id().get(param)
    if award_type == AWARD_TYPE_TASK and row is not None:
        if _claim_one(d, row, gain):
            changed.append(param)
        return gain, changed, int(row.get("type") or 0)

    # ② 主线步骤奖励: Param = Step
    if award_type == AWARD_TYPE_MAIN_STEP:
        step = param if param > 0 else main_task_step(d)
        rows = _new_hand_steps().get(step)
        # 已领过 / 超出当前进度 / 没这一步 => 什么都不发(防重复领)
        if rows is None or step > main_task_step(d) \
           or step <= _main_step_done(d):
            return gain, changed, TASK_TYPE_MAIN
        gain.extend(_grant_items(d, rows.get("award_item_list")))
        # 该步骤里的任务一并算作已领取
        for tid in sorted((rows.get("taskList") or {}).values()):
            row2 = _task_by_id().get(int(tid))
            if row2 is not None and _claim_one(d, row2, gain):
                changed.append(int(tid))
        d["task_step_done"] = max(_main_step_done(d), step)
        if main_task_step(d) == step and step < _max_main_step():
            d["task_step"] = step + 1
        return gain, changed, TASK_TYPE_MAIN

    # ③ "一键领取某一类任务": Param = **TaskType**（不是任务 id！）
    #
    # ★ 2026-10-15 修: 以前把 Param 当任务 id / 当魔法值 2 处理, 结果
    #   `view_task_type_develop_ui.ClickReceiveAll` 发的 (2, 6) 掉进了"单个任务"
    #   分支 -> 去领任务 id=6(一个主线任务, 早领过了) -> 什么都不发。
    #   客户端实测: 日常界面发 (2, 2), 养成界面发 (2, 6) —— 都是 TaskType。
    if award_type == AWARD_TYPE_DAILY:
        ttype = int(param)
        rows = _task_rows().get(ttype)
        if not rows:                      # 兜底: 认不出类型就当"日常一键全领"
            ttype = TASK_TYPE_DAILY
            rows = _task_rows().get(ttype, [])
        for row2 in rows:
            if _claim_one(d, row2, gain):
                changed.append(int(row2["id"]))
        return gain, changed, ttype

    # ④ 日常积分宝箱
    if award_type == AWARD_TYPE_DAILY_BOX:
        got = set(int(x) for x in _daily_box_claimed(d))
        supply = _daily_supply(d)
        for pt, row2 in _daily_boxes():
            if int(pt) not in got and supply >= int(pt):
                got.add(int(pt))
                gain.extend(_grant_items(d, (row2 or {}).get("award_item_list")))
        d["daily_box_got"] = sorted(got)
        return gain, changed, TASK_TYPE_DAILY

    # ⑤ 新手七日进度宝箱: AwardType=5, Param = **finish_point**(5/10/15/20/25/30/36)
    #
    # ★ 2026-10-15 修: 以前拿 Param 去比宝箱的 **id**(1..7)，但客户端发的是 finish_point ——
    #   `view_newbie_task_ui` proto#12:
    #       addClickCallback_NoScale(节点, logic_newbie_task.GetProgressAward, config.finish_point)
    #   `logic_newbie_task.GetProgressAward(p)` -> `GetTaskAwardReq(5, p)`
    #   后果（用户实测："点击只会领取累计点数25的奖励，而且可以重复领取"）:
    #     * 只有 finish_point=5 的那个宝箱"碰巧"撞上 id=5(=finish_point 25 那一档)才有反应，
    #       所以领到的永远是 25 档的奖励；其余宝箱点了没反应。
    #     * 而且没有"已领过"判断 -> 点几次发几次。
    if award_type == AWARD_TYPE_NEWBIE_BOX:
        boxes = _sevenday_boxes()
        hit = None
        for _i, point, row2 in boxes:           # 主判据: finish_point
            if int(point) == param:
                hit = (int(point), row2)
                break
        if hit is None:                         # 兜底: 万一以后客户端改成发 id
            for i2, point, row2 in boxes:
                if int(i2) == param:
                    hit = (int(point), row2)
                    break
        if hit is None:
            return gain, changed, TASK_TYPE_NEWBIE
        point, row2 = hit
        got = set(int(x) for x in (d.get("seven_day_got") or []))
        if point in got:                        # 已领过 -> 一件都不发
            return gain, changed, TASK_TYPE_NEWBIE
        if _sevenday_progress(d) < point:       # 累计点数没到 -> 不发
            return gain, changed, TASK_TYPE_NEWBIE
        got.add(point)
        d["seven_day_got"] = sorted(got)
        gain.extend(_grant_items(d, (row2 or {}).get("award_item_list")))
        save(d)
        return gain, changed, TASK_TYPE_NEWBIE

    # ⑥ 活动任务「一键全领」(AwardType=6)
    #   ★ 客户端的 `view_event_emotion_task_ui.ClickReceiveAll` = `GetTaskAward(6, 1)`。
    #   ★ Param 若是合法的 `dataconfig_activity.id`(等价于 tag) 就只领那一组,
    #     否则(0/对不上)把**所有可领的活动任务**一次领完 —— 客户端那边有二次筛选。
    if award_type == AWARD_TYPE_ACTIVITY_ALL:
        ids = _activity_task_ids(param) if param > 0 else None
        for row2 in _task_rows().get(TASK_TYPE_ACTIVITY, []):
            if ids is not None and int(row2["id"]) not in ids:
                continue
            if _claim_one(d, row2, gain):
                changed.append(int(row2["id"]))
        if changed:
            save(d)
        return gain, changed, TASK_TYPE_ACTIVITY

    # ⑦ 活动任务「全清宝箱」(AwardType=7)
    #   ★ 客户端 `view_task_type_activity_ui.GetCompletedAward` = `GetTaskAward(7, 2)`
    #     —— Param 写死 2。奖励在 `dataconfig_activity_finish_all_task[id == tag]`。
    #   ★ 领完要把 `ActivityAwardFlag[tag]` 顶成 2，否则客户端那个宝箱
    #     永远停在「可领取」的外观（mask/灯不会变）。见 build_task_type。
    #   ★ Param 对不上就退回客户端写死的 2，免得点了没反应。
    if award_type == AWARD_TYPE_ACTIVITY_FINISH_ALL:
        tag = int(param) if int(param) > 0 else ACTIVITY_FINISH_ALL_TAG
        row2 = activity_finish_all_row(tag)
        if row2 is None or activity_all_claimed(d, tag):
            return gain, changed, TASK_TYPE_ACTIVITY
        got = set(int(x) for x in (d.get("activity_all_got") or []))
        got.add(tag)
        d["activity_all_got"] = sorted(got)
        gain.extend(_grant_items(d, {1: {"item_id": int(row2.get("item_id") or 0),
                                         "item_count": int(row2.get("item_count") or 1)}}))
        save(d)
        return gain, changed, TASK_TYPE_ACTIVITY

    # ⑧ 兜底: 当作任务 ID 领
    if row is not None:
        if _claim_one(d, row, gain):
            changed.append(param)
        return gain, changed, int(row.get("type") or 0)

    return gain, changed, None


def build_task_award_res(d, award_type, param, gain, changed, scope):
    """CSGetTaskAwardRes"""
    r = uv(1, int(award_type or 0)) + uv(2, int(param or 0))
    r += uv(3, AWARD_ALLREADYGOT if gain else AWARD_NOTFINISH)
    if gain:
        for i, n in gain:
            r += bs(4, uv(1, int(i)) + uv(2, int(n)))     # CSGetItem
    if scope:
        r += bs(5, build_task_type(d, scope))
    return r


# ==================== 社交 (好友 / 分享 / 助战) ====================
# 离线环境没有真实好友服务, 这一块按用户要求"直接解锁":
#   * 好友列表/好友申请/推荐  -> 回空列表(不崩, 界面显示"没有好友")
#   * 分享抽卡领奖           -> 直接发奖 (CSRecruitShareAward 1305/1306)
#   * 支援奖励               -> 直接发奖 (CSGetSupportReward 1315/1316)
#   * 支援角色               -> 用玩家**自己的角色**顶替好友的助战位
#   * 成就/荣誉本身已经全部按"可领取"下发, 不再受社交条件限制
#
# ⚠️ 奖励数值(道具 3 = 稀因)没有配置表可依 —— 原本是原服务端的硬编码数据,
#    客户端只负责显示 Response 里的 ItemList。这里给的是**我们自己定的值**。
SOCIAL_SHARE_REWARD = [(3, 100)]        # 分享一次: 稀因 x100
SOCIAL_SUPPORT_REWARD = [(3, 300)]      # 支援奖励: 稀因 x300
SHARE_AWARD_TIMES = 99                  # 还能分享领奖几次
SUPPORT_POS_COUNT = 3                   # 助战位数量


def _grant_pairs(d, pairs):
    """[(item_id, count)] -> 落进背包, 返回**实际到手**的列表(给 1052/1065 用)。
    ★ 走 `_add_items`: 舍友合同在这里也会被换成角色(见那边的注释)。
    """
    return _add_items(d, pairs)


def build_share_award_status(d):
    """CSGetShareAwardStatusRes {1:LeftAwardTimes}"""
    return uv(1, int(d.get("share_award_left", SHARE_AWARD_TIMES)))


def claim_share_award(d):
    """分享抽卡结果领奖 -> CSRecruitShareAwardRes {1:ItemList}"""
    left = int(d.get("share_award_left", SHARE_AWARD_TIMES))
    if left <= 0:
        return []
    d["share_award_left"] = left - 1
    return _grant_pairs(d, SOCIAL_SHARE_REWARD)


def claim_support_reward(d):
    """CSGetSupportRewardRes {1:RewardList}"""
    return _grant_pairs(d, SOCIAL_SUPPORT_REWARD)


def build_support_role(d):
    """CSGetSupportRoleRes {1:SupportRoleList 2:SupportCount 3:RewardList}

    CSSupportRole{1:Pos 2:Role(CSRoleData) 3:PosState}"""
    roles = sorted(d["roles"].items())[:SUPPORT_POS_COUNT]
    r = b""
    for i, (rid, rd) in enumerate(roles, 1):
        r += bs(1, uv(1, i) + bs(2, build_role(rid, rd)) + uv(3, 1))
    r += uv(2, len(roles))
    for i, n in SOCIAL_SUPPORT_REWARD:
        r += bs(3, uv(1, int(i)) + uv(2, int(n)))
    return r


# ==================== 邮件 (2026-10-15) ====================
# 协议 (protocol_generated\cs_mail_pb.lua + cs_common_pb.lua):
#   CSMailGetListReq {1:Page 2:MaxCount} -> CSMailGetListRes {1:More 2:MailList(repeated CSMailData)}
#   CSMailReceiveReq {1:GotType 2:MailIdList(repeated Uint64Id)}
#        -> CSMailReceiveRes {1:ItemList(repeated CSItemPair)}
#   CSMailDeleteReq  {1:DelType 2:MailId 3:Location} -> CSMailDeleteRes(空)
#   CSMailData {1:MailId(uint64) 2:MailType 3:ReceiveTm(int64) 4:ExpireTm(int64) 5:State
#               6:Title 7:Content 8:ItemList(repeated CSItemPair) 9:SpecialMail
#               10:Sender 11:Location}
#
# 枚举真实值 (il2cpp_out\dump.cs, 别按名字猜):
#   EnmMailState    EMS_Init=0 EMS_New=1 EMS_Got=2 EMS_Delete=3 EMS_Expire=4
#   EnmMailLocation CommonList=0 CommonAndCollectionList=1 CollectionList=2
#
# 数据源 dataconfig_system_mail: 178 行, 字段 {id, mail_type, title, content, sender,
#   award_item_list, expire(秒), func/args(触发条件), special}
#   —— 原服是"条件满足才发", 我们没法评估条件, 所以**一次性全塞进邮箱**:
#      * 有奖励的 16 封 -> State=EMS_New(可领取), 会进红点
#      * 没奖励的(系统公告) -> State=EMS_Got(直接算已读, 不刷红点)
#      * special=1 的归到"收藏"分页(Location=2)
MAIL_STATE_NEW = 1
MAIL_STATE_GOT = 2
MAIL_STATE_DELETE = 3
MAIL_LOC_COMMON = 0
MAIL_LOC_ALL = 1
MAIL_LOC_COLLECTION = 2

_MAIL_ROWS = None


def _mail_rows():
    global _MAIL_ROWS
    if _MAIL_ROWS is None:
        import cfgdata
        out = []
        for r in cfgdata.load("dataconfig_system_mail").values():
            if isinstance(r, dict) and isinstance(r.get("id"), int):
                out.append(r)
        out.sort(key=lambda r: int(r["id"]))
        _MAIL_ROWS = out
    return _MAIL_ROWS


def _mail_by_id():
    return {int(r["id"]): r for r in _mail_rows()}


def _mail_store(d):
    return d.setdefault("mails", {})


def _mail_loc(row):
    return MAIL_LOC_COLLECTION if row.get("special") else MAIL_LOC_COMMON


def ensure_mails(d):
    """首次进游戏把系统邮件塞进邮箱(只做一次, 之后按存档状态走)。

    ★ 判空而不是只看标志位: 万一标志位是 true 但邮箱是空的(存档被覆盖/迁移过),
      只看标志位会导致**永久空邮箱**。"""
    if d.get("mails_seeded") and _mail_store(d):
        return
    d["mails_seeded"] = True
    st = _mail_store(d)
    now = NOW
    for row in _mail_rows():
        k = str(int(row["id"]))
        if k in st:
            continue
        al = row.get("award_item_list")
        has_award = isinstance(al, dict) and len(al) > 0
        st[k] = {"state": MAIL_STATE_NEW if has_award else MAIL_STATE_GOT,
                 "receive": now,
                 "expire": now + int(row.get("expire") or 2592000)}
    save(d)


def _build_mail(d, mid, row, s):
    """CSMailData"""
    r = uv(1, int(mid))                                     # MailId(uint64)
    r += uv(2, int(row.get("mail_type") or 1))              # MailType
    r += uv(3, int(s.get("receive") or NOW))                # ReceiveTm(int64)
    r += uv(4, int(s.get("expire") or 0))                   # ExpireTm(int64)
    r += uv(5, int(s.get("state") or MAIL_STATE_NEW))        # State
    # Title/Content 是**语言 key**(如 "17342"), 客户端自己 toText
    if row.get("title"):
        r += bs(6, str(row["title"]))
    if row.get("content"):
        r += bs(7, str(row["content"]))
    for p in _item_pairs(row.get("award_item_list")):
        r += bs(8, p)                                       # ItemList(CSItemPair)
    r += uv(9, int(row.get("special") or 0))                # SpecialMail
    if row.get("sender"):
        r += bs(10, str(row["sender"]))                     # Sender
    r += uv(11, _mail_loc(row))                             # Location
    return r


def build_mail_list(d, page=1, max_count=200):
    """CSMailGetListRes {1:More 2:MailList}

    ★ More 只有 0/1: 为 1 时客户端会自动再请求下一页(MaxCount=200)。
      178 封一页装得下, 所以恒为 0。"""
    ensure_mails(d)
    rowmap = _mail_by_id()
    st = _mail_store(d)
    items = []
    for k, s in st.items():
        row = rowmap.get(int(k))
        if row is None:
            continue
        if int(s.get("state") or 0) in (MAIL_STATE_DELETE, 4):
            continue
        items.append((int(k), row, s))
    items.sort(key=lambda x: (-int(x[2].get("receive") or 0), -x[0]))
    mc = int(max_count or 200) or 200
    lo = max(0, (int(page or 1) - 1) * mc)
    sel = items[lo:lo + mc]
    r = uv(1, 1 if lo + len(sel) < len(items) else 0)
    for mid, row, s in sel:
        r += bs(2, _build_mail(d, mid, row, s))
    return r


def claim_mails(d, mail_ids, got_type=0):
    """CSMailReceiveRes —— 领奖并标记 EMS_Got, 返回 [(item, count)]

    ★ GotType != 0 = 客户端的「一键领取」。
      实测(2026-10-15)客户端 一键领取 发出的 1082 **body 只有 2 字节** —— 只带
      GotType, MailIdList 是**空的**; 它把"哪些邮件能领"交给服务端决定
      (view_mail_ui.OnClickBtnGetALL -> logic_mail.SendMailReceiveReq(1, {}, cb))。
      以前这里只看 MailIdList => 一键领取 ids=[] => 一件东西都不发,
      响应里 ItemList 为空 => 结算回调 res.ItemList 也是空 => **不弹"获得物品"**。
    """
    ensure_mails(d)
    rowmap = _mail_by_id()
    st = _mail_store(d)
    if int(got_type or 0) != 0:
        mail_ids = [int(k) for k, s in st.items()
                    if int(s.get("state") or 0) == MAIL_STATE_NEW]
    gain = []
    for mid in mail_ids:
        k = str(int(mid))
        s, row = st.get(k), rowmap.get(int(mid))
        if s is None or row is None:
            continue
        if int(s.get("state") or 0) == MAIL_STATE_GOT:
            continue                                      # 已经领过
        s["state"] = MAIL_STATE_GOT
        gain.extend(_grant_items(d, row.get("award_item_list")))
    save(d)
    return gain


# ★ 批量删邮件(邮箱「清理邮件」按钮)服务端直接关掉 —— 第二道防线。
#   客户端那侧已经有 inject_mailclear.py 让按钮弹「此功能已被禁用」, 但只要玩家
#   还在跑旧 Lua(没重启游戏)就拦不住。2026-10-15 就是这样: 客户端的
#   OnClicKBtnClear -> SendMailDeleteReq(1, 0, 0) 一把删掉了 107 封普通邮件
#   (含 16 封带奖励的), 默认分页当场空白。
#   系统邮件是**一次性全塞**进去的, 删了就没了 => 服务端也拒绝。
#   想放开: 把这个常量改成 True (客户端补丁也要一起 --revert)。
ENABLE_BULK_MAIL_DELETE = False


def delete_mails(d, mail_id=0, location=MAIL_LOC_ALL):
    """CSMailDeleteReq —— 单封(MailId>0) 或按分页全删。"""
    ensure_mails(d)
    rowmap = _mail_by_id()
    st = _mail_store(d)
    if int(mail_id or 0) <= 0 and not ENABLE_BULK_MAIL_DELETE:
        return 0                                # 批量删除已禁用, 一封都不动
    if int(mail_id or 0) > 0:
        k = str(int(mail_id))
        if k in st:
            st[k]["state"] = MAIL_STATE_DELETE
        save(d)
        return 1
    n = 0
    for k, s in st.items():
        row = rowmap.get(int(k))
        if row is None or int(s.get("state") or 0) == MAIL_STATE_DELETE:
            continue
        if int(location) != MAIL_LOC_ALL and _mail_loc(row) != int(location):
            continue
        st[k]["state"] = MAIL_STATE_DELETE
        n += 1
    save(d)
    return n


# ---------------- 周常兑换 (2026-10-15) ----------------
# 协议 (dump.cs 里的真值, 不是猜的):
#   1106 CSGetTaskExchangeReq(空)            -> 1107 CSGetTaskExchangeRes {1:ExchangeList}
#   1108 CSTaskExchangeReq{1:ExchangeId 2:Count} -> 1109 Res{1:ExchangeId 2:Count 3:ItemList}
#   1194 CSGetTaskExchangeNotify {1:ExchangeList}  —— 服务端推送; 客户端收到后**重新发 1106**
#   CSTaskExchange{1:ExchangeId 2:Count(已换次数) 3:MaxCount(上限)}
#
# ★ 客户端在**登录必备请求**里(enter_necessary_infos)就调 GetTaskExchange() 发 1106。
#   以前服务端走默认分支回空 body => ExchangeList 空 => 周常界面的兑换入口**直接不显示**
#   (用户报"这个按钮似乎被隐藏了")。
EXCHANGE_ITEM = 60                   # 兑换用的代币(物品 60)
EXCHANGE_ERR_NO_NUM = 1026           # Sys_Exchange_Code_Has_No_Num   代币不够
EXCHANGE_ERR_DONE = 1021             # Sys_Exchange_Code_Has_Exchange 已经换满了
EXCHANGE_ERR_TOO_MORE = 1028         # Sys_Exchange_Code_Too_More     数量非法

_EXCHANGE_ROWS = None


def _exchange_rows():
    """dataconfig_task_exchange 的 13 行, 按 sort_weight 排序(客户端就是按它排的)"""
    global _EXCHANGE_ROWS
    if _EXCHANGE_ROWS is None:
        import cfgdata
        out = []
        for r in cfgdata.load("dataconfig_task_exchange").values():
            if isinstance(r, dict) and isinstance(r.get("exchange_id"), int):
                out.append(r)
        out.sort(key=lambda r: int(r.get("sort_weight") or 0))
        _EXCHANGE_ROWS = out
    return _EXCHANGE_ROWS


def _exchange_by_id():
    return {int(r["exchange_id"]): r for r in _exchange_rows()}


def exchange_counts(d):
    return d.setdefault("task_exchange", {})


def _pairs_times(u_log_a, k=1):
    """配置里的 award/cost_item_list -> [(item_id, count*k), ...]"""
    out = []
    if not isinstance(u_log_a, dict):
        return out
    for key in sorted(u_log_a):
        e = u_log_a[key]
        if isinstance(e, dict) and isinstance(e.get("item_id"), int):
            n = e.get("item_count")
            n = int(n) if isinstance(n, int) else 1
            out.append((int(e["item_id"]), n * int(k)))
    return out


def build_task_exchange_list(d):
    """CSGetTaskExchangeRes —— Count 取存档, MaxCount 取配置的 exchange_count"""
    cnt = exchange_counts(d)
    r = b""
    for row in _exchange_rows():
        eid = int(row["exchange_id"])
        r += bs(1, uv(1, eid)
                + uv(2, int(cnt.get(str(eid), 0)))
                + uv(3, int(row.get("exchange_count") or 0)))
    return r


def task_exchange(d, exchange_id, count=1):
    """执行兑换 -> (回包 body, gain, spend, err)。err != 0 时什么都不改。"""
    row = _exchange_by_id().get(int(exchange_id))
    if row is None:
        return b"", [], [], EXCHANGE_ERR_TOO_MORE
    mx = int(row.get("exchange_count") or 0)
    cnt = exchange_counts(d)
    key = str(int(exchange_id))
    done = int(cnt.get(key, 0))
    want = int(count or 1)
    if want <= 0:
        return b"", [], [], EXCHANGE_ERR_TOO_MORE
    if mx > 0 and done >= mx:
        return b"", [], [], EXCHANGE_ERR_DONE
    if mx > 0 and done + want > mx:
        want = mx - done                        # 超量就砍到上限(不报错)
    spend = _pairs_times(row.get("cost_item_list"), want)
    it = d.setdefault("items", {})
    for i, n in spend:
        if int(it.get(str(i), 0)) < n:
            return b"", [], [], EXCHANGE_ERR_NO_NUM
    for i, n in spend:
        it[str(i)] = int(it.get(str(i), 0)) - n
    gain = _add_items(d, list(_pairs_times(row.get("award_item_list"), want)))  # ★ 统一发放
    cnt[key] = done + want
    save(d)
    body = uv(1, int(exchange_id)) + uv(2, done + want)
    for i, n in gain:
        body += bs(3, uv(1, i) + uv(2, n))
    return body, gain, spend, 0


# ---------------- 货币兑换 (cmd 1154 CSExchangeMoneyReq)  2026-10-17 ----------------
# CSExchangeMoneyReq{1:ItemID 2:Count 3:TargetID} -> CSExchangeMoneyRes{1:GameData}
#
# ★ Count 是**要消耗的源货币数量**。
#   反汇编 `logic/currency_exchange/view_currency_exchange_normal` 里那个字段就叫
#   `costSourceNum`，请求就是拿它当 Count 发出去的。
# ★ 兑换比率在**客户端本地表** `dataconfig_currency_exchange`：
#       { id, sources_id, target_id, exchange_type, ratio = {[1]=源, [2]=目标} }
#   实测两行:  id=5: 1(稀因) -> 3(因),  ratio 1:1
#              id=4: 3(因)   -> 75,     ratio 160:1
#   ⇒ 消耗 Count 个源货币, 得到 Count * ratio[2] // ratio[1] 个目标货币。
#
# ★★ 这条命令以前**根本没实现** ⇒ 服务端回空包 ⇒ 客户端「稀因和因的数量都没变化」
#    （用户 2026-10-17 实测；bizserver.log 里能看到 `<- cmd=1154 ... -> cmd=1155`）。
# ★ 源/目标都是**货币条目 id**（1=稀因 / 2=格 / 3=因），和 `items` 是同一套键
#   ⇒ 直接扣/加 `items`，**不要碰 `actor.gold`**（顶栏读的是 `items`）。
_CURRENCY_EXCHANGE = None


def _currency_exchange_map():
    """{(源id, 目标id): (ratio_src, ratio_dst)}"""
    global _CURRENCY_EXCHANGE
    if _CURRENCY_EXCHANGE is not None:
        return _CURRENCY_EXCHANGE
    import cfgdata
    out = {}
    try:
        for r in cfgdata.load("dataconfig_currency_exchange").values():
            if not isinstance(r, dict):
                continue
            ra = r.get("ratio") if isinstance(r.get("ratio"), dict) else {}
            try:
                src = int(r.get("sources_id"))
                dst = int(r.get("target_id"))
                r1 = int(ra.get(1, ra.get("1")))
                r2 = int(ra.get(2, ra.get("2")))
            except (TypeError, ValueError):
                continue
            if r1 > 0 and r2 > 0 and src != dst:
                out[(src, dst)] = (r1, r2)
    except Exception:
        out = {}
    _CURRENCY_EXCHANGE = out
    return out


def currency_exchange(d, item_id, count, target_id):
    """货币兑换 -> (回包 body, gain, spend, err)。err != 0 时什么都不改。"""
    try:
        item_id, count, target_id = int(item_id), int(count), int(target_id)
    except (TypeError, ValueError):
        return b"", [], [], RET_ITEM_PARAM_ERROR
    if count <= 0 or item_id == target_id:
        return b"", [], [], RET_ITEM_PARAM_ERROR
    pair = _currency_exchange_map().get((item_id, target_id))
    if pair is None:
        return b"", [], [], RET_ITEM_PARAM_ERROR
    r1, r2 = pair
    it = d.setdefault("items", {})
    if int(it.get(str(item_id), 0)) < count:
        return b"", [], [], RET_ITEM_NOT_ENOUGH
    give = count * r2 // r1
    if give <= 0:
        # 换不出一个整数量 ⇒ 报参数错，别白白扣掉玩家的钱
        return b"", [], [], RET_ITEM_PARAM_ERROR
    it[str(item_id)] = int(it.get(str(item_id), 0)) - count
    gain = _add_items(d, [(target_id, give)])      # ★ 统一发放咽喉
    save(d)
    return bs(1, build_gamedata(d)), gain, [(item_id, count)], 0


# ---------------- 剧情关 / 剧情回看 (2026-10-15) ----------------
# 三条协议（dump.cs 真值）:
#   1099 CSGetStagePlotReq(空)        -> 1100 Res{1:PlotList(repeated Uint32Id{1:ID})}  已看过的剧情
#   1101 CSStagePlotFinishReq{1:PlotId} -> 1102 Res{1:PlotId}                            上报已看
#   1249 CSStagePlotAwardReq{1:Stage(CSStageBase)} -> 1250 Res{1:FinishData, 2:Stage}    剧情关「领取奖励」
#
# 剧情关 = dataconfig_stage_minor 里 `is_plot_type = True` 的 12 关（不打仗，只有对话/CG）。
# 1250 的 FinishData 和战斗结算 1076 同一个形状，客户端拿 DropList 弹「获得物品」。
_STAGE_MINOR = None


def _stage_minor():
    """(type, chapter, main_id, minor_id) -> `dataconfig_stage_minor` 行。

    ★★ 初霜与春风 的关卡**同时挂两个键**: 真的 `(4,4011,m,n)` 和冒充的 `(1,4011,m,n)`
      —— 客户端现在按 type=1 上报（见 SWORD_CHAPTERS 的注释），服务端两边都要能查到。
    """
    global _STAGE_MINOR
    if _STAGE_MINOR is None:
        import cfgdata
        _STAGE_MINOR = {}
        for r in cfgdata.load("dataconfig_stage_minor").values():
            if not isinstance(r, dict):
                continue
            k = (r.get("type"), r.get("chapter"), r.get("main_id"), r.get("minor_id"))
            if all(isinstance(v, int) for v in k):
                _STAGE_MINOR[k] = r
        # ★ 别名键（初霜与春风）: (4,ch,mn,mi) -> (1,ch,mn,mi) 以及 minor 2/3 -> 1
        for (t, c, mn, mi), r in list(_STAGE_MINOR.items()):
            if c not in SWORD_CHAPTERS:
                continue
            a = stage_alias_type(t, c)
            b = stage_alias_minor(t, c, mi)
            if a != t or b != mi:
                _STAGE_MINOR.setdefault((t, c, mn, b), r)
                _STAGE_MINOR.setdefault((a, c, mn, b), r)
    return _STAGE_MINOR


def _plot_store(d):
    st = d.setdefault("plots", [])
    return st if isinstance(st, list) else []


def build_plot_list(d):
    """CSGetStagePlotRes —— 已看过的剧情 id 列表（客户端据此决定"播还是跳过"）"""
    r = b""
    for pid in sorted(set(int(x) for x in _plot_store(d))):
        r += bs(1, uv(1, pid))
    return r


def mark_plot_finished(d, plot_id):
    """CSStagePlotFinishRes {1:PlotId}"""
    pid = int(plot_id or 0)
    if pid <= 0:
        return b""
    st = _plot_store(d)
    if pid not in set(int(x) for x in st):
        st.append(pid)
        save(d)
    return uv(1, pid)


def plot_stage_award(d, tp, ch, mn, mi):
    """剧情关「领取奖励」-> (回包 body, gain)

    ★ 不记 `stage_win` 计数: 剧情关不算战斗, 不该算进每日任务「通关任意关卡 N 次」。
    ★ 首通才发奖(按 d["stages"] 判), 再点一次只是空回包, 不重复发。
    """
    key = (int(tp), int(ch), int(mn), int(mi))
    skey = "%d:%d:%d:%d" % key
    body_stage = uv(1, key[0]) + uv(2, key[1]) + uv(3, key[2]) + uv(4, key[3])
    row = _stage_minor().get(key)
    gain = []
    # ★ row 也要参与判断: 查不到的关卡不许报首通(否则会写脏 stages 并回 Score=3)
    first = row is not None and int((d.get("stages") or {}).get(skey, 0)) <= 0
    if first:
        gain = _grant_items(d, row.get("output_items_list"))
        d.setdefault("stages", {})[skey] = 3
        save(d)
    fd = uv(1, 3 if first else 0)                       # FinishData.Score
    for i, n in gain:                                   # FinishData.DropList(repeated CSDropItem)
        fd += bs(3, uv(1, int(i)) + uv(2, int(n)) + uv(3, 0))
    return bs(1, fd) + bs(2, body_stage), gain


# ★★★ 关卡掉落的**三层**（`output_type`，2026-10-16 反汇编定案）:
#
#     output_type == 1   **首次三星奖励** —— 第一次拿到 3 星（完美通关）时给, 只给一次
#     output_type == 2   **首次通关奖励** —— 第一次通关时给, 只给一次
#     output_type == 3   **每次掉落**     —— 每次通关 / 每次扫荡都发（4 号也归这里）
#
#   ★ 证据（不是猜的）: `logic/chapter_ui/view_level_detail_ui.lua` 的 proto#7
#     把 `output_items_list` 按 `output_type` 分三桶, 然后:
#         Score < 3  ⇒ 显示 output_type==1 那桶
#         Score == 0 ⇒ 显示 output_type==2 那桶
#         总是       ⇒ 显示 output_type==3 那桶
#     而语言表连续三条正是 **4108「首次通关」/ 4109「首次三星」/ 4110「活动掉落」**。
#   ★★ 所以「54011 的稀因 x80」是**首次三星奖励**, 不是"首通奖励" —— 玩家 2026-10-16
#      亲口说「80稀因是3星通关奖励」, 反汇编完全对上了。之前的"两层"模型是错的。
#   ★ 以前服务端**什么都不发**（1076 的 FinishData 只有 Score、没有 DropList），
#     只有剧情关领奖 `plot_stage_award()` 和扫荡在发 ⇒ 正常打关是空的。
#   ⚠️ `probability` 字段目前**忽略**（都按必掉处理）—— 用户没提, 先这样。
STAGE_DROP_FIRST_3STAR_TYPE = 1     # 首次三星奖励
STAGE_DROP_FIRST_PASS_TYPE = 2      # 首次通关奖励
STAGE_DROP_EVERY_TYPE = 3           # 每次都掉（4 也按这个算）


def _stage_drop_split(row):
    """把 `output_items_list` 拆成 `(every, first_pass, first_3star)`。

    每个都是 `[(item_id, count)]`。见上面 `output_type` 的说明。
    """
    every, f_pass, f_3star = [], [], []
    lst = row.get("output_items_list") if isinstance(row, dict) else None
    if not isinstance(lst, dict):
        return every, f_pass, f_3star
    for k in sorted(lst, key=lambda x: int(x) if str(x).isdigit() else 0):
        e = lst[k]
        if not isinstance(e, dict):
            continue
        i, n = e.get("item_id"), e.get("item_count")
        if not isinstance(i, int):
            continue
        pair = (int(i), int(n) if isinstance(n, int) else 1)
        ot = int(e.get("output_type") or 0)
        if ot == STAGE_DROP_FIRST_3STAR_TYPE:
            f_3star.append(pair)
        elif ot == STAGE_DROP_FIRST_PASS_TYPE:
            f_pass.append(pair)
        else:
            every.append(pair)
    return every, f_pass, f_3star


# ★★★ 关卡星数 = **基地剩余血量**（玩家 2026-10-16 拍板）
#
#   这游戏是《明日方舟》那一类塔防: **基地有血量, 每漏一个敌人 -1**。
#   客户端把基地剩余血量通过 `CSStageBattleData{1:RemainHp}` 送上来
#   （见 `logic/battle_ui/logic_battle_main.lua` 的 `StageFinishReq` proto#11），
#   所以 **星数直接就是 `RemainHp`**（0 星=基地被打掉=失败, 3 星=完美通关）。
#
#   ★ 客户端里**没有任何**判星条件（语言表 8801 条没有条件文案、
#     配置表没有 star 条件字段、`challenge_ids` 指向的是画作名）——
#     `CSFinishData.Score` 完全是服务端说了算, 这条规则是我们定的。
STAGE_STAR_MAX = 3          # 最高星数（完美通关）
STAGE_HP_FALLBACK = 10      # 关卡行里读不到 pass_monster 时的兜底上限


def stage_max_hp(row):
    """这一关的**基地血量上限** = `dataconfig_stage_minor.pass_monster`。

    ★★★ 2026-10-16 实测: 玩家打 `1:1000:9:1` 漏 1 个, 客户端送上来 `RemainHp = 9`,
      而那一关的 `pass_monster = 10` ⇒ 10 - 1 = 9 ✓ **对上了**。
      分布: 323 关是 10 / 28 关是 3 / 14 关是 5 / 2 关是 2 / 8 关是 99（新手关）。
    ★ 之前我以为上限是 3、直接 `min(hp, 3)` —— 那是**瞎封顶**:
      上限 10 的关漏 1~7 个都会被算成 3 星, 玩家一眼就看出来了。
    """
    v = row.get("pass_monster") if isinstance(row, dict) else None
    return int(v) if isinstance(v, int) and v > 0 else STAGE_HP_FALLBACK


def stage_score(ftype, remain_hp, max_hp):
    """基地剩余血量 -> 星数（0..STAGE_STAR_MAX）。

    ★★★ 公式（玩家 2026-10-16 拍板）: **`星 = max(1, floor(剩余 / 上限 × 3))`**
        - 上限 10: 10→3, 9→2, 7→2, 6→1, 1→1（漏 1 个就掉 1 星）
        - 保底 1 星: 客户端是拿 `Score > 0` 判胜负的, 给 0 会被它当失败
    ★ 退出 / 重试 / 基地被打掉 ⇒ 0 星（走 `stage_finish_is_win` 的失败分支）。
    """
    if ftype != ESFT_NORMAL:
        return 0
    hp = int(remain_hp or 0)
    if hp <= 0:
        return 0
    m = max(1, int(max_hp or 0))
    s = (hp * STAGE_STAR_MAX) // m
    return max(1, min(STAGE_STAR_MAX, s))


def stage_score_of(stage, ftype, remain_hp):
    """按关卡坐标查表拿上限再算星（bizserver 直接调这个）。"""
    tp, ch = int(stage.get(1) or 0), int(stage.get(2) or 0)
    mn, mi = int(stage.get(3) or 0), int(stage.get(4) or 0)
    row = _stage_minor().get((tp, ch, mn, mi))
    return stage_score(ftype, remain_hp, stage_max_hp(row))


def stage_best_score(d, tp, ch, mn, mi):
    """存档里这关的历史最好星数（没打过 = 0）。"""
    return int((d.get("stages") or {}).get("%d:%d:%d:%d" % (tp, ch, mn, mi), 0) or 0)


def stage_note_score(d, tp, ch, mn, mi, score):
    """把本次星数写进 `stages`（**只留最好的**）。返回旧值。"""
    key = "%d:%d:%d:%d" % (tp, ch, mn, mi)
    st = d.setdefault("stages", {})
    old = int(st.get(key, 0) or 0)
    if int(score) > old:
        st[key] = int(score)
    return old


def sweep_unlocked(d, tp, ch, mn, mi):
    """这关能不能扫荡 —— **只有三星（完美通关）过才开**（用户 2026-10-16 拍板）。

    ★ 扫荡开关是**服务端告诉客户端**的, 两个地方都要发:
        `CSFinishData{6:IsUnlockSweep}`（1076 回包）和
        `CSStageScore{5:IsSweepUnlock}`（1068 关卡列表）。
      客户端 `logic_chapter.UpStageScore(.., Score, IsSweepUnlock)` 把它写进
      `StageDict[key].IsSweepUnlock`, 扫荡界面就看这个位。我们以前**从来没发过**,
      所以扫荡入口永远不开。
    """
    return stage_best_score(d, tp, ch, mn, mi) >= STAGE_STAR_MAX


def stage_clear_award(d, stage, score):
    """**正常打关**的掉落。返回 `(gain, info)`。

    `gain` = 本次真正要发的 `[(item_id, count)]`；
    `info` = `{"first_pass": bool, "first_3star": bool, "every": [...], ...}`（打日志用）。

    ★ 规则（见 `_stage_drop_split` 上面的 `output_type` 说明）:
        每次通关          -> `output_type == 3` 那层
        第一次通关        -> 额外发 `output_type == 2` 那层
        第一次拿到 3 星    -> 额外发 `output_type == 1` 那层（例: 54011 的稀因 x80）
    ★★ **必须先算掉落再写 `stages`** —— 首通/首三星都是拿旧值判的，顺序反了就永远不发。
    """
    tp, ch = int(stage.get(1) or 0), int(stage.get(2) or 0)
    mn, mi = int(stage.get(3) or 0), int(stage.get(4) or 0)
    row = _stage_minor().get((tp, ch, mn, mi))
    info = {"first_pass": False, "first_3star": False, "every": [], "got_pass": [],
            "got_3star": [], "old": 0}
    if row is None:
        return [], info
    old = stage_best_score(d, tp, ch, mn, mi)
    first_pass = old <= 0
    first_3 = int(score) >= STAGE_STAR_MAX and old < STAGE_STAR_MAX
    every, once_pass, once_3 = _stage_drop_split(row)
    # ★ `_add_items` 吃 [(id, count)]，是**唯一咽喉**（会走 func=5/8 的自动使用）
    gain = list(_add_items(d, every))
    got_pass = list(_add_items(d, once_pass)) if first_pass else []
    got_3 = list(_add_items(d, once_3)) if first_3 else []
    gain += got_pass + got_3
    info.update({"first_pass": first_pass, "first_3star": first_3,
                 "every": every, "got_pass": got_pass, "got_3star": got_3,
                 "old": old})
    return gain, info


def build_finish_data(score, gain, unlock_sweep=False):
    """CSFinishData { 1:Score 2:ActorExp 3:DropList 4:PlayerExp 5:NextBattleUID
                     6:IsUnlockSweep 7:ReverseScore }

    ★ DropList 在 **field 3**；`CSSweepFinishData` 的 DropList 在 **field 2** —— 别搞混。
    ★★ `IsUnlockSweep` 在 **field 6**（bool）—— 扫荡入口靠它, 以前完全没发过。
    """
    fd = uv(1, int(score))
    for i, n in gain:
        fd += bs(3, uv(1, int(i)) + uv(2, int(n)) + uv(3, 0) + uv(4, 0))
    if unlock_sweep:
        fd += uv(6, 1)
    return fd


# ---------------- 关卡扫荡 (2026-10-16 新增) ----------------
#
# 协议（从 `cs_stage_pb.lua` / `cs_common_pb.lua` 重建）:
#   1089 CSGetStageSweepReq {}                -> CSGetStageSweepRes{
#        1:State 2:Stage(CSStageBase) 3:UnitTime 4:Count 5:CountDown
#        6:AwardList(repeated CSDropItem) 7:IntellectBack }
#   1091 CSStageSweepReq { 1:Stage 2:Count }  -> CSStageSweepRes {}   （空包）
#   1095 CSFinishStageSweepReq {}             -> CSFinishStageSweepRes{
#        1:FinishData(CSSweepFinishData) 3:GameData }
#   1093 CSStopStageSweepReq {}               -> CSStopStageSweepRes{
#        1:FinishData 2:GameData }
#   1239 CSGetStageSweepGroupReq { 1:stage }  -> CSGetStageSweepGroupRes{1:RoleList(repeated Uint32Id)}
#
# ★★★ `CSSweepFinishData` 的 **`DropList` 是 field 2**（`CSFinishData` 是 field 3，别照抄！）:
#       1:Stage 2:DropList(repeated CSDropItem) 3:IntellectBack 4:ActorExp 5:FinishCount
#     `CSDropItem {1:ItemId 2:ItemCount 3:DropType 4:Power}`
#
# ★★ `State` 是 `enum_level_select.SweepState`（从常量表里挖出来的）:
#       0 = SweepBefore（没在扫荡）  2 = Sweeping（扫荡中）  3 = SweepAfter（扫完待领取）
#     客户端 `IsSweeping()` 判的就是 `State ~= SweepBefore`。
#
# ★ 奖励 = 该关的 `dataconfig_stage_minor[..].output_items_list` 全量 ——
#   和剧情关领奖 `plot_stage_award()` 用的是同一份数据、同一种发法。
# ★ 能源: **用户拍板 10 点/次，和正常打关一样**（不用表里的 sweep_intellect）。
SWEEP_UNIT_TIME = 3            # 每次扫荡耗时(秒)。原版更长, 这里给短一点, 观感还是"倒计时"
SWEEP_INTELLECT_PER = 10       # 每关消耗（= STAGE_INTELLECT_COST）
SWEEP_STATE_BEFORE = 0
SWEEP_STATE_SWEEPING = 2
SWEEP_STATE_AFTER = 3
SWEEP_MAX_COUNT = 999

# ★ 扫荡失败时回给客户端的 `EnmRetCode`（从 dump.cs 的 EnmRetCode 里抄的名）
RET_STAGE_NOT_CLEAR = 131        # STAGE_NOT_CLEAR —— 「关卡未通关」
RET_STAGE_NOT_FOUND_MINOR = 115  # STAGE_NOT_FOUND_MINOR
RET_STAGE_SWEEP_ARGS_INVALID = 128   # STAGE_SWEEP_ARGS_INVALID（已经在扫荡了）


def _stage_minor_of(d):
    """当前扫荡的关卡行（`dataconfig_stage_minor`），没有就 None。"""
    sw = d.get("sweep")
    if not isinstance(sw, dict):
        return None
    return _stage_minor().get((int(sw.get("type") or 0), int(sw.get("chapter") or 0),
                               int(sw.get("main_id") or 0), int(sw.get("minor_id") or 0)))


def sweep_status(d, now=None):
    """返回 (state, done, left_sec)。

    ★ 扫荡是**挂机倒计时**: 开始记 start_ts, 每 SWEEP_UNIT_TIME 秒完成一关。
      全部完成 → State=SweepAfter(3), 否则 Sweeping(2)。没扫荡 → SweepBefore(0)。
    """
    sw = d.get("sweep")
    if not isinstance(sw, dict):
        return SWEEP_STATE_BEFORE, 0, 0
    now = int(now if now is not None else time.time())
    cnt = max(0, int(sw.get("count") or 0))
    unit = max(1, int(sw.get("unit_time") or SWEEP_UNIT_TIME))
    el = max(0, now - int(sw.get("start_ts") or now))
    done = min(cnt, el // unit)
    if done >= cnt and cnt > 0:
        return SWEEP_STATE_AFTER, cnt, 0
    left = cnt * unit - el
    return SWEEP_STATE_SWEEPING, done, max(0, left)


def _sweep_drop_pairs(d, times=1):
    """当前扫荡关卡的掉落 × times。

    ★ 只发 `output_type == 3` 那层（每次都掉的部分）。
      能扫荡就说明这关**已经三星过** ⇒ 「首次通关」和「首次三星」那两层早就发掉了,
      不该再发。`sweep_start` 也把没三星的关挡在外面了。
    """
    row = _stage_minor_of(d)
    if not isinstance(row, dict):
        return []
    every, _p, _3 = _stage_drop_split(row)
    return [(i, n * max(1, int(times))) for i, n in every]


def _csdropitems(pairs):
    """repeated CSDropItem{1:ItemId 2:ItemCount 3:DropType 4:Power}"""
    return [(int(i), int(n), 0, 0) for i, n in pairs]


def build_sweep_info(d):
    """CSGetStageSweepRes —— 客户端 1089 问"现在扫荡到哪了"。"""
    st, done, left = sweep_status(d)
    r = uv(1, st)
    sw = d.get("sweep") if isinstance(d.get("sweep"), dict) else None
    if st != SWEEP_STATE_BEFORE and sw is not None:
        r += bs(2, uv(1, int(sw.get("type") or 0)) + uv(2, int(sw.get("chapter") or 0)) +
                    uv(3, int(sw.get("main_id") or 0)) + uv(4, int(sw.get("minor_id") or 0)))
        r += uv(3, int(sw.get("unit_time") or SWEEP_UNIT_TIME))
        r += uv(4, int(sw.get("count") or 0))
        r += uv(5, int(left))
        for i, n, dt, pw in _csdropitems(_sweep_drop_pairs(d, int(sw.get("count") or 0))):
            r += bs(6, uv(1, i) + uv(2, n) + uv(3, dt) + uv(4, pw))
        r += uv(7, 0)                       # IntellectBack
    return r


def sweep_start(d, stage, count):
    """开始扫荡。返回 **(ok, 说明, ret_code)**。

    ★ 先扣能源（10 × count），不够就直接拒绝（ret=116）。
    ★★ **只有三星（完美通关）过的关卡才能扫荡**（用户 2026-10-16 拍板）——
      「没打过的关卡只能手打；扫荡只有完美通关才能开启」。
      判定看 `d["stages"]` 里的**最好星数**（和服务端判星用的是同一份记录），
      没三星就回 131 `STAGE_NOT_CLEAR`。
    """
    if isinstance(d.get("sweep"), dict) and sweep_status(d)[0] != SWEEP_STATE_BEFORE:
        return False, "已经在扫荡中了", RET_STAGE_SWEEP_ARGS_INVALID
    tp, ch = int(stage.get(1) or 0), int(stage.get(2) or 0)
    mn, mi = int(stage.get(3) or 0), int(stage.get(4) or 0)
    row = _stage_minor().get((tp, ch, mn, mi))
    if row is None:
        return False, "关卡 %s/%s/%s/%s 不在表里" % (tp, ch, mn, mi), RET_STAGE_NOT_FOUND_MINOR
    if not sweep_unlocked(d, tp, ch, mn, mi):
        return (False, "关卡 %d:%d:%d:%d 还没三星通关(最好 %d 星), 不能扫荡"
                % (tp, ch, mn, mi, stage_best_score(d, tp, ch, mn, mi)),
                RET_STAGE_NOT_CLEAR)
    cnt = max(1, min(SWEEP_MAX_COUNT, int(count or 1)))
    need = SWEEP_INTELLECT_PER * cnt
    a = d.setdefault("actor", {})
    have = int(a.get("intellect") or 0)
    if have < need:
        return False, "能源不足(有 %d, 需要 %d)" % (have, need), RET_STAGE_NOT_ENOUGH_INTELLECT
    a["intellect"] = have - need
    d["sweep"] = {"type": tp, "chapter": ch, "main_id": mn, "minor_id": mi,
                  "count": cnt, "unit_time": SWEEP_UNIT_TIME,
                  "start_ts": int(time.time())}
    save(d)
    return True, "开始扫荡 %s/%s/%s/%s x%d, 扣能源 %d" % (tp, ch, mn, mi, cnt, need), 0


def sweep_claim(d, only_done=False):
    """结算扫荡。返回 (回包 body, gain)。

    ★ `only_done=True`（玩家中途停止）只发**已经跑完的**那几关；
      正常完成（客户端在倒计时结束时发 1095）发全部。
    ★ 首次通关也会被记进 `stages` —— 扫荡等于帮你把这关打了。
    """
    sw = d.get("sweep")
    if not isinstance(sw, dict):
        return b"", []
    st, done, _left = sweep_status(d)
    times = int(done if only_done else (sw.get("count") or 0))
    times = max(1, times) if times > 0 else 0
    tp, ch = int(sw.get("type") or 0), int(sw.get("chapter") or 0)
    mn, mi = int(sw.get("main_id") or 0), int(sw.get("minor_id") or 0)

    gain = []
    if times > 0:
        # ★ 能扫荡就说明这关**已经三星过**（`sweep_start` 挡的）⇒ 只发"每次都掉"那层。
        gain = _add_items(d, _sweep_drop_pairs(d, times))
        # ★ 扫荡等于帮你把这关打了：保住原有的星级（不降级），并记一次"通关任意关卡"
        stage_note_score(d, tp, ch, mn, mi, STAGE_STAR_MAX)
        d.setdefault("chapters", {})["%d:%d" % (tp, ch)] = 1
        try:
            bump_daily(d, "stage_win")      # 日常任务 50005/6/7
        except Exception:
            pass

    d.pop("sweep", None)
    save(d)

    fd = bs(1, uv(1, tp) + uv(2, ch) + uv(3, mn) + uv(4, mi))      # CSSweepFinishData.Stage
    for i, n, dt, pw in _csdropitems(gain):                        # ...DropList = **field 2**
        fd += bs(2, uv(1, i) + uv(2, n) + uv(3, dt) + uv(4, pw))
    fd += uv(3, 0)                                                 # IntellectBack
    fd += uv(5, times)                                             # FinishCount
    return bs(1, fd), gain


def build_sweep_group(d, stage):
    """CSGetStageSweepGroupRes{1:RoleList(repeated Uint32Id)}

    ★ 扫荡界面里选队伍用的"可用于该关的舍友"。这里回**玩家拥有的全部角色**,
      客户端自己会按关卡限制筛。
    """
    r = b""
    for rid in sorted(int(x) for x in (d.get("roles") or {}).keys()):
        r += bs(1, uv(1, rid))
    return r


# ---------------- 怪物图鉴 (1070/1072) + 角色互动 (1129) ----------------
#
# ★ 两个都是"客户端说、服务端点头"的轻量协议：
#   1070 CSStageDiscoverMonReq{1:MonId}   -> CSStageDiscoverMonRes {}   （空包）
#        —— 客户端在战斗里遇到新怪就报一次，服务端记下来即可。
#   1072 CSGetStageDiscoverMonListReq {}  -> CSGetStageDiscoverMonListRes{
#                                               1:MonIdList(repeated Uint32Id) ...}
#        client: `SetMonsterInfo(res.MonIdList)`（反汇编 proto#10 实证）
#   1129 CSRoleInteractReq{1:ID 2:SceneID 3:DialogID} -> CSRoleInteractRes {}（空包）
#        —— **对话内容全在客户端**（`GetDialogData`/`RandomDialogDatas`），
#           服务端只要回 SUCCESS，客户端拿到就什么都不做（proto#3 只有失败才打日志）。


def record_monster(d, mon_id):
    """记一个"已发现的怪物"。返回是否是新发现的。"""
    try:
        v = int(mon_id or 0)
    except Exception:
        return False
    if v <= 0:
        return False
    st = d.setdefault("monsters", [])
    if v in st:
        return False
    st.append(v)
    save(d)
    return True


def build_monster_list(d):
    """CSGetStageDiscoverMonListRes { 1: MonIdList(repeated Uint32Id) }"""
    r = b""
    for mid in sorted(int(x) for x in (d.get("monsters") or [])):
        r += bs(1, uv(1, mid))
    return r


def record_interact(d, role_id, scene_id, dialog_id):
    """记一次角色互动（只留个计数，方便以后做红点/成就）。

    ★ 回包是**空包**，所以这里记不记都不影响客户端。
    """
    try:
        st = d.setdefault("interact", {})
        k = "%s:%s" % (int(role_id or 0), int(scene_id or 0))
        st[k] = int(st.get(k, 0)) + 1
        return True
    except Exception:
        return False


# ---------------- 使用物品 (1050 -> 1051) ----------------
# dataconfig_itembase.use_way (EnmItemUseWay) =
#   0 CanNotUse | 1 ManualUse(仓库里能点「使用」) | 2 AutoUse | 3 AppUse
# 实测: use_way == 1 的 47 件物品, func 恰好只有 3 / 9 / 10 / 11 / 16 五种。
USE_WAY_CANNOT = 0
USE_WAY_MANUAL = 1

ITEM_FUNC_ENERGY = 3         # 能源恢复装置: args[1] = 恢复点数 (能源 就是 actor.intellect)
# ★★ 2026-10-16 新增: 开箱类道具 —— args[1] = `dataconfig_item_drop` 的 id。
#   实测 27 个: 「舾装补给N」/「舾装晋升补给N」(4001..4064)。它们的 award_item_list
#   是空的, 给什么全写在 drop 表里。用户报「有描述但打不开」就是这个 func 没人管。
ITEM_FUNC_DROP_BOX = 4
ITEM_FUNC_PKG_SELECT = 9     # 自选礼包:     args[1]=可选个数 args[2]=package_id
ITEM_FUNC_PKG_ALL = 10       # 全开礼包:     args[1]=package_id
ITEM_FUNC_PKG_RANDOM = 11    # 随机礼包:     args[1]=抽取个数 args[2]=package_id
ITEM_FUNC_PRIVATE_MSG = 16   # 私域通讯(玩法未实现 -> 只拒绝, 不吞道具)

# 一次请求最多开多少个 —— 防止客户端滑块拉满时循环爆掉
USE_COUNT_CAP = 9999

_ITEM_IDX = None
_RNG = None


def _item_by_id():
    """dataconfig_itembase 按 **行内 id** 建索引。

    ★ 绝对不能写成 `cfgdata.load("dataconfig_itembase")[item_id]` ——
      dump 出来的 Lua 表是**顺序数组**(键 1..N 是行号), 不是 id 索引。
      itembase 共 514 行, 其中 **476 行的键 ≠ 行内 id** (键 39 的行 id 是 59,
      之后整体错位 20)。直接按 id 取会取到**另一件物品**。
      这个坑是全表性的: system_mail 178/178、stage_minor 375/375、
      shop_goods 339/426 全都对不上。"""
    global _ITEM_IDX
    if _ITEM_IDX is None:
        import cfgdata
        _ITEM_IDX = {}
        for r in cfgdata.load("dataconfig_itembase").values():
            if isinstance(r, dict) and isinstance(r.get("id"), int):
                _ITEM_IDX[r["id"]] = r
    return _ITEM_IDX


_PKG_IDX = None
_DROP_IDX = None        # dataconfig_item_drop 的缓存, 见 _item_drops()


# ★★ drop 表的三种语义（2026-10-16 从 27 个 func=4 箱子逆出来的）:
#     type=1 全给    —— 每条 {item_id: 道具id, item_count: N, item_weight: 0}
#     type=2 随机    —— 按 item_weight 抽一条（权重 1000/5000/10000 那种）
#     type=3 嵌子包  —— 每条 {item_id: **子包 id**, item_count: **子包重复次数**}
#   ★★ 最容易搞错的是 type=3 的 `item_count`：它不是数量，是**把这个子包整体发几遍**。
#      例: drop 30041(箱子 4026) = { 30022 x1, 30023 x1 }，而 30022 = {30001 x2, ...}
#      ⇒ 30001 那 10 件道具要发 2 遍。
#   ★ type=1 里 `item_weight` 全是 0 就是"都要"；非 0 的我们仍按全给处理
#     （那些 drop 不在 func=4 箱子的链路上，见自检里的报告）。
DROP_TYPE_ALL = 1
DROP_TYPE_RANDOM = 2
DROP_TYPE_PACK = 3
_DROP_MAX_DEPTH = 6


def _expand_drop(did, rnd=None, seen=(), depth=0):
    """把一个 drop id 递归展开成 [(item_id, count)]。

    ★ 27 个 func=4 箱子里有 **12 个是套娃**，最深 3 层（4020..4026 那批）。
    ★ 有环保护（drop 10003 里就直接引用了自己）。
    """
    if depth > _DROP_MAX_DEPTH or did in seen:
        return []
    d = _item_drops().get(did)
    if not isinstance(d, dict):
        return []
    lst = d.get("args")
    if not isinstance(lst, dict):
        return []
    t = int(d.get("type") or DROP_TYPE_ALL)
    seen2 = seen + (did,)
    keys = sorted(lst, key=lambda x: int(x) if str(x).isdigit() else 0)
    out = []

    if t == DROP_TYPE_RANDOM:
        pool = [(e.get("item_id"), int(e.get("item_count") or 1),
                 int(e.get("item_weight") or 0))
                for k in keys for e in [lst[k]] if isinstance(e, dict)]
        pool = [x for x in pool if isinstance(x[0], int)]
        if not pool:
            return []
        total = sum(w for _i, _c, w in pool)
        pick = pool[0] if total <= 0 else None
        if pick is None:
            r = (rnd.randint(1, total) if rnd is not None else 1)
            acc = 0
            for i, c, w in pool:
                acc += w
                if r <= acc:
                    pick = (i, c, w)
                    break
        iid, cnt = pick[0], pick[1]
        if iid in _item_by_id():
            return [(iid, cnt)]
        return [(i, n) for i, n in _expand_drop(iid, rnd, seen2, depth + 1)]

    for k in keys:
        e = lst[k]
        if not isinstance(e, dict):
            continue
        iid = e.get("item_id")
        n = int(e.get("item_count") or 1)
        if not isinstance(iid, int):
            continue
        if iid in _item_by_id():
            out.append((iid, n))
        elif iid in _item_drops():
            # ★ type=3: n 是"子包发几遍"; type=1: 同样按重复次数处理（通常为 1）
            sub = _expand_drop(iid, rnd, seen2, depth + 1)
            out.extend([(i, c * n) for i, c in sub])
    return out


def _item_drops():
    """{drop_id: 整行} —— `dataconfig_item_drop`。

    ★★ 存的是**整行**（含 `type` 和 `args`），不是只存 `args` ——
      `_expand_drop` 要靠 `type` 决定怎么发（1 全给 / 2 随机 / 3 嵌子包）。
      踩过：一开始只存 `args`，于是 `_expand_drop` 里再 `d.get("args")` 拿到 None，
      27 个箱子一个都展不开。
    """
    global _DROP_IDX
    if _DROP_IDX is None:
        import cfgdata
        _DROP_IDX = {}
        for r in cfgdata.load("dataconfig_item_drop").values():
            if not isinstance(r, dict):
                continue
            did = r.get("id")
            if isinstance(did, int) and isinstance(r.get("args"), dict):
                _DROP_IDX[did] = r
    return _DROP_IDX


def _item_packages():
    """{package_id: package_item_list}

    package_item_list 的结构是 {键: {item_id, item_count, weight}} —— 和
    `award_item_list` **完全同构**, 所以可以直接喂 `_grant_items`。"""
    global _PKG_IDX
    if _PKG_IDX is None:
        import cfgdata
        _PKG_IDX = {}
        for r in cfgdata.load("dataconfig_item_package").values():
            if not isinstance(r, dict):
                continue
            pid, lst = r.get("package_id"), r.get("package_item_list")
            if isinstance(pid, int) and isinstance(lst, dict):
                _PKG_IDX[pid] = lst
    return _PKG_IDX


def _pkg_keys(pkg):
    """包内条目的配置顺序(键 1..n)。客户端的"位置下标"就是这个顺序。"""
    return sorted((k for k in pkg if isinstance(pkg.get(k), dict)),
                  key=lambda k: int(k) if str(k).isdigit() else 0)


def _pkg_take(pkg, positions):
    """按位置下标(1 起)取条目, 返回 {新键: 条目}。"""
    keys = _pkg_keys(pkg)
    out = {}
    for n, pos in enumerate(positions, 1):
        i = int(pos) - 1
        if 0 <= i < len(keys):
            out[str(n)] = pkg[keys[i]]
    return out


def _pkg_scaled(pkg, times):
    """整包 × times (全开礼包用) —— 不循环, 直接乘, 免得一次开 9999 个卡死。"""
    out = {}
    for n, k in enumerate(_pkg_keys(pkg), 1):
        e = pkg[k]
        c = e.get("item_count")
        out[str(n)] = {"item_id": e.get("item_id"),
                       "item_count": (int(c) if isinstance(c, int) else 1) * times}
    return out


def _pkg_random(pkg, draw, rnd):
    """按 weight 抽 draw 个**不重复**条目。

    实测: 确定性包(自选/全开)的 weight 全是 0, 随机包(func=11 用的
    13~21 / 37)全是 100 —— 所以"全 0 就等概率"这条兜底不会误伤。"""
    pool = _pkg_keys(pkg)
    out = {}
    n = 0
    while pool and n < draw:
        ws = [max(0, int((pkg[k].get("weight") or 0))) for k in pool]
        tot = sum(ws)
        if tot <= 0:
            pick = rnd.choice(pool)
        else:
            x = rnd.randrange(tot)
            acc = 0
            pick = pool[-1]
            for k, w in zip(pool, ws):
                acc += w
                if x < acc:
                    pick = k
                    break
        n += 1
        out[str(n)] = pkg[pick]
        pool.remove(pick)
    return out


def intellect_cap(d):
    """能源上限 = dataconfig_account_level[等级].intellect (1级120, 每级+1)。
    与 build_actor 里写进 `DailyIntellectRecover` 的那个 120 是同一个数。"""
    lv = int((d.get("actor") or {}).get("level") or 1)
    try:
        import cfgdata
        for r in cfgdata.load("dataconfig_account_level").values():
            if isinstance(r, dict) and int(r.get("level") or 0) == lv:
                return int(r.get("intellect") or 120)
    except Exception:
        pass
    return 120


# ---------------- 能源自然恢复 ----------------
# ★ 恢复间隔**故意取 1 分钟** —— 这是用户拍板的设计选择, 不是"没找到配置只好猜"。
#   原话: 「一个单机游戏不应该让愿意一下午打通的玩家干等太久」。
#
#   参考: 原版游戏里「体力自然恢复：5分钟恢复1点体力」, 按 DailyIntellectRecover=120
#   反推是 12 分钟/点。**别看到 5/12 就以为是"正确值"改回去** —— 这里是私服,
#   节奏按用户的意思来。
#
#   想调就改这一个数(单位: 秒)。客户端那边不需要跟着改:
#   倒计时是服务端算好通过 CSActorData{4} 和 1067 下去的。
ENERGY_REGEN_SECONDS = 60


def energy_countdown(d, now=None):
    """距下一点能源恢复还有多少秒(0..ENERGY_REGEN_SECONDS)。"""
    a = d.get("actor") or {}
    now = int(now or time.time())
    if int(a.get("intellect") or 0) >= intellect_cap(d):
        return ENERGY_REGEN_SECONDS
    ts = int(a.get("intellect_ts") or 0)
    if ts <= 0:
        return ENERGY_REGEN_SECONDS
    left = ENERGY_REGEN_SECONDS - (now - ts) % ENERGY_REGEN_SECONDS
    return max(1, min(ENERGY_REGEN_SECONDS, left))


def settle_energy(d, now=None):
    """按经过的时间结算能源自然恢复。返回 **(能源变没变, 存档要不要写, 距下一点秒数)**。

    ★ 客户端**不会自己往上加** —— 实测 `view_common_power_ui.UpdatePower` 只读
      `lua_data_center.GetIntellect()`, 而且只挂 `INTELLECT_CHANGE` 事件;
      没有任何本地计时器。所以必须由服务端**主动推** 1067 CSDataNotify。
      好在客户端每 ~5 秒一个心跳, 每个请求都结算一次就够及时了
      (见 bizserver.handle 里 pd = playerdata.load() 之后那段)。

    ★★ 第二个返回值(存档要不要写)是**独立**的, 不能拿第一个顶替 ——
      起表那一次只改 `intellect_ts`、能源没变, 如果这时不落盘,
      下次 load() 又是 0 => 又当"第一次" => **永远停在起表, 一点都不会涨**。
      (2026-10-15 实测踩到: 等了 65 秒能源纹丝不动。)
    """
    a = d.setdefault("actor", {})
    now = int(now or time.time())
    cap = intellect_cap(d)
    cur = int(a.get("intellect") or 0)
    ts = int(a.get("intellect_ts") or 0)
    dirty = False
    if ts <= 0 or ts > now:               # 第一次 / 时间倒流: 起表
        ts = now
        dirty = True
    if cur >= cap:
        # 满了或**超额**(用道具突破上限) -> 暂停自然恢复。
        # 表只在"已经隔了一整个周期"时才往前推, 免得满着的时候每个请求都写盘。
        if now - ts >= ENERGY_REGEN_SECONDS:
            ts = now
            dirty = True
        a["intellect_ts"] = ts
        return False, dirty, ENERGY_REGEN_SECONDS
    gain = (now - ts) // ENERGY_REGEN_SECONDS
    if gain <= 0:
        a["intellect_ts"] = ts
        return False, dirty, energy_countdown(d, now)
    new = min(cap, cur + gain)
    a["intellect"] = new
    ts = now if new >= cap else ts + gain * ENERGY_REGEN_SECONDS
    a["intellect_ts"] = ts
    return True, True, energy_countdown(d, now)


# EnmDataType: 0=Init 1=Intellect 2=Intellect_CountDown 3=Level 4=Exp
#              5=SupplyPoint 6=Intellect_Recover
DATA_KEY_INTELLECT = 1
DATA_KEY_INTELLECT_COUNTDOWN = 2


def build_data_notify(key, value):
    """CSDataNotify{1:Key(EnmDataType) 2:Value}

    ★ 反汇编 logic_player_data.SetUpPlayerData(proto#11) 实证:
        Key == ERDT_Intellect(1)           -> lua_data_center.SetIntellect(Value)
                                              Notifier:dispatchCmd(INTELLECT_CHANGE)  ← 界面就靠这个刷
        Key == ERDT_Intellect_CountDown(2) -> SetIntellectCountDown(Value)
        Key == ERDT_Intellect_Recover(6)   -> SetIntellectExchangeTimes(Value)
      主页电量条(view_common_power_ui)挂的就是 INTELLECT_CHANGE。
    """
    return uv(1, int(key)) + uv(2, int(value))


def build_energy_notify_bodies(d):
    """**能源一变就推这两条** —— 值 + 倒计时。

    ★ 少了倒计时那条, 客户端的"距下一点还有多久"就会一直停在旧值上。
    统一从这里出, 免得三处调用各写一遍漏掉一条。
    """
    a = d.get("actor") or {}
    return (build_data_notify(DATA_KEY_INTELLECT, int(a.get("intellect") or 0)),
            build_data_notify(DATA_KEY_INTELLECT_COUNTDOWN, energy_countdown(d)))


# ---------------- 关卡能源消耗 ----------------
# ★★ 2026-10-16 用户要求: 所有关卡(主线/活动/副本)统统一关 10 点能源。
STAGE_INTELLECT_COST = 10


def stage_intellect(tp, ch, mn, mi):
    """某关进场消耗多少能源。

    ★★ 2026-10-16 用户要求: **所有关卡(主线/活动/副本)一关 10 点**。
       原版是按 `dataconfig_stage_minor.intellect` 每关不同(1~24) —— 这里直接覆盖。
       预览(1098)和实际扣费(1074)都走这个函数, 所以两边**天然一致**,
       不会出现"界面写 10 结果扣 3"。
    """
    return STAGE_INTELLECT_COST


def build_stage_intellect_list(tp, ch):
    """CSGetStageIntellectRes{1: PreviewList(repeated CSStageIntellect)}
    CSStageIntellect{1:MainId 2:MinorId 3:Intellect 4:SweepIntellect}

    客户端拿它显示「消耗能源：N点/次」(lang 296)。以前 1097 **根本没接**,
    掉进兜底空 body ⇒ 预览是空的。
    ★ 这里**不再读配置表**, 一律用 `stage_intellect()` 的值(见上)。
    """
    tp, ch = int(tp), int(ch)
    out = []
    for (t, c, mn, mi), row in sorted(_stage_minor().items()):
        if t != tp or c != ch:
            continue
        out.append(uv(1, mn) + uv(2, mi)
                   + uv(3, stage_intellect(t, c, mn, mi))
                   + uv(4, stage_intellect(t, c, mn, mi)))
    return b"".join(bs(1, x) for x in out)


# ---------------- 个人中心 (1156/1158) / 头像 (1364) ----------------
# 协议(全部从 dump.cs 核对):
#   EnmUpdateDataType { 0=None 1=Name 2=HeadId 3=HeadFrameId 4=SignStr 5=Honor }
#   CSPlayerCenterData { 1:AccId(Uint64Id) 2:Name 3:HeadId 4:SignStr 5:CreateTm
#                        6:RoleNum 7:HeadFrameId 8:AchievementNum 9:Honors(repeated Uint32Id) }
#   CSGetPlayerCenterDataRes{1:PlayerCenter}     CSUpdatePlayerCenterDataRes{}  <- 空包!
#   CSGetHeadAndHeadFrameDataRes{1:HeadIconList 2:HeadFrameList}(repeated CSHeadFrameInfo)
#   CSHeadFrameInfo{1:Id 2:IsUnlock}
#
# ★ `HeadIconList[].Id` 用的是 **dataconfig_head_icon.id**(0~65) —— 反汇编
#   logic_personal_center proto#26 实证:
#       e.Config = getConfigDataByID('dataconfig_head_icon', 'id', e.ID)
#   头像框那边查的是 `dataconfig_head_frame`, 但**那张表我们没 dump 到**
#   (游戏是打开头像框页签才加载的) => 已加进 inject_cfgdump 的 PROACTIVE, 下次启动会 dump。
RENAME_ITEM_ID = 37            # item 37 / 175 = 「向导ID变更许可」= 文案 15474 改名道具
RENAME_ITEM_ID_ALT = 175

UPDATE_TYPE_NAME = 1
UPDATE_TYPE_HEAD_ID = 2
UPDATE_TYPE_HEAD_FRAME_ID = 3
UPDATE_TYPE_SIGN = 4
UPDATE_TYPE_HONOR = 5

RET_ITEM_NOT_ENOUGH = 54       # EnmRetCode ITEM_NOT_ENOUGH
# ★ 2026-10-15: 自选包"没选中任何物品"用这个码 —— 文案 69 =「该物品相关参数错误」
#   (见 _re\lang_zh.json)。客户端会照着弹提示。
RET_ITEM_PARAM_ERROR = 69
RET_NAME_INVALID = 122         # 兜底用; 客户端自己也会先校验(4~11 字)


def _head_icons():
    """dataconfig_head_icon: id -> row"""
    import cfgdata
    out = {}
    for r in cfgdata.load("dataconfig_head_icon").values():
        if isinstance(r, dict) and isinstance(r.get("id"), int):
            out[r["id"]] = r
    return out


def _pc_get(d, key, default=None):
    return (d.get("actor") or {}).get(key, default)


def build_player_center(d, acc_id=None):
    """CSGetPlayerCenterDataRes{1: CSPlayerCenterData}

    ★ AccId 和登录包**必须是同一个值**(个人中心也要显示那个 8 位码),
      否则玩家会看到两套 uid。
    """
    a = d.get("actor") or {}
    roles = d.get("roles") or {}
    # 成就数: 从任务标记里数已领取的成就(type=成就)
    ach = 0
    try:
        flags = d.get("task_flags") or {}
        for row in _task_rows().get(6, []):        # type=6 = 成就/发展
            # ★★ 2026-10-16 修: 以前这里写成 `AWARD_ALLREADY_GOT`(少个 G) ——
            #   常量其实叫 `AWARD_ALLREADYGOT`。NameError 被外层的 try/except 吞掉,
            #   于是**成就数永远是 0**（个人中心显示不对）。`_check_names.py` 抓出来的。
            if int(flags.get(str(row["id"]), 0)) == AWARD_ALLREADYGOT:
                ach += 1
    except Exception:
        pass
    # AccId (Uint64Id) —— 注意是 **bs**(Uint64Id 是包了一层的消息), 不是 uv
    pc = bs(1, uv(1, int(a.get("accId") or 0) if acc_id is None else int(acc_id)))
    pc += bs(2, str(a.get("name") or ""))                  # Name
    pc += uv(3, int(a.get("head_id") or 0))                # HeadId
    pc += bs(4, str(a.get("sign") or ""))                  # SignStr
    pc += uv(5, int(a.get("create_tm") or NOW))            # CreateTm
    pc += uv(6, len(roles))                                # RoleNum
    pc += uv(7, int(a.get("head_frame_id") or 0))          # HeadFrameId
    pc += uv(8, ach)                                       # AchievementNum
    for hid in (a.get("honors") or []):                    # Honors (repeated Uint32Id)
        pc += bs(9, uv(1, int(hid)))
    return bs(1, pc)


def _frame_rows():
    """dataconfig_head_frame: id -> row。**没 dump 到就回空 dict**(不是报错)。"""
    import cfgdata
    out = {}
    try:
        for r in cfgdata.load("dataconfig_head_frame").values():
            if isinstance(r, dict) and isinstance(r.get("id"), int):
                out[r["id"]] = r
    except Exception:
        return {}
    return out


# ★★★ 客户端**没有**的道具 id —— 服务端一律不要发。
#
# 起因（2026-10-17 实测定案）: 战斗结算回来时客户端跑
# `game_data.AnalysisGameData(CSGameData)` → `logic_store.UpdateStoreData` →
# `logic_store.CreateItem` 查 `dataconfig_itembase`; 查不到的 id ⇒ `item.Config = nil`
# ⇒ `PrepareShowData → ChangeData` 的 `data.Config.is_stack` 抛异常
# ⇒ **整条退出战斗的 Lua 链断掉 ⇒ 卡死**。
#
# Steam 版客户端（`steam-GL`, svn 82743）实测**没有 5133**（国服 bingkolo-Win 版有）。
# 5133 是**头像框 16 的重复件** —— 5215 的 `args[1]` 同样是 16, 二选一即可。
# ⇒ 把 5133 拉黑: 存档不再发它, `_frame_item_map` 也跳过它（否则框 16 可能映射到
#    这个客户端不认识的道具上, 反而把框 16 锁死）。
#
# ★ 名单来自客户端护栏 `_re\client_storefix.log` 里的
#   `★ 丢掉客户端没有配置的道具 id=`，目前只有这一条。以后再看到同类日志往这里加。
CLIENT_MISSING_ITEM_IDS = frozenset({5133})

# ★★★ 客户端**没有的**头像 id —— 和上面 5133 是**同一类问题**（2026-10-17 定案）。
#
# Steam 版客户端（steam-GL, svn 82743）的 `dataconfig_head_icon` 只有 **0..63**（64 行），
# 国服那份有 **0..65**（66 行）。服务端照国服那份发 ⇒ 客户端
# `logic_personal_center.SetHeadAndHeadFrameData` 里
#     v.Config = getConfigDataByID("dataconfig_head_icon", "id", v.ID)
# 回 nil ⇒ 紧接着 `v.Icon = v.Config.icon` 抛异常 ⇒ **个人信息/头像编辑页整页打不开**。
#
# ★ 名单来自客户端护栏 `_re\client_pcfix.log` 的实测输出：
#     [pcfix] ★ 丢掉客户端没有配置的 dataconfig_head_icon id=64
#     [pcfix] ★ 丢掉客户端没有配置的 dataconfig_head_icon id=65
#     [pcfix] dataconfig_head_icon 原 66 条 -> 留 64 条
#   （头像框那张表 `dataconfig_head_frame` Steam 是全的：0,2..16 ⇒ 不用拉黑。）
CLIENT_MISSING_HEAD_ICON_IDS = frozenset({64, 65})


def _frame_item_map():
    """头像框 id -> 换框道具 id。

    ★ 换框道具 = `dataconfig_itembase` 里 **func == 13** 的行, 它们的
      `args[1]` 就是**头像框 id**, 道具名是「【…】头像框」。
      实测 16 行: 5133→框16, 5201→框2, 5202→框3, 5204→框4, 5205→框5, 5206→框6,
      5207→框7, 5208→框8, 5203→框9, 5209→框10, 5210→框11, 5211→框12,
      5212→框13, 5213→框14, 5214→框15, 5215→框16（和 5133 撞同一个框）。
      ★ 5133 在 `CLIENT_MISSING_ITEM_IDS` 里被跳过 ⇒ 框 16 稳定映射到 5215。
      => 我们**不需要** dataconfig_head_frame 也能推出框 id 和"拥有即可解锁"。
    """
    import cfgdata
    out = {}
    try:
        for r in cfgdata.load("dataconfig_itembase").values():
            if not isinstance(r, dict) or int(r.get("func") or 0) != 13:
                continue
            a = r.get("args") if isinstance(r.get("args"), dict) else {}
            fid = a.get(1, a.get("1"))
            iid = r.get("id")
            if isinstance(fid, int) and isinstance(iid, int) \
                    and iid not in CLIENT_MISSING_ITEM_IDS:
                out.setdefault(fid, iid)       # 同一框有两件道具时取第一件
    except Exception:
        return {}
    return out


def build_head_and_frame(d):
    """CSGetHeadAndHeadFrameDataRes{1:HeadIconList 2:HeadFrameList}(repeated CSHeadFrameInfo)

    ★ 头像**就是角色头像**(dataconfig_head_icon 里 66 行, name 全是角色名, condition 是角色 id)
      => IsUnlock = (condition == 0) 或 **玩家拥有那个角色**。
      condition == 0 的是「向导默认头像」。

    ★★ 头像框**必须由服务端给出 id 列表** —— 反汇编 logic_personal_center proto#26
      （1365 的处理）看得很清楚, 客户端是**遍历我们回的 HeadFrameList**, 拿每条的
      Id 去 `getConfigDataByID("dataconfig_head_frame", "id", ID)` 查配置再画。
      它**不会**自己去遍历 dataconfig_head_frame。
      => 回空列表 = 整个页签空白, 这正是之前"头像框还是空的"的真因。

    id 列表来源(优先级):
      1) dataconfig_head_frame 里 dump 到的行(权威);
      2) 退路: 从 func=13 道具的 args[1] 推(现在就能用, 见 _frame_item_map)。
    IsUnlock: 玩家**拥有对应的换框道具**就解锁; 该框没有对应道具(默认框)也解锁。
    """
    roles = d.get("roles") or {}
    have = set(int(x) for x in roles.keys())
    r = b""
    for hid, row in sorted(_head_icons().items()):
        if hid in CLIENT_MISSING_HEAD_ICON_IDS:
            # ★ 客户端没有这个头像的配置 ⇒ 发过去会让它整页打不开（见常量上面的注释）
            continue
        cond = int(row.get("condition") or 0)
        unlocked = (cond == 0) or (cond in have)
        r += bs(1, uv(1, hid) + uv(2, 1 if unlocked else 0))

    finv = _frame_item_map()                       # 框id -> 道具id
    frames = _frame_rows()
    ids = sorted(frames) if frames else sorted(finv)
    for fid in ids:
        r += bs(2, uv(1, int(fid)) + uv(2, 1 if frame_unlocked(d, fid) else 0))
    return r


def frame_unlocked(d, fid):
    """头像框解锁判定: **拥有对应的换框道具**(func=13)就解锁; 没有对应道具的框也算解锁。

    ★ 和头像一样, 换框必须服务端说了算 —— 客户端的道具数量是服务端喂的。
    ★ 一旦 dataconfig_head_frame dump 到了, 表里没有的 id 一律判死(挡住乱填的号)。
      表还没 dump 时只能从道具推, 这时候认不出"默认框", 放行。
    """
    fid = int(fid)
    frames = _frame_rows()
    if frames and fid not in frames:
        return False                               # 表在手上 -> 不存在的框 = 非法
    iid = _frame_item_map().get(fid)
    if iid is None:
        return True                                # 没有对应道具的框 = 默认框, 直接给
    return int((d.get("items") or {}).get(str(iid), 0)) > 0


def update_player_center(d, data_type, pc):
    """处理 CSUpdatePlayerCenterDataReq{1:DataType 2:PlayerCenter}。

    返回 (ret_code, 说明)。0 = 成功。
    ★ CSUpdatePlayerCenterDataRes 是**空包**, 所以失败只能靠 ret_code 表达。
    """
    a = d.setdefault("actor", {})
    dt = int(data_type or 0)

    if dt == UPDATE_TYPE_NAME:
        name = str(pc.get("name") or "")
        n = len(name)
        if n < 4 or n > 11:
            return RET_NAME_INVALID, "名字长度必须是 4~11"
        if name == str(a.get("name") or ""):
            return 0, "名字没变"
        # ★ 扣改名道具(item 37 / 175)。客户端虽然会先检查, 但**扣必须由服务端做** ——
        #   客户端的道具数量是服务端喂的, 它自己扣不了。
        items = d.setdefault("items", {})
        have = int(items.get(str(RENAME_ITEM_ID), 0)) + int(items.get(str(RENAME_ITEM_ID_ALT), 0))
        if have <= 0:
            return RET_ITEM_NOT_ENOUGH, "没有改名道具(需要 item %d)" % RENAME_ITEM_ID
        for iid in (RENAME_ITEM_ID, RENAME_ITEM_ID_ALT):
            k = str(iid)
            c = int(items.get(k, 0))
            if c > 0:
                if c > 1:
                    items[k] = c - 1
                else:
                    items.pop(k, None)
                break
        a["name"] = name
        save(d)
        return 0, "改名成功"
    if dt == UPDATE_TYPE_SIGN:
        a["sign"] = str(pc.get("sign") or "")[:60]
        save(d)
        return 0, "签名已更新"
    if dt == UPDATE_TYPE_HEAD_ID:
        hid = int(pc.get("head_id") or 0)
        row = _head_icons().get(hid)
        if row is None:
            return RET_ITEM_NOT_ENOUGH, "头像 %d 不存在" % hid
        cond = int(row.get("condition") or 0)
        if cond != 0 and cond not in set(int(x) for x in (d.get("roles") or {}).keys()):
            return RET_ITEM_NOT_ENOUGH, "头像 %d 未解锁" % hid
        a["head_id"] = hid
        save(d)
        return 0, "头像已换"
    if dt == UPDATE_TYPE_HEAD_FRAME_ID:
        fid = int(pc.get("head_frame_id") or 0)
        # 0 = 不戴框, 直接放行; 其余必须存在且已解锁
        if fid != 0 and not frame_unlocked(d, fid):
            return RET_ITEM_NOT_ENOUGH, "头像框 %d 未解锁" % fid
        a["head_frame_id"] = fid
        save(d)
        return 0, "头像框已换"
    if dt == UPDATE_TYPE_HONOR:
        a["honors"] = sorted(int(x) for x in (pc.get("honors") or []))
        save(d)
        return 0, "荣誉展示已更新"
    return 0, "未知 DataType=%s(忽略)" % dt


RET_STAGE_NOT_ENOUGH_INTELLECT = 116      # EnmRetCode, 文案 116 = 「能源不足」


def consume_stage_intellect(d, tp, ch, mn, mi):
    """进关卡扣能源。返回 **(能不能进, 扣了多少, 扣完剩多少)**。

    ★★ 能源不足 = **不放行**(第一项 False), 一点不扣。
      用户明确要求:「能量不够不要放行, 因为用户一定可以在商城里获得稀因,
      然后再用稀因去买礼包, 再用礼包里的道具获得能量」——
      也就是**不存在死路**, 所以不必为了"保险"放行。
      (上一版是"最多扣到 0 也放行", 已按用户要求改掉。)

      拒绝时调用方回 `ret_code = 116` = `STAGE_NOT_ENOUGH_INTELLECT`,
      正好对上本地化表里的 `116 能源不足`。
    """
    cost = stage_intellect(tp, ch, mn, mi)
    a = d.setdefault("actor", {})
    cur = int(a.get("intellect") or 0)
    if cost > 0 and cur < cost:
        return False, 0, cur                        # 不放行, 一点不扣
    if cost <= 0:
        return True, 0, cur                         # 这关不要能源(剧情关之类)
    cap = intellect_cap(d)
    a["intellect"] = cur - cost
    # ★ 从"满/超额"掉到上限以内时, 自然恢复的计时**从这一刻重新开始**。
    #   不然表还停在很久以前 => 下一结算直接白送一大截能源。
    if cur >= cap and a["intellect"] < cap:
        a["intellect_ts"] = int(time.time())
    # ★★ 记下这次扣了多少 —— 失败/中途退出要**原样退还**(见 refund_stage_intellect)。
    d["last_stage_cost"] = {"tp": int(tp), "ch": int(ch), "mn": int(mn),
                            "mi": int(mi), "cost": cost}
    save(d)
    return True, cost, int(a["intellect"])


# ★★★ CSStageFinishReq 的 `Type` 字段 = `EnmStageFinishType`, **不是胜/负**:
#   ESFT_Normal=0(正常打完, 胜负都可能) / ESFT_Cancel=1(玩家主动退出) / ESFT_Retry=2(重试)
#   (证据: `play_normal_mode` proto#4 → `finishType = activeExit and ESFT_Cancel or ESFT_Normal`)
ESFT_NORMAL, ESFT_CANCEL, ESFT_RETRY = 0, 1, 2
ESFT_NAME = {ESFT_NORMAL: "正常打完", ESFT_CANCEL: "玩家退出", ESFT_RETRY: "重试"}


def stage_finish_is_win(ftype, remain_hp):
    """CSStageFinishReq 到底算不算「胜利」。

    ★★ `Type` 里没有胜负信息, 只能这样合起来判:
       - 玩家退出 / 重试             -> 不是胜利(退能源)
       - 正常打完 且 基地还有血量     -> 胜利
       - 正常打完 且 RemainHp == 0   -> 失败(基地被打掉, 退能源)

    ★★★ `RemainHp` = **基地剩余血量**, 不是队伍血量（玩家 2026-10-16 确认:
    这游戏是《明日方舟》那一类塔防, 基地有血量、每漏一个敌人 -1）。
    所以它**同时就是星数** —— 见 `stage_score()`。
    """
    return ftype == ESFT_NORMAL and (remain_hp or 0) > 0


def settle_stage_intellect(d):
    """**胜利结算**: 把这次进场的待退记录销掉。

    ★★ 为什么必须销: 1076 的 `Type` 是 `EnmStageFinishType`(0=正常打完/1=取消/2=重试),
      **不含胜负**。判定"胜利"只能靠 `BattleData.RemainHp`(见 bizserver 1076)。
      如果胜利后不销记录, 那么**之后任何一次**"非胜利"的 1076(比如退出)都会把
      上一次胜利的能源退回来 —— 等于白送。
    """
    if "last_stage_cost" in d:
        d.pop("last_stage_cost", None)
        save(d)
        return True
    return False


def refund_stage_intellect(d):
    """把上一次进场扣掉的能源**还回去**。返回 (退了没, 退了多少, 退完剩多少)。

    ★ 用户 2026-10-16:「活动关卡不会把能量还给我」—— 失败/中途退出应当退能源。
      记录在 1074 进场时写进存档的 `last_stage_cost`。
    ★★ **只兑现一次**(拿完就删): 否则反复点"失败结算"就能无限刷能源。
    ★ 退还**不超过上限**(不然可以从超上限状态倒刷)。
    """
    rec = d.get("last_stage_cost")
    a = d.setdefault("actor", {})
    cur = int(a.get("intellect") or 0)
    if not isinstance(rec, dict):
        return False, 0, cur
    d.pop("last_stage_cost", None)                      # 先删, 保证只兑现一次
    cost = int(rec.get("cost") or 0)
    if cost <= 0:
        save(d)
        return False, 0, cur
    cap = intellect_cap(d)
    a["intellect"] = min(cap, cur + cost)
    save(d)
    return True, cost, int(a["intellect"])


def _rnd():
    """随机包的抽样源。

    ★ 必须**模块级持有一个 RNG**。之前写成 `random.Random(0x5EED ^ len(req_list))`
      —— `len(req_list)` 恒为 1, 于是每次调用种的都是同一个种子,
      随机礼包会**永远抽到同一件**(自检里连开 3 个全出 25 才发现)。
      `Random()` 无参 = 用 os.urandom 播种, 不复现但真的随机。"""
    global _RNG
    if _RNG is None:
        import random
        _RNG = random.Random()
    return _RNG


def use_items(d, req_list):
    """1050 CSUseItemReq -> (1251 回包 body, gain, spent, refused)

    req_list: [{'id': 道具id, 'count': 用几个, 'args': [位置下标...]}]

    ★ 客户端的 `UseItem.ArgList` **只有「自选礼包」才非空** —— 里面放的是
      `package_item_list` 里被选中的**位置下标**。全开/随机礼包是空的,
      服务端自己决定发什么。反汇编 view_item_use.lua proto#21 确认。

    返回值:
      body    —— CSGetUseItemRes 的 body
      gain    —— [(item_id, +n)]
      spent   —— [(item_id, -n)]
      refused —— [(item_id, 原因)]  给日志用: 没实现的 func 一律**不消耗**
    """
    items_map = d.setdefault("items", {})
    idx = _item_by_id()
    gain, spent, refused = [], [], []
    rnd = _rnd()

    for e in (req_list or []):
        if not isinstance(e, dict):
            continue
        iid = int(e.get("id") or 0)
        cnt = int(e.get("count") or 0)
        # ★★ 0 / 缺失一律当 **1**。
        #   实测(2026-10-15, bizserver.log): 客户端在**第一次使用之后**
        #   会把 view_item_use 的 `useCount` 重置成 0, 之后每次点"使用"
        #   发过来的 `Count` 都是 0:
        #       08 c3 1f  10 00  18 00  22 02 08 04
        #       ID=4035   Exp=0  Count=0  ArgList=[4]
        #   第一次(Count=1)正常发奖; 从第二次起服务端 `cnt<=0 => continue` 直接跳过
        #   ⇒ 玩家看到「用了一个拿到物品, 然后就什么也用不了了」, 而且**不报网络错误**
        #     (服务端每次都好好回了空 ItemList)。
        #   "使用 0 个物品"本来就不是合法操作, 所以 0 只能理解成 1 ——
        #   而且方向是安全的: 绝不会比客户端想要的多消耗。
        if cnt <= 0:
            cnt = 1
        row = idx.get(iid)
        if row is None:
            continue
        if int(row.get("use_way") or 0) == USE_WAY_CANNOT:
            refused.append((iid, "use_way=CanNotUse"))
            continue
        have = int(items_map.get(str(iid), 0))
        cnt = min(cnt, have, USE_COUNT_CAP)
        if cnt <= 0:
            continue

        func = row.get("func")
        args = row.get("args") if isinstance(row.get("args"), dict) else {}

        def _arg(n, _a=args):
            v = _a.get(n, _a.get(str(n)))
            return int(v) if isinstance(v, int) else 0

        got = []
        if func == ITEM_FUNC_PKG_ALL:
            pkg = _item_packages().get(_arg(1))
            if pkg is None:
                refused.append((iid, "package_id=%s 不在表里" % _arg(1)))
                continue
            got = _grant_items(d, _pkg_scaled(pkg, cnt))
        elif func == ITEM_FUNC_PKG_SELECT:
            # ★★ 2026-10-15 修「自选包选完给的奖励是随机的」。
            #   客户端 view_item_use.UseReq(proto#21) 构造 ArgList 的方式是:
            #       每使用一次之前, 它先检查 `#已选 >= args[1](可选个数)`, 不够就
            #       **弹提示直接 return, 请求根本不发**; 够的话
            #           for i = 1, #已选 do table.insert(ArgList, {ID = 已选[i].Pos}) end
            #       —— 也就是 ArgList 里放的是**玩家选中的那些位置**, `Count` 才是使用次数。
            #   ⇒ `args[1] × Count` **不是** ArgList 应有的长度。
            #   原来拿 `len(pos) < want` 就"按配置顺序补足前 want 个", 于是
            #   日志里出现的那一幕: Count=3 + ArgList=[9] => 发的是位置 1,2,3 的
            #   91/92/93, 而玩家选的是位置 9 —— 看起来就像"给的是随机的"。
            #   现在: **照玩家送的位置发**, 重复 `Count` 次(每一次的选择相同)。
            pkg = _item_packages().get(_arg(2))
            if pkg is None:
                refused.append((iid, "package_id=%s 不在表里" % _arg(2)))
                continue
            pos = [int(x) for x in (e.get("args") or [])]
            if not pos:
                # ★★ 2026-10-15 用户要求: **没选中任何东西就不许用**。
                #   以前这里会"兜底取前 per 个位置"发出去 —— 玩家什么都没选
                #   却拿到东西, 看起来就像"给的是随机的"。
                #   正常流程下客户端自己会拦(`#已选 < 可选个数` 时只弹提示、
                #   不发请求), 所以这里到不了; 但服务端**不该**把没选中的请求
                #   当成"随便给几个", 直接拒绝、一点不消耗。
                refused.append((iid, "自选包未选择任何物品 -> 拒绝使用(不消耗)"))
                continue
            sel = _pkg_take(pkg, pos)
            if not sel:
                refused.append((iid, "自选包选中项为空/位置越界 pos=%s" % pos))
                continue
            got = _grant_items(d, _pkg_scaled(sel, cnt))
        elif func == ITEM_FUNC_PKG_RANDOM:
            pkg = _item_packages().get(_arg(2))
            if pkg is None:
                refused.append((iid, "package_id=%s 不在表里" % _arg(2)))
                continue
            for _i in range(cnt):
                got.extend(_grant_items(d, _pkg_random(pkg, _arg(1), rnd)))
        elif func == ITEM_FUNC_ENERGY:
            a = d["actor"]
            before = int(a.get("intellect") or 0)
            # ★★ 能源道具**可以突破上限** —— 这是原版设计, 用户明确要求:
            #   「手动使用道具恢复能源可以突破限制如 180/129」。
            #   以前写的 `min(cap, ...)` 会把它压回去。
            #   超额之后自然恢复会**暂停**(见 settle_energy), 直到消耗到上限以内。
            after = before + _arg(1) * cnt
            if after != before:
                a["intellect"] = after
                save(d)
            # 能源不是道具, 不进 gain
        elif func == ITEM_FUNC_DROP_BOX:
            # ★★ 2026-10-16 新增（用户报「舾装补给等内测物资有描述但打不开使用」）:
            #   func=4 的 27 个箱子(4001..4064) `award_item_list` 是**空的**,
            #   给什么写在这件道具 `args[1]` 指向的 `dataconfig_item_drop` 里。
            #   以前没有这个分支 ⇒ 落到最后那个 else「还没实现」⇒ 拒绝且不消耗,
            #   客户端表现就是"点了没反应 / 打不开"。
            #   ★ 道具的 `story` 文本和 drop 表**对不上**（内测期数值没同步）,
            #     以**配置表**为准 —— 配置表才是游戏实际会执行的。
            drop = _item_drops().get(_arg(1))
            if not isinstance(drop, dict) or not drop:
                refused.append((iid, "drop_id=%s 不在表里" % _arg(1)))
                continue
            # ★★ drop 可以**套 drop**（27 个 func=4 箱子里 12 个是套娃，最深 3 层），
            #   所以不能只发一层 —— 要递归展开。见 `_expand_drop`。
            pairs = _expand_drop(_arg(1), rnd)
            if not pairs:
                refused.append((iid, "drop_id=%s 展不开(空表/成环)" % _arg(1)))
                continue
            got = _add_items(d, [(i, n * cnt) for i, n in pairs])
        elif isinstance(row.get("award_item_list"), dict) and row["award_item_list"]:
            got = _grant_items(d, _pkg_scaled(row["award_item_list"], cnt))
        else:
            # ★ 不认识的 func 一律**拒绝且不消耗** —— 静默吞掉玩家的道具
            #   比"点了没反应"更糟。func=16(私域通讯) 就走这条。
            refused.append((iid, "func=%s 还没实现" % func))
            continue

        items_map[str(iid)] = have - cnt
        if items_map[str(iid)] <= 0:
            items_map.pop(str(iid), None)
        spent.append((iid, cnt))
        gain.extend(got)

    if gain or spent:
        save(d)

    # ★ 按 item_id 合并增量。随机礼包一次抽 9999 次会产出**十万条** gain,
    #   合并后只剩十来条 —— 下游(回包 + 1052 推送)全部跟着变轻。
    #   ★★ 这一步不能省: `build_item_notify` 虽然自己也合并, 但它拿到
    #     十万条时已经太晚了(见 2026-10-15 满核卡死事故)。
    merged, order = {}, []
    for i, n in gain:
        i = int(i)
        if i not in merged:
            merged[i] = 0
            order.append(i)
        merged[i] += int(n)
    gain = [(i, merged[i]) for i in order if merged[i]]

    parts = []
    for i, n in gain:                       # UseItemContent.ItemList (repeated CSGetItem)
        parts.append(bs(2, uv(1, i) + uv(2, n) + uv(3, 0)))
    parts.append(bs(3, build_gamedata(d)))  # UseItemContent.GameData
    return bs(1, b"".join(parts)), gain, spent, refused


ACTIVITY_STATUS_OPEN = 0
ACTIVITY_STATUS_CLOSE = 1


def build_fourteen_sign_info(d):
    """14日签到「安抚」—— CSGetFourteenSignInfoRes{1: Info(FourteenSignInfo)}

    ★ 策略 = **空列表 + Status=Close**，让首页那个入口根本不出现。

    依据（全是反汇编出来的，不是猜的）：

      view_home_ui proto#81:
        SetActive(self.activitySign, logic_fourteen_sign.IsUnlock()
                                    and logic_fourteen_sign.GetActivityState())

      logic_fourteen_sign proto#13 GetActivityState()  → 直接返回 uv0
      logic_fourteen_sign proto#14 RefreshActivityState(info):
        uv0 = (info.Status == Activity_Open)          -- Activity_Open = 0 / Close = 1

    所以 Status=Close ⇒ GetActivityState() 假 ⇒ `activitySign` 按钮 SetActive(false)。

    ★ 必须**显式把 Status 写出来**：EnumActivityStatus 上没有 DefaultValueAttribute，
      省略字段 = 客户端读到 0 = Activity_Open ⇒ 入口又冒出来了。
      光是"不写 Days"达不到目的。
    """
    info = uv(3, ACTIVITY_STATUS_CLOSE)      # Days(1) 不写 = 空列表; NowDay(2) 默认 0
    return bs(1, info)


def build_fourteen_sign_award_res(d):
    """CSSignFourteenSignRes{1: ItemList} —— 入口都藏了，正常走不到这儿。

    真被调到也只回空 ItemList（不报错、也不发奖）。
    """
    return b""


def build_stage_support(d):
    """CSGetStageSupportRes {1:SupportRoleList(repeated CSStageSupportData)
                            2:RefreshRemainTm}

    CSStageSupportData{1:Occupation 2:SupportList(repeated CSOccupationSupport)}
    CSOccupationSupport{1:SupportPos 2:FriendData(CSFriendData)}
    没有好友 -> 把玩家自己塞进去, 让助战选择界面有东西可选。"""
    roles = sorted(d["roles"].items())[:SUPPORT_POS_COUNT]
    friend = b""
    friend += uv(1, int(d["actor"].get("accId", 1)))         # AccId
    friend += bs(2, str(d["actor"].get("name", "Kleins")))   # Name
    friend += bs(3, str(d["actor"].get("name", "Kleins")))   # NickName
    friend += uv(4, int(d["actor"].get("level", 1)))         # Level
    friend += bs(5, _today_str())                            # LastLoginTm
    for i, (rid, rd) in enumerate(roles, 1):
        friend += bs(6, uv(1, i) + bs(2, build_role(rid, rd)) + uv(3, 1))
    friend += uv(12, len(d["roles"]))                        # RoleCount
    r = b""
    # 职业 0 一个分组(客户端按 Occupation 分组显示)
    r += bs(1, uv(1, 0) + bs(2, uv(1, 1) + bs(2, friend)))
    r += uv(2, 0)                                            # RefreshRemainTm
    return r


# ======================================================================
#  启动公告 (滚动公告 marquee)   —— 2026-10-15
# ======================================================================
# 客户端链路(全部在 logic_notice.lua 里, **不用改客户端**):
#   进主页 -> ENTER_SCENE_MSG 回调(proto#10) -> logic_notice.SendGetNoticeReq(2)
#        -> 业务层 1182 CSGetNoticeReq{1:Type, 2:Num, 3:Offset}
#   服务端回 1183 CSGetNoticeRes{1:NoticeInfo(repeated Notice), 2:Total}
#   -> logic_notice.proto#3 (OnGetNoticeRes): 逐条看 Notice.Type,
#      == Actual_Time(2) 的丢给 ShowUrgentNotice -> view_urgent_notice_ui.Open
#   -> view_urgent_notice_ui.InitData(proto#3) 只取:
#        notice.NoticeContent.Contents[1].StrContent.Content   ★ 必须有第 1 条
#        notice.Interval / notice.RollTimes                    ★ RollTimes 必须 >= 1
#      RollTimes <= 0 时 proto#4 直接 Close, 什么都不显示。
#
# ★ 为什么不用 dialog(ShowServiceNotice, logic_notice proto#8)?
#   那条路读的是 HTTP 平台公告, 而且按钮回调是 AppQuit() —— 点一下游戏就退出。
#   滚动公告不阻塞、不退出、常驻顶部, 是这里唯一合适的形式。
#
# 文案放在 _re\notice.json (可热改, 不用重打包); 文件缺了就退回下面的默认值。
NOTICE_FILE = paths.path("notice.json")

NOTICE_TYPE_NOT = 0
NOTICE_TYPE_GENERAL = 1
NOTICE_TYPE_ACTUAL_TIME = 2         # ★ 只有这个值会被 ShowUrgentNotice 收走
NOTICE_TYPE_LOGIN_BEF = 3
NOTICE_TYPE_LOGIN_AFT = 4

# lines 用 list, 一行一条 —— 滚动条是单行 TextMeshPro, 换行会串版,
# 所以发出去之前把换行压成中间的间隔符。
NOTICE_SEP = "\u3000\u3000"          # 两个全角空格

DEFAULT_NOTICE = {
    "enabled": True,
    "marquee": False,       # ★ 顶部滚动横幅: False = 不发(内容在「公告」界面里)
    "id": 1001,
    "title": "敬告",
    # ★ 改这里的话记得同步 _re\notice.json —— 这个只是 notice.json 缺失时的兜底
    "lines": [
        "敬告",
        "你现在正在游玩的是由 KleinsPatch 本地服务端驱动的单机版《环行旅舍》。"
        "本项目仅供协议逆向、游戏保存与技术交流研究。",
        "以及致所有妄图拿去开私服盈利的：",
        "KleinsPatch 在架构层面强制锁死了单客户端会话，且完全不具备多用户账户体系，"
        "所有道具与进度严格依赖本地存档。所以你大可断了这个念想。无论是出于尊重知识产权，"
        "还是出于对游戏本身的热爱——我操你妈。",
    ],
    "roll_times": 3,
    "interval": 5,
}

_notice_cache = {"mtime": None, "cfg": None}


def load_notice():
    """每次内容变了才重读 notice.json (按 mtime)。坏文件 -> 用默认值, 绝不让公告把服务搞挂。"""
    try:
        mt = os.path.getmtime(NOTICE_FILE)
    except OSError:
        return DEFAULT_NOTICE
    if _notice_cache["mtime"] == mt and _notice_cache["cfg"] is not None:
        return _notice_cache["cfg"]
    try:
        with open(NOTICE_FILE, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("notice.json 顶层必须是对象")
        merged = dict(DEFAULT_NOTICE)
        merged.update(cfg)
        _notice_cache["mtime"], _notice_cache["cfg"] = mt, merged
        return merged
    except Exception:
        return DEFAULT_NOTICE


def notice_text(cfg=None):
    """把 lines 拼成滚动条用的**单行**文本。"""
    cfg = cfg or load_notice()
    lines = cfg.get("lines") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    parts = [str(x).strip() for x in lines if str(x).strip()]
    return NOTICE_SEP.join(parts)


def build_notice_res(d=None, req_type=None):
    """CSGetNoticeRes{1:NoticeInfo(repeated Notice), 2:Total}

    Notice{ 1:Id 2:NoticeTittle 3:NoticeContent 4:Type 5:Channel
            6:Index 7:RollType 8:Interval 9:RollTimes 10:PlatId }
    NoticeContent{ 1:Contents(repeated ChildContent) 2:UrlContent }
    ChildContent{ 1:StrContent 2:IdContent 3:ColorId 4:Image 5:JumpType 6:JumpUrl }
    ChildStrContent{ 1:Content }

    ★ 请求里的 Type 不参与判断: 只回这一条 Actual_Time 公告, 客户端自己按
      Notice.Type == 2 过滤。这样客户端无论用哪个 Type 来问, 拿到的都是它。
    """
    cfg = load_notice()
    if not cfg.get("enabled", True):
        return uv(2, 0)
    # ★★ 2026-10-15 用户要求: **取消顶部滚动横幅**, 内容挪到「公告」界面。
    #   客户端 logic_notice.proto#3 只把 Notice.Type == Actual_Time(2) 的条目丢给
    #   ShowUrgentNotice(就是那个跑马灯); 所以这里干脆一条 Actual_Time 都不回。
    #   (公告界面的内容走的是另一条路: HTTP notice/api/notice/v1/getGeneralNotice,
    #    见 fakeserver / routes.json。)
    if not cfg.get("marquee", False):
        return uv(2, 0)
    text = notice_text(cfg)
    if not text:
        return uv(2, 0)
    child = bs(1, bs(1, text))                      # ChildContent{1:ChildStrContent{1:Content}}
    nbody = bs(1, child)                            # NoticeContent{1:Contents[0]}
    notice = b""
    notice += uv(1, int(cfg.get("id") or 1001))     # Id (要和 view_urgent 的 v.id 对得上)
    notice += bs(2, str(cfg.get("title") or ""))    # NoticeTittle (滚动条不显示, 留着备用)
    notice += bs(3, nbody)                          # NoticeContent
    notice += uv(4, NOTICE_TYPE_ACTUAL_TIME)        # ★ Type = 2
    notice += uv(6, 1)                              # Index
    notice += uv(7, 0)                              # RollType 0 = 战斗内外都滚
    notice += uv(8, max(1, int(cfg.get("interval") or 5)))    # Interval 秒
    notice += uv(9, max(1, int(cfg.get("roll_times") or 3)))  # ★ RollTimes >= 1
    return bs(1, notice) + uv(2, 1)                 # Total = 1


# ======================================================================
#  还原数据 (登录页「还原数据」按钮)   —— 2026-10-15
# ======================================================================
# 客户端链路: view_game_main_ui.ExitLoginBtnClick 被我们换成二次确认 ->
#   确认后 NetworkMgr.sendPkg(CMD_RESET, "", cb)  ->  bizserver 处理
#
# ★ 基准档是**外部文件** _re\baseline.json(由 _make_baseline.py 生成),
#   改它不用重打包, 服务端每次还原都重读。
# ★ 这是**不可逆**操作 —— 所以覆盖前一定先把当前存档备份掉; 备份失败就干脆不动手。
BASELINE = paths.path("baseline.json")
CMD_RESET = 19000            # 自定义命令(ECV 枚举只到 1410, 绝不会撞车)
BAK_KEEP = 10                # 保留最近多少个自动备份

# ★★ 「还原数据」真正走的是**文件标记**, 不是那条 19000。
#   原因(2026-10-15 实测踩到): 按钮在**登录页**, 那一刻客户端**还没连上 10301**
#   (协议连接是点了"进入游戏"之后才建的), 所以 NetworkMgr.sendPkg 根本发不出去
#   —— 我一开始按"已经连着"设计, 结果点了确认什么也没发生。
#   改成: 客户端写一个标记文件 -> 服务端在**登录(1001)时**兑现, 直接拿新档回包。
#   这样和连接状态完全无关, 而且用户看到的就是新数据, 不用重连也不用清缓存。
RESET_FLAG = paths.data_path(None, "state", "do_reset.flag")


def mark_reset_pending():
    """客户端点了「确定还原」-> 写标记。"""
    import os
    os.makedirs(os.path.dirname(RESET_FLAG), exist_ok=True)
    with open(RESET_FLAG, "w", encoding="utf-8") as f:
        f.write(str(int(time.time())) + "\n")


def consume_reset_flag():
    """有标记就删掉并返回 True —— **只兑现一次**。"""
    import os
    try:
        if os.path.exists(RESET_FLAG):
            os.remove(RESET_FLAG)
            return True
    except OSError:
        pass
    return False


# --- 「退出登录」标记(公告页那个按钮) ---
# 和 do_reset.flag 一个套路: HTTP 层(fakeserver)只负责**写标记**,
# 真正的动作由业务服在**下一个请求进来时**兑现(那里才拿得到 socket)。
# ★ 为什么不做成 HTTP 直接关 TCP: 两个进程、两套端口, HTTP 层摸不到业务服的 socket。
LOGOUT_FLAG = paths.data_path("KLEINS_LOGOUT_FLAG", "state", "do_logout.flag")


def mark_logout_pending():
    """公告页点了「退出登录」-> 写标记。"""
    import os
    os.makedirs(os.path.dirname(LOGOUT_FLAG), exist_ok=True)
    with open(LOGOUT_FLAG, "w", encoding="utf-8") as f:
        f.write(str(int(time.time())) + "\n")


def consume_logout_flag():
    """有标记就删掉并返回 True —— **只兑现一次**。"""
    import os
    try:
        if os.path.exists(LOGOUT_FLAG):
            os.remove(LOGOUT_FLAG)
            return True
    except OSError:
        pass
    return False


# --- 「重置角色数据」标记(公告页那个按钮) ---
# 和 do_reset.flag 一个套路, 区别是只动角色部分(见 reset_roles_to_baseline)。
RESET_ROLES_FLAG = paths.data_path("KLEINS_RESET_ROLES_FLAG", "state",
                                   "do_reset_roles.flag")


def consume_reset_roles_flag():
    """有标记就删掉并返回 True —— **只兑现一次**。"""
    import os
    try:
        if os.path.exists(RESET_ROLES_FLAG):
            os.remove(RESET_ROLES_FLAG)
            return True
    except OSError:
        pass
    return False


# --- 「删掉指定角色」标记(公告页「删除角色」按钮, 2026-10-16) ---
# 文件里放角色 id, 一行一个。和 do_reset.flag 一个套路:
# 服务端在**每个请求最前面、load() 之前**兑现。
DROP_ROLE_FLAG = paths.data_path("KLEINS_DROP_ROLE_FLAG", "state",
                                 "do_droprole.flag")


def consume_drop_role_flag():
    """取出并清空"要删掉的角色 id"列表 —— **只兑现一次**。"""
    import os
    try:
        if not os.path.exists(DROP_ROLE_FLAG):
            return []
        with open(DROP_ROLE_FLAG, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        os.remove(DROP_ROLE_FLAG)
    except OSError:
        return []
    out = []
    for line in raw.replace(",", "\n").split():
        try:
            out.append(int(line))
        except ValueError:
            continue
    return out


# --- 「角色工具」标记(公告页那个搜索框, 2026-10-16) ---
# 文件是 JSON: {"mode": "drop"|"give", "ids": [1001, 1055]}
# ★ 用户拍板: 这个功能**不是测试工具**, 是给"不想抽卡的玩家"用的正常功能 ——
#   所以获得要走完整流程(角色进 roles、道具进 items、该推的推送都要推)。
ROLE_TOOL_FLAG = paths.data_path("KLEINS_ROLE_TOOL_FLAG", "state",
                                 "do_roletool.flag")


# --- 「客户端渠道」握手 (2026-10-17) ---
# ★★ HTTP 层(fakeserver)从请求里刮到 `subPlatType` -> 写 state/client_channel.json;
#   业务服在**每个请求最前面、load() 之前**兑现, 用来选**哪个渠道的配置表目录**。
#   ★ 为什么非走文件: `CSLoginReq` 里压根没有渠道字段, 两个进程两套端口。
#     详见 cfgdata._channel_from_state / fakeserver._note_client_channel。
CHANNEL_STATE = paths.data_path(None, "state", "client_channel.json")
_last_channel = [None]          # 用 list 包一层, 免得写 global


def consume_client_channel():
    """读 state/client_channel.json; **与上次不同**才返回渠道名, 否则 None。

    ★ 读不到 / 解析失败一律 None —— 服务端退回 cfgdata 的自动探测, 绝不崩。
    """
    try:
        with open(CHANNEL_STATE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
        ch = d.get("subPlatType") or d.get("platType")
        if not isinstance(ch, str) or not ch:
            return None
    except Exception:
        return None
    if _last_channel[0] == ch:
        return None
    _last_channel[0] = ch
    return ch

# ★★ 「获得」规则(用户 2026-10-16 拍板, 逐字实现):
#     没有这个角色    -> 获得**角色本身**
#     已经有了        -> 发该角色的**专属补充协议** x1
#     ★ 获得次数 > 5  -> 改发**通用补充协议** x8
#   ★ 这条规则**只属于这个功能**, 不覆盖配置表的 repeat_gain ——
#     舍友合同那条路仍按配置表走(用户明确要求"保持配置表原样")。
GENERIC_PROTOCOL_ITEM = 68          # 「补充协议」(通用), 实测自 dataconfig_itembase
ROLE_TOOL_MAX_OWNED = 5             # 获得次数超过这个数就改发通用
ROLE_TOOL_GENERIC_COUNT = 8


def role_protocol_item(rid):
    """角色的专属补充协议道具 id。

    ★★ **不能想当然写成 `角色id + 2000`** —— 踩过:
       布林(1069) 的补充协议是 **3065**、露希尼(1055) 是 **3051**、
       蜜柑(1068) 是 **3064**、海珂特尔(1060) 是 **3056**……
       只有开头 1001->3001 / 1002->3002 那一段碰巧对得上。
       编号是**另一条顺序**(和舍友合同 2001..2065 一样), 不是角色 id 加偏移。
       ★ 症状: 按 +2000 发出去的 id 根本不存在, 客户端就显示**占位符**
         (用户实测: "布林的补充协议发下来是占位符")。

    ★ 正确答案和舍友合同完全一样, 在 `dataconfig_itembase` 的
      `func == 7` + `args[1] == 角色id` 上。
    """
    rid = int(rid)
    m = _protocol_role_item_map()
    if rid in m:
        return m[rid]
    # 兜底: 配置表读不到时才用老规则(至少不会崩)
    return rid + 2000


def consume_role_tool_flag():
    """取出并清空「角色工具」标记, 返回 (mode, ids)。没有标记时返回 (None, [])。"""
    import os
    try:
        if not os.path.exists(ROLE_TOOL_FLAG):
            return None, []
        with open(ROLE_TOOL_FLAG, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
        os.remove(ROLE_TOOL_FLAG)
    except (OSError, ValueError):
        return None, []
    if not isinstance(raw, dict):
        return None, []
    mode = raw.get("mode")
    ids = []
    for v in (raw.get("ids") or []):
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    if mode not in ("drop", "give"):
        return None, []
    return mode, ids


def items_push_body(pairs):
    """自建命令 **19102** 的 body: `"id:count,id:count"`。

    ★ 客户端补丁 `inject_newrole.py` 收到后开 `UICfg.COMMON_GET_ITEMS_UI`
      (游戏自带的「获得物品」窗), 参数 `{title, data={{itemId,itemCount}}}`。
    """
    return ",".join("%d:%d" % (int(i), int(n)) for i, n in (pairs or []))


def set_relogin_pending(d):
    """登记"玩家下次从公告回到主页时要自动重新登录"。

    ★★ 用户 2026-10-16 要求: 改角色之后**不要立刻**把人踢回登录页 ——
      公告 WebView 还开着的时候踢, 体验很怪(界面突然没了)。
      改成**条件式**: ①用过角色工具改过角色 ②从公告栏退回主页 —— 两条都满足才重登。
      标记存在存档里(跨请求有效), 由客户端发 19101 时兑现。
    """
    d["_relogin_pending"] = True


def consume_relogin_pending(d):
    """取出并清掉"待重登"标记 —— 只兑现一次。"""
    if d.pop("_relogin_pending", None):
        save(d)
        return True
    return False


def give_role_for_player(d, rid):
    """「角色工具」的**获得**。

    返回 `(说明文字, 新获得的角色 id 或 None, [(item_id, count)] 发到的道具)`。

    ★ 不调用 `_note_new_role` —— 角色弹窗由调用方**当场**推(见 bizserver),
      走那个"下个请求才推"的待推列表会晚一拍, 也会和这里的返回重复。
    """
    rid = int(rid)
    k = str(rid)
    roles = d.setdefault("roles", {})
    gc = d.setdefault("role_gain_count", {})
    it = d.setdefault("items", {})

    if k not in roles:
        # ---- 没有这个角色: 直接给角色本身 ----
        roles[k] = {"level": 1, "exp": 0, "strength": 0,
                    "potential": 0, "ability": 1, "skin": 0}
        gc[k] = max(1, int(gc.get(k, 0) or 0))
        return "获得角色本身", rid, []

    # ---- 已经有了: 按"获得次数"分档发补充协议 ----
    gain_time = int(gc.get(k, 0) or 0)
    if gain_time > ROLE_TOOL_MAX_OWNED:
        iid, n = GENERIC_PROTOCOL_ITEM, ROLE_TOOL_GENERIC_COUNT
        desc = "获得次数 %d > %d, 发通用补充协议 x%d" % (
            gain_time, ROLE_TOOL_MAX_OWNED, n)
    else:
        iid, n = role_protocol_item(rid), 1
        desc = "已有该角色, 发专属补充协议 x%d (item %d)" % (n, iid)
    it[str(iid)] = int(it.get(str(iid), 0)) + n
    gc[k] = gain_time + 1          # 这次获得也算一次, 档位会往上走
    return desc, None, [(iid, n)]


def drop_roles(d, ids):
    """从存档里"抹掉"若干角色, 返回做了哪些改动(字符串列表)。

    ★★ 这是**给测试用的**: 让"没有该舍友"这个前提可以反复重建
      (舍友合同给角色的逻辑要在这个前提下验)。

    清掉三处:
      roles[id]            角色本体
      role_gain_count[id]  获得次数 —— **必须一起清**, 留着的话合同会被当成
                           "重复获得", 只发专属补充协议/迹, **不给角色本身**
      groups[*].members    编队里的槽位 -> 置空 [0, 0](保留队伍形状)

    ★ 不 save(), 由调用方决定(独立脚本要写盘, 服务端本来就写)。
    """
    changed = []
    targets = set()
    for i in ids:
        try:
            targets.add(str(int(i)))
        except (TypeError, ValueError):
            continue
    if not targets:
        return changed

    roles = d.setdefault("roles", {})
    gc = d.setdefault("role_gain_count", {})
    for rid in sorted(targets):
        if rid in roles:
            del roles[rid]
            changed.append("roles 删掉 %s" % rid)
        if rid in gc:
            del gc[rid]
            changed.append("role_gain_count 删掉 %s" % rid)

    for gid, g in (d.get("groups") or {}).items():
        mem = (g or {}).get("members") or []
        for i, slot in enumerate(mem):
            if isinstance(slot, list) and slot and str(slot[0]) in targets:
                changed.append("groups[%s].members[%d] %s -> 空" % (gid, i, slot[0]))
                mem[i] = [0, 0]

    for key in ("cockpit", "rooms"):
        v = d.get(key)
        if isinstance(v, dict):
            for k in list(v.keys()):
                if str(k) in targets:
                    changed.append("%s 删掉 %s" % (key, k))
                    del v[k]
    return changed


_bl_cache = {"key": None, "cfg": None}


def load_baseline():
    """读基准档(按 内容大小+mtime 做键, 变了才重读)。"""
    import os
    st = os.stat(BASELINE)
    key = (st.st_size, int(st.st_mtime))
    if _bl_cache["key"] == key and _bl_cache["cfg"] is not None:
        return _bl_cache["cfg"]
    with open(BASELINE, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    _bl_cache["key"], _bl_cache["cfg"] = key, cfg
    return cfg


def _prune_backups():
    import glob
    import os
    try:
        files = sorted(glob.glob(SAVE + ".bak_*"))
        for p in files[:-BAK_KEEP] if len(files) > BAK_KEEP else []:
            os.remove(p)
    except Exception:
        pass


def reset_to_baseline():
    """把存档还原成基准值。返回 (成功?, 给人看的说明)。

    ★★ 顺序很重要: **先备份, 再覆盖**。备份失败就中止, 绝不在没有退路的情况下覆盖。
    """
    import os
    import shutil

    try:
        cfg = load_baseline()
    except Exception as e:
        return False, "基准档读不到: %r" % (e,)
    src = cfg.get("save")
    if not isinstance(src, dict):
        return False, "基准档格式不对(缺 save 段)"
    opts = cfg.get("_options") or {}

    bak = None
    try:
        if os.path.exists(SAVE):
            bak = "%s.bak_%s" % (SAVE, time.strftime("%Y%m%d_%H%M%S"))
            shutil.copyfile(SAVE, bak)
    except Exception as e:
        return False, "备份当前存档失败, 已中止(存档没动): %r" % (e,)

    new = json.loads(json.dumps(src))          # 深拷贝

    # --- 名字: _options.keep_name 为真就保留玩家当前的名字 ---
    if opts.get("keep_name"):
        try:
            new.setdefault("actor", {})["name"] = (load().get("actor") or {}).get("name")
        except Exception:
            pass

    # --- 数量覆盖 ---
    a = new.setdefault("actor", {})
    for key in ("diamond", "gold", "copper"):
        if opts.get(key) is not None:
            a[key] = int(opts[key])
    n = int(opts.get("item_count") or 0)
    items = new.setdefault("items", {})
    if n > 0:
        # ★★ 保险: 不可堆叠的道具**绝不能**被盖成大数量。
        #   客户端 logic_store.ChangeData 对 is_stack~=1 && Expired<=0 的物品走
        #   `for i = 1, Count do table.insert(展示列表, {...}) end`, 而它的调用方
        #   PrepareShowData 会对背包里每一件物品都调一次 —— 75 件 x 100000 就是
        #   750 万张 Lua 表, 客户端当场卡死(详见 docs 08 §8.40)。
        #   基准档本身已经不写 item_count 了, 这里再兜一层。
        try:
            import cfgdata
            _byid = {}
            for _row in cfgdata.load("dataconfig_itembase").values():
                _byid[_row.get("id")] = _row
        except Exception:
            _byid = {}
        for k in list(items):
            _r = _byid.get(int(k)) if str(k).lstrip("-").isdigit() else None
            items[k] = 1 if (_r is not None and _r.get("is_stack") != 1) else n

    save(new)
    _prune_backups()
    d = load()                                  # 过一遍 load() 的补全/规范化
    save(d)
    return True, ("已还原: 角色 %d 个 / 道具 %d 种 / 钻石=%s 金币=%s 铜币=%s%s"
                  % (len(d.get("roles") or {}), len(d.get("items") or {}),
                     a.get("diamond"), a.get("gold"), a.get("copper"),
                     ("  备份=" + os.path.basename(bak)) if bak else "  (原本没有存档)"))


def reset_roles_to_baseline():
    """**只**把角色部分回基准档: `roles` / `role_gain_count` / `groups`。

    ★ 用户 2026-10-16 要求(公告页「重置数据」的第二个按钮)。
      背包道具 / 关卡进度 / 邮件 / 任务**全部不动**。
    ★ 同样先备份再覆盖。
    ★ 用它是为了"把已获得的角色清掉, 好重新走一遍首次获得"
      (舍友合同/抽卡/活动送的都算首次)。
    """
    import os
    import shutil

    try:
        cfg = load_baseline()
    except Exception as e:
        return False, "基准档读不到: %r" % (e,)
    src = cfg.get("save")
    if not isinstance(src, dict):
        return False, "基准档格式不对(缺 save 段)"

    bak = None
    try:
        if os.path.exists(SAVE):
            bak = "%s.bak_%s" % (SAVE, time.strftime("%Y%m%d_%H%M%S"))
            shutil.copyfile(SAVE, bak)
    except Exception as e:
        return False, "备份当前存档失败, 已中止(存档没动): %r" % (e,)

    d = load()
    before = len(d.get("roles") or {})
    for k in ("roles", "role_gain_count", "groups"):
        if k in src:
            d[k] = json.loads(json.dumps(src[k]))      # 深拷贝
        else:
            d.pop(k, None)
    save(d)
    _prune_backups()
    d = load()
    save(d)
    return True, ("角色数据已回基准档: %d -> %d 个%s"
                  % (before, len(d.get("roles") or {}),
                     ("  备份=" + os.path.basename(bak)) if bak else ""))
