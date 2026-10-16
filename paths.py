#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一的路径解析。

★★ 两类目录, 别搞混
  - **程序目录** `base_dir()` —— exe/脚本自身所在目录。
    只放「程序本体 + 作者手写的**只读**配置」, 保证成品能干净地整包分发。
  - **用户数据目录** `data_root()` —— `%USERPROFILE%\\Saved Games\\freeKleins`。
    存档 / 游戏资产 / 运行期状态 / 日志全在这里。

★★ 为什么要分开(2026-10-17 用户要求)
  以前所有运行期文件都堆在 exe 旁边 —— 分发服务端就等于连别人的存档和 276MB 的
  配置表 dump 一起发。而且客户端 dump 配置表的**绝对路径写死在 Lua 字节码里**,
  换台机器就静默失效。分开之后: 成品里没有任何游戏资产, 客户端第一次启动自己 dump。

★ 程序目录里留下的(都是只读的作者配置)
    routes.json  notice.json  notice_page.html  settings.default.json
    baseline.json(存档模板 —— 用户指定留在程序目录, 不进数据目录)
    exe / *.py / tools/ / docs

★ 用户数据目录布局
    saves\\       player.json, player.json.bak_*
    resources\\   cfg\\(各渠道的表 dump + __pcache\\)、roles_index.json
    state\\       local_name.txt, do_*.flag, turn_activity.json,
                  settings.json, bizdata.json, client_channel.json
    logs\\        bizserver.log, probe.log

★ 环境变量覆盖(默认值 = 用户数据目录下的同名文件)
    KLEINS_DATA_ROOT        整个数据根            %USERPROFILE%\\Saved Games\\freeKleins
    KLEINS_PLAYER_JSON      玩家存档              <root>\\saves\\player.json
    KLEINS_CFG_DIR          配置表目录            <root>\\resources\\cfg
    KLEINS_LOCAL_NAME_FILE  登录页显示名          <root>\\state\\local_name.txt
    KLEINS_TURN_ACT_JSON    回合活动当前线路      <root>\\state\\turn_activity.json
    KLEINS_ROLES_INDEX      角色 id→名字          <root>\\resources\\roles_index.json
    KLEINS_LOGOUT_FLAG      退出登录标记          <root>\\state\\do_logout.flag
    KLEINS_RESET_ROLES_FLAG 删除角色标记          <root>\\state\\do_reset_roles.flag
    KLEINS_DROP_ROLE_FLAG   丢弃角色标记          <root>\\state\\do_droprole.flag
    KLEINS_ROLE_TOOL_FLAG   角色工具标记          <root>\\state\\do_roletool.flag
    KLEINS_HTTP_LOG_BODY    fakeserver 是否记录请求体("0" 关闭)
  ★ 自检脚本靠 `KLEINS_DATA_ROOT` 把状态整个隔离到临时目录。
"""
import json
import os
import re
import shutil
import stat
import sys

# 用户数据目录的四个子目录
KINDS = ("saves", "resources", "state", "logs")


def base_dir():
    """exe/脚本自身所在目录(程序目录)。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def path(name):
    """程序目录下的某个文件(只读配置用)。"""
    return os.path.join(base_dir(), name)


def data_root():
    """用户数据根目录。"""
    env = os.environ.get("KLEINS_DATA_ROOT")
    if env:
        return env
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "Saved Games", "freeKleins")


_made = set()


def data_dir(kind):
    """用户数据下的某个子目录, 保证存在。"""
    d = os.path.join(data_root(), kind)
    if d not in _made:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass                      # 只读介质之类 → 交给调用方在写的时候报错
        _made.add(d)
    return d


def data_path(var, kind, name):
    """环境变量优先; 没设就用用户数据目录 `<kind>/<name>`。

    ★ `var` 传 None/空 表示"这个文件没有环境变量开关"。
      别写成 `os.environ.get(var) or ...` —— `os.environ.get(None)` 会抛
      **AttributeError**(不是 KeyError, `MutableMapping.get` 只吞 KeyError), 直接崩。
    """
    if var:
        env = os.environ.get(var)
        if env:
            return env
    return os.path.join(data_dir(kind), name)


