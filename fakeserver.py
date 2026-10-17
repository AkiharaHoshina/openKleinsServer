#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
《环行旅舍》假服务端 —— HTTP 层
监听 127.0.0.1:10300, 处理客户端的所有 HTTP 请求。

路由表每次请求都从 routes.json 重新读取 => 改完立即生效, 不用重启。
routes.json 结构:
{
  "POST /full/path": { ...json... },     # 精确匹配 方法+路径
  "/path/suffix":    { ...json... },     # 路径后缀匹配
  "__default__":     {"code": 0}         # 兜底
}
"""
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

import paths
import cfgdata

def _base_dir():
    """打包成 exe 后 __file__ 指向临时解包目录, 必须改用 exe 自身所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


ROOT = _base_dir()
# ★ 2026-10-17: 运行期数据搬进**用户数据目录**(见 paths.py)。
#   ★★ 这里只能用 paths.*, 不能借 playerdata 的 —— fakeserver **不 import playerdata**。
ROUTES = os.path.join(ROOT, "routes.json")
# ★ 资源更新下发的根目录（见 serve_res）。结构镜像客户端的 Win\。
#   ★★ 2026-10-17: **这条路已经废了**，见下面「关于 /res/ 更新通道」那段。
#      留着只是为了万一要手动取文件，**绝对不要再往 baseapi/v1/update 里塞 updateList**。
RES_ROOT = os.path.join(ROOT, "res")
LOG = paths.data_path(None, "logs", "probe.log")
_lock = threading.Lock()
_seq = [0]


def log(msg):
    with _lock:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    print(msg, flush=True)


# ---------------- 公告正文页 (游戏内嵌浏览器) ----------------
# ★ 游戏「公告」界面右侧是个 WebView: 点左侧条目会导航到
#       GET /noticeweb?id=<公告id>&platType=..&subPlatType=..
#   (实测自 probe.log)。这里给它回一个 HTML 页, 内容**直接读 notice.json**
#   —— 和滚动横幅同一个源, 改一处两边都变。
#   注意: 其余所有路由仍然回 {code,msg,data} 信封的 JSON, 不受影响。
NOTICE_JSON = os.path.join(ROOT, "notice.json")
NOTICE_TPL = os.path.join(ROOT, "notice_page.html")   # 外置排版模板, 热改

# ---------------- 「活动切换」页 (2026-10-15) ----------------
# 为什么要有这个: 回合活动(turn_activity)的三条线**客户端同一时刻只认一条**
# (model_turn_activity_data 全局只有一个"当前活动"), 详见 docs §8.51。
# 所以换线只能改服务端 —— 这里给它一个**游戏内可点的入口**:
#   公告列表里多一条「活动切换」(id=1002) -> 点开是这个页面 -> 按钮改 turn_activity.json
#   -> 服务端(10301)每个 1401 请求重读该文件 -> 重新登录即生效。
# ★ 走 HTML 而不是客户端补丁: 零客户端改动、零崩溃风险。
SWITCH_NOTICE_ID = 1002
ACT_JSON = paths.data_path("KLEINS_TURN_ACT_JSON", "state", "turn_activity.json")

# ★★★ 必须是**三条**，和 `playerdata.TURN_ACTIVITY_LINES` 以及客户端写死的
#   `model_turn_activity_data.LimitActivityTimeId` 对齐:
#       Emotion = 20001 / Carmen = 50001 / Sleep = 70001
#   ★ 2026-10-17 踩过: 这里原来只有两条（缺 20001）。`turn_activity.json` 在
#     用户数据目录里被清空时，页面就退化到这份 fallback ⇒「活动切换」页
#     **根本不显示情绪线**，而页面文案还写着"一共三条线"，自相矛盾。
#   ⇒ 以后加/删线路，三处要一起改（这里 / playerdata / 客户端）。
_ACT_FALLBACK_LINES = [
    {"time_id": 50001, "name": "幕前序曲", "desc": "卡门线 (Carmen)"},
    {"time_id": 70001, "name": "伴好梦入眠", "desc": "寝取线 (Sleep)"},
    {"time_id": 20001, "name": "非等张共鸣",
     "desc": "情绪线 (Emotion) — 无章节数据，切过去可能是空白页"},
    # ★ 初霜与春风: 是**章节目录里的活动章节线**（章节 4011/4012/4013），
    #   客户端的 LimitActivityTimeId 只有三条分支，把它设成"当前活动"会让
    #   ServiceDataInitOK 三个分支全不匹配 ⇒ 主页活动入口整个消失。
    #   所以它只能**列出来当说明**，不能点。
    {"time_id": 80001, "name": "初霜与春风", "selectable": False,
     "desc": "章节线 (4011~4013) — 不是回合活动，从关卡列表进"},
]


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))


