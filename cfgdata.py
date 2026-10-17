#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读取 _re\\cfg\\ 下游戏真实配置表(从运行时 dump 出来的 Lua 表文本), 供假服务端使用。

文本形如:
    1 =>
    {
        weight = 50,
        id = 1,
        up_role =
        {
            [1] = 1046,
        },
    }

解析成: { <行主键int>: {字段: 值} }

★ 2026-10-15 性能重写
    原来用 `re.match` 逐行解析 —— `dataconfig_stage_minor.txt` 有 **234 万行 / 47MB**,
    解析一次要 **3.5 秒**, 而客户端一登录就发 `1068` 拉关卡列表 => 登录转好几秒圈。
    现在:
      1. 解析器改成纯字符串操作(不碰 re), 快 3~4 倍;
      2. 解析结果落 **pickle 缓存** 到 `cfg/__pcache/`, 之后启动/重启都是毫秒级
         (第一次仍然要真解析一遍)。
    解析结果与旧版**逐字节一致**(用 _cfgdata_old.py 全表比对验证过)。

★ 2026-10-15 缓存键从 (大小, mtime) 改成 (大小, 内容 blake2b 摘要)
    游戏每次启动都会重新 dump 配置表, 内容一样但 mtime 变了 -> 缓存全部作废 ->
    "登录转圈" 复发。详见 _digest() 的注释。