def is_reparse_point(p):
    """Windows 的 junction / symlink。

    ★★ 为什么必须单独判: Python 3.8 **没有** `os.path.isjunction`, 而且 junction 的
       `st_mode` 是 `S_IFDIR`(不是 S_IFLNK) ⇒ `os.path.islink()` 对它返回 **False**。
       只有 `st_file_attributes` 里的 reparse 位认得出来。
       (踩过: 仓库里 `openKleinsServer\\cfg` 就是指向 `_re\\cfg` 的 junction。)
    """
    try:
        st = os.lstat(p)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


# ★ 迁移表: 程序目录里的旧文件 → 用户数据目录。
#   规则: **代码运行期会写的一律外置; 只读的留在程序目录**。
_MIGRATE = (
    ("player.json",         "saves",     "player.json"),
    ("roles_index.json",    "resources", "roles_index.json"),
    ("local_name.txt",      "state",     "local_name.txt"),
    ("turn_activity.json",  "state",     "turn_activity.json"),
    ("settings.json",       "state",     "settings.json"),
    # ★ bizdata.json 是从**正式服抓回来的原始回包**(body_hex) ⇒ 算游戏资产, 进 resources。
    #   它只读不写(load_bizdata 只 read), 但内容本身就是游戏数据。
    ("bizdata.json",        "resources", "bizdata.json"),
    ("do_reset.flag",       "state",     "do_reset.flag"),
    ("do_reset_roles.flag", "state",     "do_reset_roles.flag"),
    ("do_logout.flag",      "state",     "do_logout.flag"),
    ("do_droprole.flag",    "state",     "do_droprole.flag"),
    ("do_roletool.flag",    "state",     "do_roletool.flag"),
    ("bizserver.log",       "logs",      "bizserver.log"),
    ("probe.log",           "logs",      "probe.log"),
    ("cfg",                 "resources", "cfg"),           # 目录, 可能上百 MB
)

# 存档备份是 `player.json.bak_<时间戳>`, 数量不定 ⇒ 前缀扫描
_BAK_PREFIX = "player.json.bak_"


def _move_one(s, d, what, log, moved, skipped):
    """搬一个。目标已在 ⇒ 跳过(幂等); reparse point ⇒ 跳过(会搬错东西)。"""
    if os.path.exists(d):
        skipped.append(what)
        return
    if is_reparse_point(s):
        # ★★ junction/symlink: shutil.move 搬的是**链接本体**, 不是内容
        skipped.append(what + "(junction, 未动)")
        if log:
            log("   ⚠ %s 是 junction/symlink, 不搬(避免把链接本体搬走): %s" % (what, s))
        return
    try:
        if os.path.isdir(s):
            if log:
                log("★ 正在搬目录 %s -> %s (大目录可能要等几秒)…" % (s, d))
        shutil.move(s, d)
    except Exception as e:
        skipped.append(what)
        if log:
            log("   !! 搬 %s 失败(忽略): %r" % (what, e))
        return
    moved.append(what)


def migrate_legacy(log=None):
    """把程序目录里的运行期数据搬进用户数据目录。**幂等** —— 重复调用无副作用。

    返回 `(moved, skipped)` 两个列表(名字), 调用方拿去打日志。

    ★ 调用点: `bizserver.main()` 和 `fakeserver.__main__`, 都要在**第一次读存档之前**。
      放在这里而不是 playerdata.py —— fakeserver 不 import playerdata。
    """
    src = base_dir()
    moved, skipped = [], []
    try:
        dst_root = data_root()
        for kind in KINDS:
            data_dir(kind)
    except Exception as e:
        if log:
            log("!! 建用户数据目录失败, 跳过迁移: %r" % (e,))
        return moved, skipped

    for old, kind, name in _MIGRATE:
        s = os.path.join(src, old)
        try:
            if not os.path.exists(s) and not is_reparse_point(s):
                continue
        except OSError:
            continue
        _move_one(s, os.path.join(dst_root, kind, name), old, log, moved, skipped)

    # 存档备份
    try:
        for f in sorted(os.listdir(src)):
            if f.startswith(_BAK_PREFIX):
                _move_one(os.path.join(src, f),
                          os.path.join(dst_root, "saves", f),
                          f, log, moved, skipped)
    except OSError:
        pass

    if log and (moved or skipped):
        log("★ 数据目录迁移: 搬了 %d 项, 跳过 %d 项 -> %s"
            % (len(moved), len(skipped), dst_root))
    return moved, skipped