def _read_act():
    """读 turn_activity.json -> (当前 time_id, lines 列表)。坏了就用兜底, 绝不抛。"""
    try:
        with open(ACT_JSON, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    try:
        cur = int(cfg.get("time_id"))
    except Exception:
        cur = _ACT_FALLBACK_LINES[0]["time_id"]
    lines = cfg.get("lines")
    if not isinstance(lines, list) or not lines:
        lines = _ACT_FALLBACK_LINES
    return cur, lines


def _write_act(tid):
    """把当前线路写回 turn_activity.json(保留 lines 和 _说明)。"""
    try:
        with open(ACT_JSON, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg["time_id"] = int(tid)
    with open(ACT_JSON, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def switch_html():
    """「活动切换」页: 一段说明 + 每条线一个按钮(当前那条标出来)。

    ★★ `"selectable": false` 的条目渲染成**不可点的说明行**（2026-10-16 新增）。
      为什么需要: 「初霜与春风」是**章节目录里的活动章节线**（章节 4011/4012/4013），
      客户端 `model_turn_activity_data.LimitActivityTimeId` 只写死三条
      （Emotion=20001 / Carmen=50001 / Sleep=70001），没有第四条分支 ——
      把它设成"当前回合活动"会让 `ServiceDataInitOK` 三个分支全不匹配，
      `TaskActivityId` 留空 ⇒ 主页活动入口消失 + `dataconfig_activity` 取到 nil。
      所以它只能列在这里当说明，不能当"当前线路"。
    """
    cur, lines = _read_act()
    btns = []
    offs = []
    for ln in lines:
        try:
            tid = int(ln.get("time_id"))
        except Exception:
            continue
        name = _esc(ln.get("name") or tid)
        desc = _esc(ln.get("desc") or "")
        if ln.get("selectable") is False:
            # ★ 不能点: 用 div + 内联样式(不碰 CSS, 免得影响其它页)
            offs.append(
                '<div class="swbtn" style="opacity:.62;cursor:default">'
                '<span class="nm">%s <span style="font-size:.8em">[不可切换]</span></span>'
                '<span class="ds">%s</span></div>' % (name, desc))
            continue
        here = (tid == cur)
        btns.append(
            '<a class="swbtn%s" href="/switchact?to=%d">'
            '<span class="nm">%s%s</span>'
            '<span class="ds">%s</span></a>'
            % (" cur" if here else "", tid, name,
               "（当前启用）" if here else "", desc))
    body = (
        "<p>《环行旅舍》的<b>回合活动</b>一共三条线。客户端的活动数据模型同一时刻"
        "<b>只认一条线</b>（章节 / 任务 / 商店 / 剧情页全靠「当前活动」解析），"
        "所以三条不能同时开着 —— 这里提供切换。</p>"
        "<p>点下面的按钮切换。<b>切换后需要重新登录</b>（退回登录页再进，或重开游戏）："
        "活动时间窗只在登录时拉取一次。</p>"
        "<div class=\"swlist\">%s</div>"
        "<p class=\"hint\">当前：<b>%s</b>（time_id=%d）</p>"
        % ("".join(btns), _esc(_name_of(cur, lines)), cur))
    if offs:
        body += (
            "<hr><p><b>其它活动线（不是回合活动，客户端没有对应入口，"
            "不能设为「当前线路」）</b></p>"
            "<div class=\"swlist\">%s</div>"
            "<p class=\"hint\">这些活动<b>直接走关卡列表</b>进：章节会照常出现在"
            "关卡选择里，进去打就行。强制把它的 time_id 设成「当前回合活动」会让"
            "主页的活动入口整个消失（客户端三条线是写死的），所以这里不给按钮。</p>"
            % "".join(offs))
    return _render("活动切换", body)


def switch_result_html(tid):
    """切换完成后的小页: 告诉用户切到哪了 + 怎么生效 + 退出登录 + 返回按钮。"""
    _, lines = _read_act()
    body = (
        "<p>已切换到 <b>%s</b>（time_id=%d）。</p>"
        "<p>点下面的<b>退出登录</b>，游戏会断开与本地服务端的连接并回到登录页，"
        "重新登录后活动入口就变成这条线了。"
        "（也可以自己退回登录页 / 重开游戏。）</p>"
        "<p><a class=\"swbtn quit\" href=\"/logout\">退出登录</a></p>"
        "<p><a class=\"swbtn back\" href=\"/noticeweb?id=%d\">← 返回活动切换</a></p>"
        % (_esc(_name_of(tid, lines)), tid, SWITCH_NOTICE_ID))
    return _render("活动切换", body)


RESET_NOTICE_ID = 1003
# ★ 和 playerdata.RESET_FLAG / RESET_ROLES_FLAG 保持一致。HTTP 层只**写标记**,
#   真正的动作由业务服在下一个请求最前面兑现(然后推 1009 让客户端重登)。
RESET_ALL_FLAG = paths.data_path(None, "state", "do_reset.flag")
RESET_ROLES_FLAG = paths.data_path(None, "state", "do_reset_roles.flag")
# ★ 和 playerdata.ROLE_TOOL_FLAG 保持一致(JSON: {"mode":..,"ids":[..]})。
#   两边都认 `KLEINS_ROLE_TOOL_FLAG` 环境变量 —— 自检要指到临时路径。
ROLE_TOOL_FLAG = paths.data_path("KLEINS_ROLE_TOOL_FLAG", "state",
                                 "do_roletool.flag")
# ★ 故意**不 import playerdata**(fakeserver.exe 是单独打包的, PyInstaller 抓不到
#   函数内的动态 import)。存档路径两边都认 `KLEINS_PLAYER_JSON`。
PLAYER_JSON = paths.data_path("KLEINS_PLAYER_JSON", "saves", "player.json")


def _write_flag(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())) + "\n")
        return True
    except Exception:
        return False


# ==================== 客户端渠道握手 (2026-10-17) ====================
# ★★ 为什么要有这个
#   业务服(10301)其实**不知道**面前是国服客户端还是 Steam 客户端 ——
#   `CSLoginReq` 里根本没有渠道字段(bizserver 的 `_LOGIN_IDENT_FIELDS` 只有
#   UserId/Account/Device/Di/Pi)。而**HTTP 层看得到**: WebView 的
#   `GET /noticeweb?id=..&platType=..&subPlatType=..` 实测就带着这两个 query。
#   ⇒ 这里刮出来写成 `state/client_channel.json`, 业务服在**每个请求开头**兑现
#     (和 do_reset.flag 一个套路: 两个进程两套端口, 只能靠文件通信)。
#
# ★ 拿到它有什么用: `cfgdata` 用它决定读哪个渠道的配置表 dump 目录。
#   "表最多的那个子目录"只是启发式 —— 国服和 Steam 两份都 dump 过之后就不可靠了。
CHANNEL_STATE = paths.data_path(None, "state", "client_channel.json")
_CH_SEEN = []           # 上次写过的身份(元组), 只用来"没变就不写盘 / 不刷日志"
_CH_LAST = {}           # ★ 跨请求累积的字段(渠道在一个请求里, 版本在另一个里)

_CH_SUB = ("subPlatType", "sub_plat_type", "subplattype", "SubPlatType")
_CH_PLAT = ("platType", "plat_type", "plattype", "PlatType")
_CH_VER = ("svnVersion", "svn_version", "SvnVersion", "version", "Version")
# ★ 客户端版本号 `resVer`(实测 `{"svnVersion":"82743","resVer":"1.5.0.0",...}`)。
#   用户要按「渠道+版本」给存档分子目录(`steam-GL1.5.0.0`), 所以要单独刮一个。
#   注意别和 `svnVersion` 混了: 后者是 svn 号(82743), 前者才是 1.5.0.0。
_CH_RES = ("resVer", "resver", "ResVer", "res_version", "resourceVersion")
_CH_KEYS = {"subPlatType": _CH_SUB, "platType": _CH_PLAT}


def _grab(text, key):
    """在文本里找 `key` 后面跟的值（正则兜底用）。

    支持三种写法: `key=value` / `"key":"value"` / `key: value`。
    ★★ 必须判**词边界**: `platType` 是 `subPlatType` 的子串（大小写不敏感搜索时),
      不判就会把 `subPlatType` 的值当成 `platType` 读出来。实测客户端两种都会发
      (JSON body 和 `/noticeweb?..&subPlatType=..` 的 query)。
    """
    if not text:
        return None
    low = text.lower()
    kk = key.lower()
    start = 0
    while True:
        i = low.find(kk, start)
        if i < 0:
            return None
        if i == 0 or not (low[i - 1].isalnum() or low[i - 1] == "_"):
            break                       # 前面不是字母/数字/下划线 ⇒ 真是这个词
        start = i + 1
    k = i + len(key)
    while k < len(text) and text[k] in '"\'=: \t':
        k += 1
    k2 = k
    while k2 < len(text) and (text[k2].isalnum() or text[k2] in "._-"):
        k2 += 1
    return text[k:k2] or None


def _scrape_from_text(text):
    """从一个字符串里刮 {subPlatType, platType, svn}。空 dict = 没刮到。

    ★ 先按 JSON 解析(实测客户端 body 就是 JSON), 失败再退回正则 ——
      正则那套是为了 `/noticeweb?..&subPlatType=..` 这种 query 形态。
    """
    out = {}
    t = (text or "").strip()
    if t.startswith("{"):
        try:
            obj = json.loads(t)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            low = {str(k).lower(): v for k, v in obj.items()}
            for field, names in _CH_KEYS.items():
                for nm in names:
                    v = low.get(nm.lower())
                    if isinstance(v, str) and v:
                        out[field] = v
                        break
            for nm in _CH_VER:
                v = low.get(nm.lower())
                if isinstance(v, str) and v:
                    out["svn"] = v
                    break
            for nm in _CH_RES:
                v = low.get(nm.lower())
                if isinstance(v, str) and v:
                    out["resVer"] = v
                    break
            return out
    for field, names in _CH_KEYS.items():
        for nm in names:
            v = _grab(text, nm)
            if v:
                out[field] = v
                break
    for nm in _CH_VER:
        v = _grab(text, nm)
        if v:
            out["svn"] = v
            break
    for nm in _CH_RES:
        v = _grab(text, nm)
        if v:
            out["resVer"] = v
            break
    return out


def _note_client_channel(path, body_text):
    """从请求里刮渠道, 变了就写 state/client_channel.json。

    ★ 尽力而为 / 绝不抛: 这个功能挂了也不能影响任何正常请求(照抄 _write_flag 的风格)。

    ★★ 2026-10-17: 必须**跨请求合并**, 不能只看当前这一个请求。
      实测这几个字段是分散在不同请求里的:
        `/noticeweb?id=..&platType=..&subPlatType=..`  → 只有渠道
        `baseapi/v1/update` / `config` 的 body          → 只有 svnVersion / resVer
      只看单个请求的话, 先来的那条没版本、后到的那条没渠道 ⇒ 永远拼不出完整身份。
      (`client_scope()` 要靠 subPlatType + resVer 拼存档目录名。)
    """
    try:
        found = {}
        found.update(_scrape_from_text(body_text))
        for k, v in _scrape_from_text(urlparse(path).query).items():
            found.setdefault(k, v)                          # query 补 body 缺的
        if not found:
            return
        # 累积: 只覆盖"这一条真的带了"的字段, 其余保留上次的
        for k, v in found.items():
            if v:
                _CH_LAST[k] = v
        sub = _CH_LAST.get("subPlatType")
        if not sub:
            return                                          # 还没见过渠道 ⇒ 不写
        mark = (sub, _CH_LAST.get("platType"), _CH_LAST.get("svn"),
                _CH_LAST.get("resVer"))
        if _CH_SEEN and _CH_SEEN[0] == mark:
            return                      # 没变 ⇒ 不写盘、不刷日志
        _CH_SEEN[:] = [mark]
        found = dict(_CH_LAST)
        found["at"] = int(time.time())

        os.makedirs(os.path.dirname(CHANNEL_STATE), exist_ok=True)
        with open(CHANNEL_STATE, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False)

        # ★ 顺手把渠道目录建出来 —— 客户端是 Lua, **建不了目录**
        #   (LuaJIT 没有 mkdir, io.open 只能建文件)。不建的话客户端只能退回平面目录,
        #   那正是我们要修的老 bug。
        try:
            cdir = os.path.join(paths.data_dir("resources"), "cfg", sub)
            os.makedirs(cdir, exist_ok=True)
        except Exception:
            cdir = "（建目录失败）"
        # ★★★ 2026-10-17: 顺手刷新「登录页用户名」。
        #   这个时刻是**唯一正确**的时刻: 渠道/版本刚认出来(所以知道读哪个作用域的存档),
        #   而登录页还没渲染(客户端马上才会显示它)。
        #   不做的话登录页会显示**上一个客户端**的名字 —— 用户实测:
        #   国服改名成 22222 之后, Steam 登录也显示 22222。
        #   ★ 存档还没建出来 ⇒ 写空字符串 ⇒ 客户端显示兜底名「向导」
        #     (= 用户要的「无数据时向导, 有数据时用户名」)。
        try:
            paths.publish_local_name()
        except Exception:
            pass
        log("★ 客户端渠道: %s  ->  写 %s ; 配置表目录 %s"
            % (found, CHANNEL_STATE, cdir))
    except Exception:
        pass


def reset_html():
    """公告里的「游戏工具」页: 重置存档 / 重置角色 / 角色工具。

    ★ 2026-10-16 用户要求: 公告里这条的标题从「重置数据」改成「游戏工具」
      (这一页早就不只是重置了, 还挂着角色工具)。
    """
    body = (
        "<p>本地服务端的工具页。三个功能的区别：</p>"
        "<p class=\"hint\"><b>重置存档</b> —— 整份存档回基准档："
        "角色 / 道具 / 关卡进度 / 邮件 / 任务……<b>全部重来</b>。</p>"
        "<p><a class=\"swbtn quit\" href=\"/resetall\">重置存档</a></p>"
        "<p class=\"hint\"><b>重置角色数据</b> —— <b>只</b>把角色部分回基准档"
        "（角色列表 + 养成进度 + 编队），<b>背包道具和关卡进度不动</b>。</p>"
        "<p><a class=\"swbtn quit\" href=\"/resetroles\">重置角色数据</a></p>"
        "<p class=\"hint\">点下去之后游戏会<b>自动回到登录页</b>，"
        "重新登录就能看到新数据了。</p>"
        "<hr>"
        "<p class=\"hint\"><b>角色工具</b> —— 按名字或 id 搜角色，可以"
        "<b>删除</b>（把角色从存档里抹掉）或 <b>获得</b>"
        "（没有就给角色本身；已经有了就发该角色的补充协议）。</p>"
        "<p><a class=\"swbtn quit\" href=\"/roletool\">角色工具</a></p>"
    )
    return _render("游戏工具", body)


# ---------------------------------------------------------------------------
# 删除角色(2026-10-16 用户要求: 反复重建"没有该舍友"这个前提)
# ★ 只**写标记**, 真正的删除由业务服在下一个请求最前面兑现(然后推 1009 让客户端重登)。
#   和「重置数据」完全一个套路。
# ---------------------------------------------------------------------------
def _read_owned_roles():
    """读存档里的角色 -> [(id, gain_count)]。读不到就回空表(不抛)。"""
    try:
        with open(PLAYER_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
        gc = d.get("role_gain_count") or {}
        out = []
        for rid in (d.get("roles") or {}).keys():
            try:
                out.append((int(rid), int(gc.get(rid, 0))))
            except (TypeError, ValueError):
                continue
        out.sort()
        return out
    except Exception:
        return None


def _write_tool_flag(mode, ids):
    """写「角色工具」标记(JSON: mode + ids), 业务服下一个请求兑现。"""
    try:
        os.makedirs(os.path.dirname(ROLE_TOOL_FLAG), exist_ok=True)
        with open(ROLE_TOOL_FLAG, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "ids": [int(i) for i in ids]}, f)
        return True
    except Exception:
        return False


ROLES_INDEX = paths.data_path("KLEINS_ROLES_INDEX", "resources",
                              "roles_index.json")

TOOL_MODES = [("drop", "删除角色", "把这个角色从存档里删掉（含获得次数）"),
              ("give", "获得角色",
               "没有这个角色 → 给角色本身并弹获得界面；已经有了 → 发补充协议，"
               "**不会**弹获得界面")]


def _load_role_index():
    """角色 id(str) -> 名字。

    ★★★ 缺文件就**现场从配置表生成**（2026-10-17）。
      以前必须手动跑 `python tools\\make_roles_index.py`；不跑的话
      `_load_role_index()` 返回空表，而「角色工具」页是**遍历这张表**渲染的
      ⇒ 页面**整个是空的**（老注释写"退化成只显示 id"是错的，实测什么都没有）。
      现在：缺文件 → `cfgdata.ensure_roles_index()` 现场算一份再读。
    """
    def _read():
        try:
            with open(ROLES_INDEX, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    d = _read()
    if d:
        return d
    try:
        wrote, n = cfgdata.ensure_roles_index(ROLES_INDEX, log=log)
        if wrote and n:
            d = _read()
    except Exception as e:
        log("★ 角色名索引生成失败: %r" % (e,))
    return d


def _write_tool_flag(mode, ids):
    try:
        os.makedirs(os.path.dirname(ROLE_TOOL_FLAG), exist_ok=True)
        with open(ROLE_TOOL_FLAG, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "ids": [int(i) for i in ids]}, f)
        return True
    except Exception:
        return False


def roletool_html(mode="drop", q="", filt=""):
    """角色工具页: 模式切换 + 筛选 + 可点的角色列表(+ 一个搜索框)。

    ★★ 2026-10-16 用户实测: **这个 WebView 的输入框打不了字**。
       原因是游戏用的 Vuplex WebView 没开 `Native2DModeEnabled`(原生 2D 模式) ——
       这种模式下点击会被翻译成网页点击(所以链接一直好用), 但**键盘事件根本不会
       转发给网页**; `NativeOnScreenKeyboardEnabled` 又是移动端专用的。
       这是打包好的 C# 行为, 从 Lua 侧改不了 ⇒ **页面必须不依赖打字也能用**。
       ⇒ 主界面改成"全部角色列表, 点一下就执行", 搜索框保留(能用更好, 不能用不影响)。
    """
    if mode not in ("drop", "give"):
        mode = "drop"
    if filt not in ("", "owned", "missing"):
        filt = ""
    idx = _load_role_index()
    owned = _read_owned_roles()
    owned_map = dict(owned) if owned else {}

    # ---- 模式切换(两个 tab) ----
    tabs = "".join(
        "<a class=\"swbtn%s\" href=\"/roletool?mode=%s&amp;filt=%s\">"
        "<span class=\"nm\">%s</span><span class=\"ds\">%s</span></a>"
        % (" cur" if m == mode else "", m, _esc(filt), _esc(title), _esc(desc))
        for m, title, desc in TOOL_MODES
    )

    # ---- 筛选(也是链接, 不用输入) ----
    filters = "".join(
        "<a class=\"swbtn%s\" style=\"display:inline-block;margin:.2rem .4rem .2rem 0\""
        " href=\"/roletool?mode=%s&amp;filt=%s\">"
        "<span class=\"nm\">%s</span></a>"
        % (" cur" if f == filt else "", _esc(mode), f, _esc(label))
        for f, label in (("", "全部"), ("missing", "未拥有"), ("owned", "已拥有"))
    )

    # ---- 挑出要显示的角色 ----
    q = (q or "").strip()
    hits = []
    for k, name in idx.items():
        try:
            rid = int(k)
        except ValueError:
            continue
        if q:
            if q.isdigit():
                # ★ 输入 id -> **只显示完全相符的**
                if str(rid) != q:
                    continue
            elif q not in name:
                # ★ 输入名字 -> 显示所有相关项
                continue
        is_owned = rid in owned_map
        if filt == "owned" and not is_owned:
            continue
        if filt == "missing" and is_owned:
            continue
        hits.append((rid, name))
    hits.sort()

    rows = []
    for rid, name in hits:
        gc = owned_map.get(rid)
        if gc is None:
            state = "未拥有"
        else:
            state = "已拥有（获得 %d 次）" % gc
        # ★★ 这里**绝对不能加 `quit` 类** —— 模板里 `.swbtn.quit{display:inline-block}`
        #    会盖掉 `.swbtn{display:block}`, 角色行就变成行内元素、按长短换行了
        #    (用户报的"每个角色按钮应该占据一行无论长短")。
        #    删除模式用内联的红色边框提示, 不碰 display。
        #
        # ★★ 动作文案要**如实反映会发生什么**(用户实测踩到过):
        #    「获得」对已有角色是**发补充协议**, 不会弹获得界面 ——
        #    原来一律写"点这里获得", 玩家点了没反应就以为坏了。
        if mode == "drop":
            act = "点这里删掉"
        elif gc is None:
            act = "点这里获得角色（会弹获得界面）"
        else:
            act = "点这里发补充协议（不弹获得界面）"
        rows.append(
            "<a class=\"swbtn\"%s href=\"/roletool?mode=%s&amp;id=%d\">"
            "<span class=\"nm\">%s</span>"
            "<span class=\"ds\">id %d · %s · %s</span></a>"
            % (" style=\"border-color:#c98a8a\"" if mode == "drop" else "",
               mode, rid, _esc(name or "（无名字）"), rid, _esc(state), _esc(act)))
    if not rows:
        rows.append("<p class=\"hint\">没有符合条件的角色。</p>")

    mode_desc = dict((m, d) for m, _t, d in TOOL_MODES).get(mode, "")
    owned_note = ("<p class=\"hint\">当前拥有 <b>%d</b> / 共 %d 个角色"
                  "（上面显示 %d 个）。</p>"
                  % (len(owned_map), len(idx), len(hits))) \
        if owned is not None else ""

    # ★ 用列表拼, 不要用 `%` 数占位符 —— 之前就是这么把 rows 漏掉的(手测抓到)。
    # ★★ 没有搜索框: 这个内置浏览器(Vuplex, 没开原生 2D 模式)收不到键盘事件,
    #    输入框打不了字(用户实测)。整页改成"点一下就行"。
    #    (URL 仍支持 `?q=` 过滤, 只是页面上不给输入口了。)
    parts = [
        "<p>%s</p>" % _esc(mode_desc),
        tabs,
        "<p class=\"hint\">筛选：</p>",
        filters,
        owned_note,
        "<p class=\"hint\">下面每个角色<b>点一下就直接执行</b>。</p>",
        "".join(rows),
        "<hr>",
        "<p class=\"hint\">几秒内生效：<b>获得</b>会立刻到账（新角色会弹获得界面）；"
        "<b>删除</b>在你<b>退出公告回到主页时</b>自动重新登录，"
        "重新登录后角色就没了。</p>",
    ]
    return _render("角色工具", "".join(parts))


def roletool_result_html(mode, ids, ok):
    if not ids:
        return _render("角色工具", "<p>没给角色 id，什么都没做。</p>"
                       + "<p><a class=\"swbtn\" href=\"/roletool\">返回角色工具</a></p>")
    ids_s = "、".join(str(i) for i in ids)
    if not ok:
        return _render("角色工具", "<p>写标记失败，没能执行。</p>")
    if mode == "drop":
        # ★ 条件式重登(用户要求): 不是立刻踢, 而是等玩家从公告退回主页时才重登
        tail = ("<p><b>已记下。</b>你继续点「关闭」退出公告、回到主页时，"
                "游戏会自动重新登录一次，重新登录后这些角色就没了。</p>")
    else:
        tail = ("<p>几秒内角色/道具会到账：没有的角色会给角色本身并弹出获得界面，"
                "已经有了的发补充协议。获得<b>不会</b>把你踢下线。</p>")
    return _render("角色工具",
                   "<p><b>已请求%s：%s</b></p>%s"
                   % ("删除" if mode == "drop" else "获得", _esc(ids_s), tail)
                   + "<p><a class=\"swbtn\" href=\"/roletool?mode=%s\">返回角色工具</a></p>"
                   % _esc(mode))


def reset_result_html(kind):
    if kind == "all":
        name, flag = "重置存档", RESET_ALL_FLAG
    else:
        name, flag = "重置角色数据", RESET_ROLES_FLAG
    if _write_flag(flag):
        body = ("<p><b>已请求%s。</b></p>"
                "<p>几秒内游戏会自己回到登录页，重新登录后就是新数据了。</p>"
                % name)
    else:
        body = "<p>写标记失败，没能%s。</p>" % name
    return _render("游戏工具", body)


def logout_result_html():
    """点了「退出登录」之后的小页。

    ★ 这里只能**写标记** —— 推 1009 得由业务服来做(它才拿着那个 socket),
      两个进程两套端口, HTTP 层够不着。业务服下一个请求进来时兑现
      (客户端心跳 ~5 秒一次, 所以最多等几秒)。
    ★ 客户端收到 1009 后执行的是正牌登出流程, 见 `_re\\inject_logout.py`。
    """
    try:
        # ★ 故意**不 import playerdata** —— fakeserver.exe 是单独打包的,
        #   PyInstaller 抓不到函数内的动态 import, 运行时可能 ModuleNotFoundError。
        #   路径要和 playerdata.LOGOUT_FLAG 保持一致(两边都认这个环境变量)。
        _p = paths.data_path("KLEINS_LOGOUT_FLAG", "state", "do_logout.flag")
        os.makedirs(os.path.dirname(_p), exist_ok=True)
        with open(_p, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())) + "\n")
        ok = True
    except Exception as e:
        ok = False
        _e = e
    if ok:
        body = (
            "<p><b>已请求退出登录。</b></p>"
            "<p>游戏会在几秒内（最多一个心跳）自己回到登录页，"
            "走的是和<b>设置 → 用户 → 退出登录</b>完全一样的流程。</p>"
            "<p>然后重新登录，活动线就生效了。</p>"
        )
    else:
        body = ("<p>写标记失败，没能退出登录：<code>%s</code></p>"
                % _esc(str(_e)))
    return _render("退出登录", body)