"""
import hashlib
import json
import os
import pickle
import sys

import paths

# 缓存版本号: 解析逻辑变了就 +1, 让旧缓存失效
CACHE_VER = 1

# ★ 配置表不在本仓库里 —— 它们是从游戏客户端运行时 dump 出来的游戏数据，
#   由 openKleinsPatcher 打补丁后的客户端在**第一次进游戏时**自动写到客户端旁边的
#   `_kleinscfg\cfg\`。这里按优先级把那个目录找出来。
SENTINEL = "dataconfig_stage_minor.txt"

# 少这几张就基本没法玩，缺了要在启动时明确喊出来
ESSENTIAL = [
    "dataconfig_stage_minor", "dataconfig_stage_chapter", "dataconfig_itembase",
    "dataconfig_role_config", "dataconfig_shop_label", "dataconfig_recruit_pools",
    "dataconfig_task_base", "dataconfig_activity",
]


def _find_cfg_dir():
    """按优先级定位配置表目录；全找不到就退回「用户数据目录\\resources\\cfg」。

    ★★ 2026-10-17: **用户数据目录排第一位**。原来的候选（程序目录、客户端旁）
      只有 `KLEINS_CFG_DIR` 显式指定时才该盖过它。
    """
    env = os.environ.get("KLEINS_CFG_DIR")
    if env:
        return env                      # 显式指定，无条件信任
    base = paths.base_dir()
    candidates = [
        os.path.join(paths.data_dir("resources"), "cfg"),               # ⓪ 用户数据目录
        os.path.join(base, "cfg"),                                     # ① 本地
        os.path.join(os.path.dirname(base), "_kleinscfg", "cfg"),       # ② 上一级（客户端旁）
        os.path.join(os.path.dirname(os.path.dirname(base)),
                     "_kleinscfg", "cfg"),                             # ③ 上两级
    ]
    for c in candidates:
        if os.path.isfile(os.path.join(c, SENTINEL)):
            return c
    return candidates[0]


CFG_DIR = _find_cfg_dir()
PCACHE_DIR = os.path.join(CFG_DIR, "__pcache")


# ★★★ 2026-10-17: 配置表 dump **按客户端渠道分目录**了。
#
#   为什么: 以前所有客户端共用一个 cfg 目录, 而客户端的 `alreadyDumped` 只看
#   "文件存在且 >1000 字节" ⇒ **先 dump 的那个客户端会永久占位**, 别的渠道从此不再
#   重 dump。服务端于是拿着(比如国服产的)那份表推 id, 客户端查不到就
#   `attempt to index a nil value` 崩 —— 2026-10-17 一天踩了四次:
#   道具 5133 / 头像 64,65 / 卡池 35..40 / 角色图鉴 dataconfig_role_files。
#
#   ⇒ 客户端现在 dump 到 `<cfg>/<SubPlatType>/`, 这里**优先读它**。
#     服务端推出来的 id 天然就是客户端真有的 ⇒ 这一类 bug 从根上消失。
def _channel_from_state():
    """读 `state/client_channel.json` —— fakeserver 从 HTTP 请求里刮出来的渠道。

    ★★ 这是**权威值**: 客户端自己报的 `subPlatType`。优先于下面那个"表最多"的启发式。
      文件不存在(还没跑过客户端)就返回 None, 绝不抛。
    """
    try:
        import json
        p = os.path.join(paths.data_root(), "state", "client_channel.json")
        with open(p, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        s = d.get("subPlatType") or d.get("platType")
        if isinstance(s, str) and s:
            return s
    except Exception:
        pass
    return None


def _client_dir():
    """渠道子目录名；返回 None 表示直接用平面目录（兼容老 dump / 还没重 dump）。

    优先级: `KLEINS_CFG_CLIENT` 环境变量 > **客户端自己报的渠道** > **表最多的那个**子目录 > None。

    ★ 踩过的坑: 一开始写的是"唯一一个子目录才认" —— 但客户端脚本会**预建**几个
      候选目录（steam-GL / bingkolo-Win / unknown，Lua 没有 mkdir），
      于是永远"不止一个" ⇒ 永远退回平面 ⇒ 整个机制白做。
    ⇒ 改成按**实际 dump 出来的 `dataconfig_*.txt` 张数**挑最多的那个（0 张不算）。
      多个候选目录并存时这也自然选到真正在用的那个。
    ★★ 但"最多"只是启发式: 国服和 Steam 两份都 dump 过之后占比就看运气了。
      所以 2026-10-17 加了 `state/client_channel.json`（HTTP 层握手）当**权威来源**,
      而且在**每个请求开头**都会重新确一遍（见 switch_client / bizserver）。
    """
    env = os.environ.get("KLEINS_CFG_CLIENT")
    if env:
        return env
    # ① 客户端自己报的（权威）
    ch = _channel_from_state()
    if ch and os.path.isdir(os.path.join(CFG_DIR, ch)):
        return ch
    # ② 兜底: 表最多的那个子目录
    best, best_n = None, 0
    try:
        subs = [d for d in os.listdir(CFG_DIR)
                if d != "__pcache" and os.path.isdir(os.path.join(CFG_DIR, d))]
    except Exception:
        return None
    for d in subs:
        try:
            n = len([f for f in os.listdir(os.path.join(CFG_DIR, d))
                     if f.startswith("dataconfig_") and f.endswith(".txt")])
        except Exception:
            n = 0
        if n > best_n:
            best, best_n = d, n
    return best


CLIENT_DIR = _client_dir()


def _table_path(name):
    """一张表的实际路径: 优先渠道子目录, 找不到再退回平面目录。

    ★ 退回链是**过渡期必须的**: 新机制刚上线时渠道目录还是空的（要等客户端
      启动一次才会 dump 出来），这时必须还能读到平面目录里那份，
      否则整个服务端空转 —— 用户会看到"关卡/商店/任务全空"。
    """
    if CLIENT_DIR:
        p = os.path.join(CFG_DIR, CLIENT_DIR, name + ".txt")
        if os.path.exists(p):
            return p
    return os.path.join(CFG_DIR, name + ".txt")


def cfg_report():
    """给启动横幅用：返回 (是否找到, 人类可读的多行说明)。"""
    def _names(d):
        try:
            return {f for f in os.listdir(d)
                    if f.startswith("dataconfig_") and f.endswith(".txt")}
        except Exception:
            return set()

    flat = _names(CFG_DIR)
    own = _names(os.path.join(CFG_DIR, CLIENT_DIR)) if CLIENT_DIR else set()
    # ★★★ 服务端**实际能读到**的表 = 渠道目录 ∪ 平面目录（渠道优先，平面兜底）。
    #   2026-10-17 修: 以前这里只数**平面目录**、哨兵也只在平面目录里找 ——
    #   渠道化之后表都 dump 在 <cfg>\<渠道>\ 里，于是明明有 67 张却报
    #   "找到 0 张 / 没找到配置表"，把人吓得以为白 dump 了。
    eff = own | flat
    n = len(eff)
    n_own, n_fb = len(own), len(flat - own)
    # ★ `SENTINEL` 本身就带 `.txt`，`_table_path()` 会自动再接一个 ⇒ 这里要去掉。
    found = os.path.isfile(_table_path(SENTINEL[:-4]))
    lines = ["配置表目录 : %s" % CFG_DIR,
             "渠道子目录 : %s  (来自它 %d 张 / 退回平面 %d 张)"
             % (CLIENT_DIR or "（无 —— 全部读平面目录）", n_own, n_fb),
             "渠道来源   : %s" % client_source(),
             "找到的表   : %d 张" % n]
    if CLIENT_DIR is None:
        lines += ["",
                  "  ▸ 平面目录里的表可能是**别的渠道**的客户端 dump 出来的。",
                  "    建议：启动一次客户端让它 dump 出 <cfg>\\<SubPlatType>\\ 之后重启本服务端；",
                  "    或设环境变量 KLEINS_CFG_CLIENT 指定渠道目录。"]
    if not found:
        missing = [e + ".txt" for e in ESSENTIAL if e + ".txt" not in eff]
        lines += [
            "",
            "  ！！ 没找到配置表 —— 服务端能启动，但关卡/抽卡/商店/任务等会全部是空的。",
            "",
            "  怎么拿到配置表（只需做一次）：",
            "    1) 用 openKleinsPatcher 给客户端打补丁（补丁里含配置 dump 模块）",
            "    2) 启动一次游戏，配置表会自动 dump 到  <客户端上级目录>\\_kleinscfg\\cfg\\",
            "    3) 再启动本服务端即可（会自动去那里找）",
            "    或者：把配置表放到 %s ，" % os.path.join(paths.base_dir(), "cfg"),
            "          或设环境变量 KLEINS_CFG_DIR 指向你已经有的 cfg 目录。",
        ]
        if missing:
            lines.append("")
            lines.append("  关键缺失: %s" % ", ".join(sorted(missing)))
    return found, "\n".join(lines)

_cache = {}
_path_of = {}          # name -> 上次读的是哪个文件（用于"路径变了就重读"）


# ★★ 2026-10-17: 运行期切换渠道目录。
#   fakeserver 从 HTTP 请求里刮到 subPlatType -> 写 state/client_channel.json;
#   业务服在**每个请求最前面、load() 之前**调 sync_client_from_state() 兑现。
#   ★ 为什么不用重启: 用户可能开着游戏换客户端(国服/Steam), 重启会断 TCP。
def switch_client(name):
    """把当前渠道目录切到 `name`。返回 True 表示真的变了。

    ★★ 必须清缓存: `_cache` 是按**表名**缓存的解析结果, 换了目录不清的话,
      下一次 load() 会直接命中缓存 —— 拿到的还是**另一个渠道**的行, 症状和没切一样。
      (`_path_of` 也得清, 它的作用正是"路径变了就重读"。)
    """
    global CLIENT_DIR
    if name == CLIENT_DIR:
        return False
    CLIENT_DIR = name
    _cache.clear()
    _path_of.clear()
    return True


def sync_client_from_state():
    """启动时调一次: state 里有渠道就用它, 否则保持启发式结果(_client_dir)。"""
    ch = _channel_from_state()
    if not ch:
        return False
    return switch_client(ch)


def client_source():
    """当前渠道是**从哪来的** —— 只给启动横幅看。"""
    if os.environ.get("KLEINS_CFG_CLIENT"):
        return "环境变量 KLEINS_CFG_CLIENT"
    if _channel_from_state():
        return "state/client_channel.json（HTTP 层握手）"
    if CLIENT_DIR:
        return "自动探测（表最多的子目录）"
    return "（无）"


def _parse_value(tok):
    tok = tok.strip().rstrip(",").strip()
    if not tok:
        return tok
    c = tok[0]
    if c == '"':
        return tok[1:-1] if tok.endswith('"') and len(tok) > 1 else tok
    if c == "-" or c == "." or c.isdigit():
        try:
            return int(tok)
        except ValueError:
            pass
        try:
            return float(tok)
        except ValueError:
            return tok
    if tok == "true":
        return True
    if tok == "false":
        return False
    if tok == "nil":
        return None
    return tok


def _parse_file(path, rows):
    """把 dump 文本解析进 rows(行主键 -> dict)。"""
    stack = []
    pending = None
    top = None
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line[-2:] == "=>":
                k = line[:-2].strip()
                if k.isdigit():
                    top = {}
                    rows[int(k)] = top
                    stack = [top]
                    pending = None
                    continue
            if not stack:
                continue
            c = line[0]
            if c == "{":
                if pending is not None:
                    nt = {}
                    stack[-1][pending] = nt
                    stack.append(nt)
                    pending = None
                continue
            if c == "}":
                if len(stack) > 1:
                    stack.pop()
                continue
            if c == "[":
                i = line.find("]")
                if i <= 0:
                    continue
                k = line[1:i].strip()
                if not k.isdigit():
                    continue
                rest = line[i + 1:].lstrip()
                if not rest.startswith("="):
                    continue
                v = rest[1:].strip()
                if not v or v == ",":
                    pending = int(k)
                else:
                    stack[-1][int(k)] = _parse_value(v)
                continue
            j = line.find("=")
            if j <= 0:
                continue
            k = line[:j].strip()
            # ★ 必须 ASCII-only —— 旧版正则是 [A-Za-z_], 而 Python 的 str.isalpha()
            #   对中文也返回 True。dump 里有**含换行的中文字符串**(换行后那半行里
            #   可能带 '='), 用 isalpha() 会把中文当成字段名, 解析结果就和旧版不一致。
            if not k or not (("a" <= k[0] <= "z") or ("A" <= k[0] <= "Z")
                             or k[0] == "_"):
                continue
            v = line[j + 1:].strip()
            if not v or v == ",":
                pending = k
            else:
                stack[-1][k] = _parse_value(v)
    return rows


def _digest(path):
    """文件内容的 blake2b-64 摘要(不用 mtime!)。

    ★ 为什么不用 (大小, mtime): 游戏**每次启动都会重新 dump 一遍配置表**
      (inject_cfgdump 包装了 config_data_center.getConfigData), 内容一模一样但
      文件被重写 => mtime 变了 => 缓存全部作废 => 下次登录又把 234 万行的
      dataconfig_stage_minor 重新解析一遍 => **登录又开始转圈**(2026-10-15 复发)。
      按内容做键, 只要 dump 出来的东西没变就永远命中。
      47MB 读+哈希约 80ms, 相比 1.5s 的解析完全是赚的。"""
    h = hashlib.blake2b(digest_size=8)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pcache_path(name, size, digest):
    return os.path.join(PCACHE_DIR, "%s.v%d.%d.%s.pkl"
                        % (name, CACHE_VER, size, digest))


def load(name):
    """name 形如 'dataconfig_recruit_pools' (不带 .txt)。返回 {row_key: dict}

    ★ 缓存为空时会重新读盘 —— 有些表是客户端跑起来后才 dump 出来的
      (例如 dataconfig_day_task_reward), 服务端先启动、后 dump 时也能拿到。"""
    path = _table_path(name)
    if name in _cache:
        # ★ 路径变了也要重读 —— 客户端刚 dump 出渠道目录里的那份时，_table_path()
        #   的返回值会从平面文件变到渠道文件，必须让缓存跟着换。
        if (_cache[name] and _path_of.get(name) == path) or not os.path.exists(path):
            return _cache[name]
    rows = {}
    if not os.path.exists(path):
        _cache[name] = rows
        _path_of[name] = path
        return rows

    cp = None
    try:
        size = os.path.getsize(path)
        cp = _pcache_path(name, size, _digest(path))
        if os.path.exists(cp):
            with open(cp, "rb") as f:
                rows = pickle.load(f)
            _cache[name] = rows
            _path_of[name] = path
            return rows
    except Exception:
        rows = {}

    rows = _parse_file(path, {})

    if cp is not None:
        try:
            os.makedirs(PCACHE_DIR, exist_ok=True)
            with open(cp, "wb") as f:
                pickle.dump(rows, f, 4)
            # 清掉这张表的旧缓存(内容变过的那几份)
            for old in os.listdir(PCACHE_DIR):
                if old.startswith(name + ".v") and old != os.path.basename(cp):
                    try:
                        os.remove(os.path.join(PCACHE_DIR, old))
                    except Exception:
                        pass
        except Exception:
            pass
    _cache[name] = rows
    _path_of[name] = path
    return rows


def recruit_pools():
    """返回 [(pool_id, pool_type, pool_name, up_roles, one_cost, ten_cost), ...]"""
    out = []
    for k in sorted(load("dataconfig_recruit_pools")):
        r = load("dataconfig_recruit_pools")[k]
        up = r.get("up_role")
        ups = []
        if isinstance(up, dict):
            ups = [up[i] for i in sorted(up) if isinstance(up[i], int)]
        out.append((int(r.get("pool_id", k)), int(r.get("pool_type", 1)),
                    r.get("pool_name") or "",
                    ups, int(r.get("one_cost", 1)), int(r.get("ten_cost", 10))))
    return out


def pool_roles(pool_id):
    """某卡池里的角色 id 列表(带权重)。返回 [(role_id, weight), ...]"""
    out = []
    for k in sorted(load("dataconfig_recruit_pool_role")):
        r = load("dataconfig_recruit_pool_role")[k]
        if int(r.get("pool_id", 0)) == int(pool_id):
            out.append((int(r.get("role_id", 0)), int(r.get("weight", 1))))
    return out


def all_role_ids():
    """dataconfig_role_config 里全部角色 id"""
    return sorted(int(r["id"]) for r in load("dataconfig_role_config").values()
                  if isinstance(r.get("id"), int))


def _by_id(name, key="id"):
    rows = load(name)
    return {r[key]: r for r in rows.values() if isinstance(r.get(key), int)}


# ---------------- 角色成长 ----------------
def role_star(role_id):
    r = _by_id("dataconfig_role_config").get(int(role_id))
    return int(r.get("star", 1)) if r else 1


def max_role_level(star, step=0):
    """dataconfig_star_strengthen[star, step].max_level  (角色等级上限)"""
    for r in load("dataconfig_star_strengthen").values():
        if r.get("star") == star and r.get("step") == (step or 0):
            return int(r.get("max_level", 999))
    return 999


def level_cost(star, lvl):
    """dataconfig_star_levelup[star, lvl] -> (升级所需经验, 每100经验的金币)"""
    for r in load("dataconfig_star_levelup").values():
        if r.get("star") == star and r.get("level") == lvl:
            return int(r.get("exp", 999)), int(r.get("money_count", 0))
    return 999, 0


def item_exp(item_id):
    """经验道具 -> 经验值 (itembase[id].args[1])"""
    r = _by_id("dataconfig_itembase").get(int(item_id))
    if not r:
        return 0
    a = r.get("args")
    if isinstance(a, dict):
        return int(a.get(1, 0) or 0)
    return 0


# ---------------- 皮肤(着装) ----------------
def role_ranks(role_id):
    """角色的天赋阶位列表 [(rank, max_step), ...] (dataconfig_role_talen_rank)"""
    out = []
    for r in load("dataconfig_role_talen_rank").values():
        if r.get("id") == int(role_id):
            out.append((int(r.get("rank", 1)), int(r.get("max_step", 0))))
    out.sort()
    return out


# ---------------- 角色养成: 突破 / 能力 / 天赋  (2026-10-15) ----------------
# ★★ 血泪: cfg 文本的键是**行序号**, `id` 只是行里的一个字段。要按 id 找行必须
#    走 _by_id()。而下面这几张表**同一个 id 有多行**(按 step/rank/level 区分),
#    _by_id 会互相覆盖 => 一律自己扫表, 用 (id, 次键[, 三次键]) 定位。
#
# 客户端事实 (反汇编 view_role_up_step_ui / view_role_up_ability_ui /
#            view_role_up_rank_ui 的按钮回调):
#   * 突破  SureUpBtnClick  -> dataconfig_role_strengthen[id, StrengthLevel+1].cost_item_list
#        cost_item_list 的第 4 项固定是「格」(item 2), 界面单独当货币渲染。
#        档位走 StrengthLevel ~= 最大档 时直接 return (满档点不动)。
#   * 能力  AbilityLevelBtnClick -> dataconfig_ab_skill_levelup[id, AbilityLevel].cost
#        AbilityLevel == 4 时 return  => 上限 4。
#   * 天赋  RankLevelBtnClick -> dataconfig_role_talen_levelup[id, rank, step].cost
#        step == 4 时 return => 上限 4。


def _scan(name, **want):
    """在 name 表里找**所有**匹配 want 字段的行 (want 的值按 int 比较)。"""
    out = []
    try:
        rows = load(name)
    except Exception:
        return out
    for r in rows.values():
        ok = True
        for k, v in want.items():
            rv = r.get(k)
            if not isinstance(rv, int) or rv != int(v):
                ok = False
                break
        if ok:
            out.append(r)
    return out


def strengthen_row(role_id, step):
    """dataconfig_role_strengthen[role_id, step] —— 突破一档的消耗/产出。

    ★ step 0 是"初始态", cost/award 都为空; 真正花钱的是 step>=1。
    """
    rows = _scan("dataconfig_role_strengthen", id=role_id, step=step)
    return rows[0] if rows else None


def max_role_step(role_id):
    """该角色能突破到的最大档位 (dataconfig_star_strengthen 按 star 分组, step 0..2)。"""
    star = role_star(role_id)
    steps = [int(r.get("step", 0)) for r in load("dataconfig_star_strengthen").values()
             if r.get("star") == star]
    return max(steps) if steps else 2


def ability_row(role_id, level):
    """dataconfig_ab_skill_levelup[role_id, level] —— 能力升到 level 的消耗。

    客户端是 `AbilityLevelBtnClick` 里拿 `GetRoleAbilityLevelConfigById(ID, 当前等级)`,
    也就是"花第 N 行的钱, 等级从 N 涨到 N+1"。level=4 那行 cost 为空 = 封顶标记。
    """
    rows = _scan("dataconfig_ab_skill_levelup", id=role_id, level=level)
    return rows[0] if rows else None


def max_ability_level():
    """能力等级上限 —— 客户端硬编码 4 (AbilityLevel == 4 时按钮直接 return)。"""
    lv = [int(r.get("level", 0)) for r in load("dataconfig_ab_skill_levelup").values()
          if isinstance(r.get("level"), int)]
    return max(lv) if lv else 4


def talen_rank_row(role_id, rank):
    """dataconfig_role_talen_rank[role_id, rank] —— 阶位的 max_step / unlock_step。"""
    rows = _scan("dataconfig_role_talen_rank", id=role_id, rank=rank)
    return rows[0] if rows else None


def talen_levelup_row(role_id, rank, level):
    """dataconfig_role_talen_levelup[role_id, rank, level] —— 天赋升一步的消耗。

    ★ 这张表 2026-10-15 才加进 inject_cfgdump 的 PROACTIVE 列表, **没 dump 到就返回
      None** —— 调用方必须能容忍 (表现为"天赋暂不可升", 而不是崩)。
    """
    rows = _scan("dataconfig_role_talen_levelup", id=role_id, rank=rank, level=level)
    return rows[0] if rows else None


def max_talen_step(role_id, rank):
    """天赋步数上限。客户端硬编码 4 (step == 4 时 return)。"""
    return 4


def role_skin_ids(role_id):
    r = _by_id("dataconfig_role_config").get(int(role_id))
    s = (r or {}).get("skin_ids")
    return [int(v) for v in s.values()] if isinstance(s, dict) else []


def all_skin_ids():
    """全部皮肤 id (dataconfig_skin_config)"""
    return sorted(int(r["skin_id"]) for r in load("dataconfig_skin_config").values()
                  if isinstance(r.get("skin_id"), int))


# ======================================================================
#  角色名索引 (roles_index.json)                           2026-10-17
# ======================================================================
#  ★ 以前这份索引必须**手动**跑 `python tools\make_roles_index.py` 生成。
#    没生成时 `fakeserver._load_role_index()` 返回空表，而「角色工具」页是
#    **遍历这张表**渲染的 ⇒ 页面**整个是空的**（不是老注释说的"只显示 id"）。
#  ⇒ 改成自动生成：bizserver 启动时生成一次，fakeserver 打开角色工具页前
#    发现缺文件就现场补。产物落在 <用户数据>\resources\roles_index.json。
#  ★ 内容是从**配置表**算出来的（dataconfig_role_config + dataconfig_language），
#    所以它属于游戏派生数据，不进 git —— 每个渠道自己算一份。
def build_roles_index():
    """{角色id(str): 名字}。配置表读不到就返回 {}（不抛）。"""
    lang = {}
    for r in load("dataconfig_language").values():
        if isinstance(r, dict):
            i, t = r.get("id"), r.get("text")
            if isinstance(i, int) and isinstance(t, str):
                lang[str(i)] = t
    out = {}
    for r in load("dataconfig_role_config").values():
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not isinstance(rid, int) or rid <= 0:
            continue
        nm = r.get("name")
        out[str(rid)] = lang.get(str(nm), "") if nm is not None else ""
    return out


def ensure_roles_index(path=None, force=False, log=None):
    """保证 `roles_index.json` 存在。返回 `(是否写入, 条目数)`。**幂等**。

    `path` 不给就用 `paths.data_path(...)`
    （= `<用户数据>\\resources\\roles_index.json`）。
    ★ 配置表还没 dump 出来时返回 `(False, 0)` 且**不留半成品**，下次再试。
    """
    path = path or paths.data_path("KLEINS_ROLES_INDEX", "resources",
                                   "roles_index.json")
    if not force and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d:
                return False, len(d)
        except Exception:
            pass                      # 文件坏了 ⇒ 重建
    idx = build_roles_index()
    if not idx:
        return False, 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, path)             # 原子替换, 不会留下半个文件
    if log:
        log("角色名索引已生成: %s (%d 个角色)" % (path, len(idx)))
    return True, len(idx)


if __name__ == "__main__":
    # `python cfgdata.py` = 配置表体检（只读，不改任何东西）
    _ok, _rep = cfg_report()
    print(_rep)
    if not _ok:
        sys.exit(1)
    print()
    ps = recruit_pools()
    print("卡池数:", len(ps))
    for p in ps[:5]:
        print("  ", p, "-> 角色", pool_roles(p[0])[:4])
    print("全部角色数:", len(all_role_ids()))