# ── 存档按「客户端(渠道+版本)」分开 (2026-10-17 用户要求) ──────────────
# ★ 为什么: 配置表早就按渠道分了 (`resources\cfg\<SubPlatType>\`), 但**存档还是共用一份**。
#   换客户端测试时会串档 —— 在国服客户端上领到的道具 id, Steam 客户端可能没有,
#   正是 5133 / 头像 64,65 / 卡池 35..40 那一类崩。
#   用户原话: 「把 saves\ 按客户端版本号+渠道(如 steam1.5.0.0)分开」。
#
# ★★★ 渠道键必须用 **subPlatType**, 绝对不能用 platType。两个理由, 第二个是硬伤:
#   ① 它是项目里渠道的**权威键** (`cfgdata` / `client_channel.json` / `cfg\<名字>\`
#      用的都是它), 粒度也更细。
#   ② **打完补丁之后 platType 会撞车** —— 补丁把 Steam 的 `PlatType`
#      从 `steam` 改成 `bingkolo`(`binpatch.PLAT_WANT`, 不改会加载 sdk_steam
#      去唤起 Steam)。实测 2026-10-17:
#        F:\KleinsPatchNew  PlatType=bingkolo   SubPlatType=steam-GL
#        D:\hxls            PlatType=bingkolo   SubPlatType=bingkolo-Win
#      ⇒ 两个客户端的 platType **精确相同**。拿它当键, 两份存档会挤进同一个目录,
#        这功能等于没做。
#   ⇒ 目录名形如 `steam-GL1.5.0.0` / `bingkolo-Win1.5.0.0`。
#   (「如 steam1.5.0.0」是用户举例时的简写, 见 `_selftest_savescope.py` 第 7 节。)
_SCOPE_BAD = re.compile(r"[^0-9A-Za-z._-]+")


def _scope_part(s):
    """把一段名字洗成能当目录名的形式; 洗空了返回空串。"""
    return _SCOPE_BAD.sub("_", str(s)).strip("._-")