def _name_of(tid, lines):
    for ln in lines:
        try:
            if int(ln.get("time_id")) == int(tid):
                return ln.get("name") or str(tid)
        except Exception:
            continue
    return str(tid)


def _render(title, body_html):
    """套 notice_page.html 模板。★ 这里的 body **不转义**(按钮是 HTML)。"""
    tpl = None
    try:
        with open(NOTICE_TPL, "r", encoding="utf-8-sig") as f:
            tpl = f.read()
    except Exception:
        tpl = None
    if not tpl:
        tpl = _NOTICE_TPL_FALLBACK
    return tpl.replace("{{TITLE}}", _esc(title)).replace("{{BODY}}", body_html)


def notice_html(req_path=""):
    """按 notice.json 生成公告正文页。文件缺失/损坏时回一个说明页, 绝不抛。

    ★ 排版模板外置在 _re\\notice_page.html (占位符 {{TITLE}} / {{BODY}}),
      这样调样式**不用重打包**；模板文件不在时才用下面的兜底。
    ★ id=SWITCH_NOTICE_ID 走「活动切换」页(见上)。
    """
    try:
        q = parse_qs(urlparse(req_path).query)
        nid = int((q.get("id") or ["0"])[0])
    except Exception:
        nid = 0
    if nid == SWITCH_NOTICE_ID:
        return switch_html()
    if nid == RESET_NOTICE_ID:
        return reset_html()
    try:
        with open(NOTICE_JSON, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    title = str(cfg.get("title") or "公告")
    lines = cfg.get("lines") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    body = "".join("<p>%s</p>" % _esc(x) for x in lines if str(x).strip())
    if not body:
        body = "<p>（公告内容为空）</p>"
    return _render(title, body)


_NOTICE_TPL_FALLBACK = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>{{TITLE}}</title><style>"
    "html{font-size:2.1vw;}"
    "html,body{margin:0;padding:0;background:#f4f2ee;color:#1c1c1c;"
    "font-family:'Microsoft YaHei','PingFang SC',sans-serif;"
    "-webkit-text-size-adjust:100%;text-size-adjust:100%;}"
    ".wrap{max-width:44rem;margin:0 auto;padding:2.4rem 2.8rem 6rem;}"
    "h1{font-size:1.6rem;margin:0;color:#b8912f;letter-spacing:.3rem;}"
    ".rule{height:.14rem;background:#c8a24a;opacity:.5;margin:1rem 0 2rem;}"
    "p{font-size:1.08rem;line-height:1.95;margin:0 0 1.3rem;}"
    "</style></head><body><div class=\"wrap\"><h1>{{TITLE}}</h1>"
    "<div class=\"rule\"></div>{{BODY}}</div></body></html>"
)