def client_scope():
    """客户端身份 = `subPlatType + resVer`, 例如 `steam-GL1.5.0.0`。

    从 `state/client_channel.json` 读(fakeserver 从 HTTP 请求里刮的, 见
    `fakeserver._note_client_channel`)。业务服**不能在 import 时算死** ——
    这份文件是客户端登录后才有/才变的, 所以要每个请求重读。

    ★ 认不出来(文件没有 / 字段不全) 就返回 **None** ⇒ 调用方退回平面目录,
      也就是改动前的行为, **不是新 bug**。
    ★★★ `subPlatType` 和 `resVer` **两个都要有**, 缺一个就返回 None —— 这条踩过坑:
      2026-10-17 第一次上这套时, `client_channel.json` 是**上一版**留下的(那时还没刮
      resVer) ⇒ 作用域先算成「只有渠道」`steam-GL`, 老存档被收编进 `saves\\steam-GL\\`;
      等第一个请求带上 resVer, 作用域变成 `steam-GL1.5.0.0` ⇒ **老存档当场被孤立**,
      客户端看起来像"存档重置了"。
      ⇒ **宁可不分, 也不分一半**: 缺字段就留在平面目录等下一次结果。
      (两个真实客户端都发 resVer, 所以正常情况下不会退到这里。)
    """
    try:
        with open(os.path.join(data_root(), "state", "client_channel.json"),
                  "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    ch = _scope_part(d.get("subPlatType") or d.get("platType") or "")
    ver = _scope_part(d.get("resVer") or "")
    if not ch or not ver:
        return None
    return ch + ver


def saves_dir(scope=None):
    """存档目录: 有客户端身份 ⇒ `saves\\<渠道><版本>\\`; 没有 ⇒ `saves\\`。

    ★ 每次都 ensure 一下目录存在, **不要**学 `data_dir` 用 `_made` 缓存:
      缓存了的话, 目录被手工删掉之后我们就不再去建它, `open`/`shutil.move`
      会直接失败。(写这段自检时真踩到了: 作用域目录建过一次又被删, 收编就静默失败。)
      ★ 开销可忽略 —— `refresh_save()` 只在**客户端身份变了**的时候才调到这里。
    """
    base = data_dir("saves")
    if scope is None:
        scope = client_scope()
    if not scope:
        return base
    d = os.path.join(base, scope)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return base            # 建不出来就别把存档往不存在的目录里写
    return d


def adopt_flat_save(dst_dir):
    """把老的**平面**存档收编进某个客户端的作用域目录。**幂等**。

    只在「作用域目录里还没有 player.json」且「平面目录里有」时搬一次。

    ★ 为什么是搬不是复制: 留着平面那份, 下一个客户端启动时照样会去读它 ⇒ 串档没解决。
    ★ 只搬**自己的** —— 作用域目录里已经有存档就一个字节都不动(所以要判存在)。
    """
    base = data_dir("saves")
    if os.path.abspath(base) == os.path.abspath(dst_dir):
        return []
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except OSError:
        return []
    names = ["player.json"]
    try:
        names += [f for f in os.listdir(base) if f.startswith(_BAK_PREFIX)]
    except OSError:
        pass
    moved = []
    for n in names:
        s = os.path.join(base, n)
        d = os.path.join(dst_dir, n)
        if not os.path.exists(s) or os.path.exists(d) or is_reparse_point(s):
            continue
        try:
            shutil.move(s, d)
            moved.append(n)
        except Exception:
            pass                       # 搬不动就留着(退回平面目录读, 不算坏)
    return moved


def scoped_state_path(name):
    """按当前客户端作用域分的运行期状态文件: `state\\<scope>\\<name>`。

    认不出客户端(没握手文件 / 字段不全) ⇒ 退回 `state\\<name>`(平面, 行为同改动前)。

    ★ 哪些东西该放这儿: **游戏会改、而且改的就是"这个客户端这份档"的设置**。
      目前是 `settings.json`(游戏里改设置就写它 —— 舞台背景那类就在里面)。
    ★ 不该放的: 全局开关(`turn_activity.json`)、一次性标记(`do_*.flag`)、
      抓回来的原始数据(`bizdata.json`) —— 那些本来就该所有客户端共用。
    """
    base = data_dir("state")
    sc = client_scope()
    if not sc:
        return os.path.join(base, name)
    d = os.path.join(base, sc)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return os.path.join(base, name)
    return os.path.join(d, name)


def local_name_file():
    """登录页显示的用户名文件。★ **故意不分作用域** —— 理由见 publish_local_name。"""
    return os.path.join(data_dir("state"), "local_name.txt")


def publish_local_name():
    """把**当前客户端存档**里的 `actor.name` 写进 `state\\local_name.txt`。

    ★★ 为什么这个文件**不按作用域分**: 读它的是**客户端**, 而客户端在登录页那一刻
      **还不知道自己是哪个作用域**(它就是来问服务端的)。所以只能由服务端在
      **认出渠道的那一刻**把它刷新成"这次要登的这个客户端"的名字。
      ⇒ 它是**当前客户端的缓存**, 不是每客户端一份。

      2026-10-17 的 bug 正是它没被刷新: 国服把自己改的名字 `22222` 写进去之后,
      Steam 再登录也读到 `22222`; 反过来 Steam 写 `Kleins`, 国服就显示 `Kleins`。
      更早还有"整个会话都显示兜底名"的情况 —— 那是客户端把路径**只在加载时算一次**,
      那次没命中的话这一局就再也读不到文件了(客户端侧也一起修了)。

    存档不存在 / 名字为空 ⇒ 写**空字符串** —— 客户端会显示兜底名「向导」。
    这正是用户要的「**无数据时写向导, 有数据时写用户名**」。
    """
    name = ""
    try:
        sc = client_scope()
        p = os.path.join(saves_dir(sc), "player.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
            if isinstance(d, dict):
                a = d.get("actor")
                if isinstance(a, dict) and a.get("name"):
                    name = str(a["name"]).strip()
    except Exception:
        name = ""
    try:
        with open(local_name_file(), "w", encoding="utf-8") as f:
            f.write(name)
        return True
    except Exception:
        return False


def summary():
    """一行式现状描述, 启动横幅用。"""
    return "程序目录 %s | 数据目录 %s" % (base_dir(), data_root())