# ---------------- 日志隐私化 ----------------
# ★ probe.log 会**一直留在磁盘上**, 所以手机号/证件号/密码/设备指纹
#   不该原样躺进去。这是"防日志泄漏", 不是防攻击。
#
#   要处理的两种写法: 正常 JSON (`"phone":"..."`)
#   和**被转义进字符串里的 JSON** (`\"phone\":\"..."` —— 客户端把设备信息
#   塞在 logInfo 这个字符串里, 所以两种都会出现)。
# 开关: 设 KLEINS_HTTP_LOG_BODY=0 就**完全不记 body**(默认记, 但已打码)。
LOG_BODY = os.environ.get("KLEINS_HTTP_LOG_BODY", "1") != "0"

_Q = r'\\?"'          # 引号: 可能是 " 也可能是 \" (转义形式)


def _mask(text):
    """把日志文本里的个人身份信息打码。"""
    if not text:
        return text
    import re
    subs = (
        # 手机号: 13120035138 -> 131****5138
        (r"(" + _Q + r"phone" + _Q + r"\s*:\s*" + _Q + r")(\d{3})\d+(\d{4})(" + _Q + r")",
         r"\1\2****\3\4"),
        # 身份证: 只留前 2 后 4
        (r"(" + _Q + r"(?:idNum|pi|idCard|identity)" + _Q + r"\s*:\s*" + _Q + r")"
         r"(\w{2})\w+(\w{4})(" + _Q + r")", r"\1\2************\3\4"),
        # 密码 / token: 一律抹掉
        (r"(" + _Q + r"(?:password|passwd|pwd)" + _Q + r"\s*:\s*" + _Q + r")[^\"\\]*(" + _Q + r")",
         r"\1***\2"),
        (r"(" + _Q + r"token" + _Q + r"\s*:\s*" + _Q + r")(\w{4})\w*(" + _Q + r")", r"\1\2***\3"),
        # 设备指纹: 只留前 8 位
        (r"(" + _Q + r"device" + _Q + r"\s*:\s*" + _Q + r")(\w{8})\w*(" + _Q + r")", r"\1\2...\3"),
        # 主机名 / 主板型号(设备指纹)
        (r"(" + _Q + r"deviceName" + _Q + r"\s*:\s*" + _Q + r")([^\"\\]{2})[^\"\\]*(" + _Q + r")",
         r"\1\2***\3"),
        (r"(" + _Q + r"deviceModel" + _Q + r"\s*:\s*" + _Q + r")([^\"\\]{4})[^\"\\]*(" + _Q + r")",
         r"\1\2***\3"),
        # 邮箱(如果有)
        (r"(" + _Q + r"\w*[Ee]mail\w*" + _Q + r"\s*:\s*" + _Q + r")([^\"\\@]{2})[^\"\\]*@",
         r"\1\2***@"),
    )
    for pat, rep in subs:
        try:
            text = re.sub(pat, rep, text)
        except Exception:
            pass
    return text


def serve_res(path):
    """GET /res/<rel> -> <程序目录>\\res\\<rel> 的原始字节。返回 (说明, 内容)。"""
    rel = unquote(path[len("/res/"):]).replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return "res(非法路径 %r)" % rel, b""
    full = os.path.join(RES_ROOT, rel.replace("/", os.sep))
    try:
        with open(full, "rb") as f:
            data = f.read()
        return "res(%s, %d 字节)" % (rel, len(data)), data
    except OSError:
        return "res(★缺文件: %s)" % rel, b""


# ============================================================================
# 关于 /res/ 更新通道 —— **已废弃，永远不要再启用**（2026-10-17 定案）
# ============================================================================
#
# 这里原来有个 `apply_res_update()`：把 `res_update.json` 里的文件清单塞进
# `baseapi/v1/update` 响应，让客户端从 `/res/` 把 Lua 补丁下载下来，
# 排在 HotUpdate 的 Decompress 之后落地 —— 听起来正好能绕开
# "Steam 版每次启动都从 bk.zip 解压覆盖 Win\lua" 的问题。
#
# **但这条通道从架构上就不可能工作。** 客户端拿到文件后走的是：
#
#     System.IO.File.Move(临时文件, 目标路径)
#
# Mono 的 `File.Move` **不覆盖已存在的目标**，直接抛
# `IOException: Win32 IO returned ERROR_ALREADY_EXISTS`。
# 而 `Win\lua` 里那些文件**本来就在**（刚才的 Decompress 刚铺过一遍）⇒ 必失败。
# 失败之后 HotUpdate 卡在「下载中 11.59/339.15KB」，整个客户端进不去。
#
# ★★ 而且它是**延迟引爆**的：只要清单里的 md5 和客户端本地文件一致，
#    客户端就跳过下载、看起来一切正常；等哪天 wrapper 的字节变了
#    （哪怕只是删掉一句日志），md5 对不上，立刻炸。
#    2026-10-17 光是删掉 `inject_cfgdump` 里的探针就把这个雷踩响了。
#
# ⇒ **Lua 补丁的唯一投递方式是改 `StreamingAssets\bk.zip` 的源包**
#    （见 openKleinsPatcher）。`baseapi/v1/update` 一律回空 `updateList`。
#
# 留着的死代码：`RES_ROOT` / `serve_res()` / `handle()` 里的 `/res/` 分支 ——
# 它们只在客户端主动来取文件时才动，不填 updateList 就永远走不到。
# ============================================================================


def load_routes():
    try:
        with open(ROUTES, "rb") as f:
            raw = f.read()
        # 允许 BOM / 注释
        return json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        log("!! routes.json 解析失败: %s" % e)
        return {}


def resolve(routes, method, path):
    key = "%s %s" % (method, path)
    if key in routes:
        return key, routes[key]
    for k, v in routes.items():
        if k.startswith("__"):
            continue
        if " " not in k and path.endswith(k):
            return k, v
        if " " not in k and k in path:
            return k, v
    return "__default__", routes.get("__default__", {"code": 0})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kleins-fake/0.1"

    def _handle(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        body = self.rfile.read(n) if n else b""
        text = body.decode("utf-8", "replace")

        # ★ 2026-10-17: 顺手刮一下客户端渠道(platType/subPlatType)写状态文件 ——
        #   业务服靠它决定读哪个渠道的配置表目录。见 _note_client_channel。
        _note_client_channel(self.path, text)

        with _lock:
            _seq[0] += 1
            sid = _seq[0]

        routes = load_routes()
        # ★ 公告正文页 / 活动切换页走 HTML, 其余一律走 {code,msg,data} 的 JSON 信封
        _path = unquote(self.path)
        if _path.startswith("/noticeweb"):
            rkey = "noticeweb(HTML, 来自 notice.json / turn_activity.json)"
            out = notice_html(_path).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif _path.startswith("/resetall") or _path.startswith("/resetroles"):
            # ★ 公告页「重置数据」的两个按钮: 只写标记, 业务服下一个请求兑现。
            _kind = "all" if _path.startswith("/resetall") else "roles"
            rkey = "reset-%s(写 %s)" % (
                _kind, RESET_ALL_FLAG if _kind == "all" else RESET_ROLES_FLAG)
            out = reset_result_html(_kind).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif _path.startswith("/roletool") or _path.startswith("/droprole"):
            # ★ 「角色工具」: 搜索 + 删除/获得 两种模式。
            #   带 id 就是**执行**(写 do_roletool.flag, 业务服下一个请求兑现),
            #   不带 id 就是那个可搜索的页面。
            #   `/droprole` 保留成别名(老链接/书签不至于失效), 默认删除模式。
            _qs = parse_qs(urlparse(_path).query)
            _mode = (_qs.get("mode") or ["drop"])[0]
            if _mode not in ("drop", "give"):
                _mode = "drop"
            _raw = (_qs.get("id") or [""])[0]
            _ids = []
            for _tok in _raw.replace(",", " ").split():
                try:
                    _ids.append(int(_tok))
                except ValueError:
                    continue
            if _ids:
                _ok = _write_tool_flag(_mode, _ids)
                rkey = "roletool[%s](写 %s: %s)" % (_mode, ROLE_TOOL_FLAG, _ids)
                out = roletool_result_html(_mode, _ids, _ok).encode("utf-8")
            else:
                _q = (_qs.get("q") or [""])[0]
                _filt = (_qs.get("filt") or [""])[0]
                rkey = "roletool[%s](页面, q=%r, filt=%r)" % (_mode, _q, _filt)
                out = roletool_html(_mode, _q, _filt).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif _path.startswith("/logout"):
            # ★ 「退出登录」: 只写 do_logout.flag, 业务服下一个请求进来时兑现
            #   (推 1009 + 断开连接)。见 logout_result_html 的说明。
            rkey = "logout(退出登录 -> 写 do_logout.flag, 等业务服兑现)"
            out = logout_result_html().encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif _path.startswith("/switchact"):
            # ★ 「活动切换」按钮打到这里: 改 turn_activity.json -> 业务服热读生效。
            try:
                to = int((parse_qs(urlparse(_path).query).get("to") or ["0"])[0])
            except Exception:
                to = 0
            if to > 0:
                _write_act(to)
                rkey = "switchact(切换回合活动线路 -> time_id=%d)" % to
                out = switch_result_html(to).encode("utf-8")
            else:
                rkey = "switchact(缺少 to 参数)"
                out = switch_html().encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif _path.startswith("/res/"):
            # ★ 资源更新下载口 —— 客户端读了 update 响应里的 updateList 之后,
            #   会按每个 part 的 ResName 到这里取文件。
            #   根目录是 <程序目录>\res\, 结构**镜像客户端的 Win\**
            #   (res\lua\logic\xxx.lua 对应客户端 Win\lua\logic\xxx.lua)。
            rkey, out = serve_res(_path)
            ctype = "application/octet-stream"
        else:
            rkey, payload = resolve(routes, self.command, self.path)
            out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"

        log("=" * 78)
        log("#%d  %s %s" % (sid, self.command, unquote(self.path)))
        log("    -> 路由: %s" % rkey)
        for k, v in self.headers.items():
            log("    %s: %s" % (k, _mask(v)))       # 头里也可能带 token
        # ★ body 里有手机号/身份证/密码/设备指纹 —— 打码后再落盘。
        #   KLEINS_HTTP_LOG_BODY=0 则完全不记。
        if text and LOG_BODY:
            log("    BODY: %s" % _mask(text))
        elif text:
            log("    BODY: <已省略 (KLEINS_HTTP_LOG_BODY=0), %d 字节>" % len(text))
        # ★ 必须 errors="replace" —— /res/ 下发的是 **Lua 字节码**, 不是合法 UTF-8,
        #   直接 decode 会抛 UnicodeDecodeError, 把已经处理好的响应连连接一起丢掉。
        log("    <== 响应 %d bytes: %s"
            % (len(out), _mask(out.decode("utf-8", "replace")[:600])))

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_HEAD = _handle

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")     # 控制台编码兜底, 避免中文打印炸掉
    except Exception:
        pass
    ports = [int(x) for x in sys.argv[1:]] or [10300]
    # ★ 2026-10-17: 数据目录迁移(程序目录 -> 用户数据目录)。幂等, 重复启动无副作用。
    #   ★ 这里必须先于 log() 之外的任何读写; 业务服那边也会跑一遍, 谁先起来谁搬。
    try:
        paths.migrate_legacy(log)
    except Exception as e:
        log("!! 数据目录迁移失败(不影响启动): %r" % (e,))
    log("### fake server 启动, 端口: %s ###" % ports)
    log("    %s" % paths.summary())
    for p in ports[:-1]:
        threading.Thread(target=lambda pp=p: ThreadingHTTPServer(("127.0.0.1", pp), Handler).serve_forever(),
                         daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", ports[-1]), Handler).serve_forever()
