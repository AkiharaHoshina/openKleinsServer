#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「存档按客户端(渠道+版本)分开」的自检。

    python tools\\_selftest_savescope.py        (工作目录随意)

背景(用户 2026-10-17 要求):
  配置表早就按渠道分了 (`resources\\cfg\\<SubPlatType>\\`), 但**存档只有一份**。
  换客户端测试时会串档 —— 在国服客户端上领到的道具 id, Steam 客户端可能没有,
  正是 5133 / 头像 64,65 / 卡池 35..40 那一类崩。
  ⇒ `saves\\<渠道><版本>\\player.json`, 例如 `steam-GL1.5.0.0`。

验:
  * `paths.client_scope()` 能从 state\\client_channel.json 拼出身份
  * 认不出 / 坏文件 / 缺字段 ⇒ **None**(退回平面目录), 且绝不抛
  * 名字里的非法字符被洗掉(不能拼出带 `\\ / :` 的目录名)
  * `paths.saves_dir()` 有身份进子目录、没身份回平面
  * `paths.adopt_flat_save()` 把老的单份存档收编进作用域, **幂等**, 不覆盖已有的
  * ★★★ `playerdata.refresh_save()`: 换客户端 ⇒ 存档路径跟着换, **两份互不影响**
  * `KLEINS_PLAYER_JSON` 设了就固定不动(自检隔离用)
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

OK, BAD = [], []


def check(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  %s %s %s" % ("[OK] " if cond else "[!!] ", name, extra))


ROOT = tempfile.mkdtemp(prefix="_kleins_savescope_")
os.environ["KLEINS_DATA_ROOT"] = ROOT
os.environ.pop("KLEINS_PLAYER_JSON", None)

import paths                      # noqa: E402
import playerdata                 # noqa: E402

STATE = os.path.join(ROOT, "state")
CHAN = os.path.join(STATE, "client_channel.json")
SAVES = os.path.join(ROOT, "saves")


def set_chan(d):
    os.makedirs(STATE, exist_ok=True)
    if d is None:
        if os.path.exists(CHAN):
            os.remove(CHAN)
        return
    with open(CHAN, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def set_chan_raw(s):
    os.makedirs(STATE, exist_ok=True)
    with open(CHAN, "w", encoding="utf-8") as f:
        f.write(s)


def reseed():
    """把模块状态和目录清干净, 让每个场景从零开始。"""
    set_chan(None)
    playerdata._save_scope = None
    playerdata.SAVE = playerdata.paths.data_path(
        "KLEINS_PLAYER_JSON", "saves", "player.json")
    if os.path.isdir(SAVES):
        shutil.rmtree(SAVES, ignore_errors=True)
    paths._made.clear()          # ★ 别留"建过"的缓存 —— 目录已经没了


print("=== 1. 认不出客户端 ⇒ 退回平面目录(改动前的行为) ===")
reseed()
check("没有 client_channel.json 时 client_scope() 是 None",
      paths.client_scope() is None, str(paths.client_scope()))
check("saves_dir() 回平面目录", paths.saves_dir() == SAVES, paths.saves_dir())
_sp, _new = playerdata.refresh_save()
check("refresh_save() 保持平面路径", playerdata.SAVE == os.path.join(SAVES, "player.json"),
      playerdata.SAVE)
check("而且报告『没变』", _new is False)

print()
print("=== 2. ★ 拼出身份: 渠道 + 版本 ===")
reseed()
set_chan({"subPlatType": "steam-GL", "platType": "steam",
          "svn": "82743", "resVer": "1.5.0.0"})
sc = paths.client_scope()
check("★ client_scope() == 'steam-GL1.5.0.0'", sc == "steam-GL1.5.0.0", repr(sc))
check("★ saves_dir() 进子目录",
      paths.saves_dir() == os.path.join(SAVES, "steam-GL1.5.0.0"), paths.saves_dir())

print()
print("=== 3. ★★★ 字段不全 ⇒ 宁可不分, 也不分一半 ===")
# ★★ 这条是**真事故**换来的(2026-10-17): 第一次上这套时, state/client_channel.json
#   是上一版留下的(那版还没刮 resVer) ⇒ 作用域先算成「只有渠道」`steam-GL`,
#   老存档被收编进去; 等第一个请求带上 resVer, 作用域变成 `steam-GL1.5.0.0`
#   ⇒ **老存档当场被孤立**, 客户端看起来像"存档重置了"。
#   ⇒ 缺任何一个字段都必须返回 None(留在平面目录等下一次结果)。
for tag, chan in (("只有渠道、没有 resVer", {"subPlatType": "steam-GL"}),
                  ("只有 resVer、没有渠道", {"resVer": "1.5.0.0"}),
                  ("只有 platType、没有版本", {"platType": "bingkolo"}),
                  ("渠道是空串", {"subPlatType": "", "resVer": "1.5.0.0"}),
                  ("版本是空串", {"subPlatType": "steam-GL", "resVer": ""})):
    reseed()
    set_chan(chan)
    check("  %-22s ⇒ None" % tag, paths.client_scope() is None,
          repr(paths.client_scope()))

print()
print("=== 4. ★ 名字里的非法字符必须洗掉 ===")
reseed()
set_chan({"subPlatType": 'steam/GL:*?"<>|', "resVer": "1.5.0.0"})
sc = paths.client_scope() or ""
bad = [c for c in '\\/:*?"<>|' if c in sc]
check("★ 结果里不含任何路径保留字符", not bad, "sc=%r 含 %r" % (sc, bad))
check("  而且是非空的", bool(sc), repr(sc))
check("  saves_dir() 里的目录名就是它", os.path.basename(paths.saves_dir()) == sc,
      os.path.basename(paths.saves_dir()))

print()
print("=== 5. 坏文件也不崩 ===")
for tag, raw in (("坏 JSON", "{不是 json"), ("不是对象", "[1,2,3]"), ("空文件", "")):
    reseed()
    set_chan_raw(raw)
    try:
        v = paths.client_scope()
        ok = v is None
        err = None
    except Exception as e:
        ok, err = False, repr(e)
    check("  %-8s ⇒ None, 不抛" % tag, ok, err or repr(v))

print()
print("=== 6. ★★ 老的单份存档被收编进作用域(幂等) ===")
reseed()
os.makedirs(SAVES, exist_ok=True)
with open(os.path.join(SAVES, "player.json"), "w", encoding="utf-8") as f:
    f.write('{"marker":"老存档"}')
with open(os.path.join(SAVES, "player.json.bak_20261017"), "w", encoding="utf-8") as f:
    f.write("bak")
set_chan({"subPlatType": "steam-GL", "resVer": "1.5.0.0"})
sp, new = playerdata.refresh_save()
sub = os.path.join(SAVES, "steam-GL1.5.0.0")
check("★ refresh_save() 把存档重定到作用域目录",
      playerdata.SAVE == os.path.join(sub, "player.json"), playerdata.SAVE)
check("★ 老存档已经搬进去了", os.path.exists(os.path.join(sub, "player.json")))
check("★ 备份也一起搬了", os.path.exists(os.path.join(sub, "player.json.bak_20261017")))
check("★ 平面目录里已经没有 player.json 了(否则下个客户端还会读到)",
      not os.path.exists(os.path.join(SAVES, "player.json")))
# 幂等: 再调一次不动
before = os.path.getmtime(os.path.join(sub, "player.json"))
n_moved = paths.adopt_flat_save(sub)
check("★ 再调 adopt_flat_save() 什么都不搬", n_moved == [], str(n_moved))
check("★ 文件没被动过", os.path.getmtime(os.path.join(sub, "player.json")) == before)

print()
print("=== 7. ★★★ 换客户端 ⇒ 两份存档互不影响 ===")
reseed()
set_chan({"subPlatType": "steam-GL", "resVer": "1.5.0.0"})
playerdata.refresh_save()
steam_save = playerdata.SAVE
with open(steam_save, "w", encoding="utf-8") as f:
    f.write('{"who":"steam"}')

set_chan({"subPlatType": "bingkolo-Win", "resVer": "1.5.0.0"})
_sp2, changed2 = playerdata.refresh_save()
bing_save = playerdata.SAVE
check("★ 换成国服 ⇒ 路径变了", bing_save != steam_save,
      "%s -> %s" % (steam_save, bing_save))
check("★ 而且报告『变了』(bizserver 会打日志)", changed2 is True)
check("★ 国服这份**还是空的**(不会读到 Steam 的档)", not os.path.exists(bing_save))
with open(bing_save, "w", encoding="utf-8") as f:
    f.write('{"who":"bingkolo"}')

set_chan({"subPlatType": "steam-GL", "resVer": "1.5.0.0"})
playerdata.refresh_save()
check("★ 换回 Steam ⇒ 路径回到 steam 那份", playerdata.SAVE == steam_save,
      playerdata.SAVE)
check("★★ Steam 的档**原样还在**(没被国服那次覆盖)",
      open(steam_save, encoding="utf-8").read() == '{"who":"steam"}',
      open(steam_save, encoding="utf-8").read())
check("★★ 国服的档也还在",
      open(bing_save, encoding="utf-8").read() == '{"who":"bingkolo"}')
check("★ 两个目录名都对",
      os.path.basename(os.path.dirname(steam_save)) == "steam-GL1.5.0.0"
      and os.path.basename(os.path.dirname(bing_save)) == "bingkolo-Win1.5.0.0",
      "%s | %s" % (os.path.dirname(steam_save), os.path.dirname(bing_save)))

print()
print("=== 8. ★ 同一客户端重复调用 ⇒ 不重定、不刷盘 ===")
_sp3, changed3 = playerdata.refresh_save()
check("★ 没变就报 False", changed3 is False)
check("★ 路径也没动", playerdata.SAVE == steam_save)

print()
print("=== 9. KLEINS_PLAYER_JSON 优先级最高(自检隔离) ===")
pinned = os.path.join(ROOT, "pinned.json")
os.environ["KLEINS_PLAYER_JSON"] = pinned
playerdata._save_scope = "某个别的东西"
set_chan({"subPlatType": "bingkolo-Win", "resVer": "1.5.0.0"})
playerdata.refresh_save()
check("★ 环境变量覆盖时, 存档就固定在它上面", playerdata.SAVE == pinned,
      playerdata.SAVE)
check("★ 而且不看 client_channel.json(换了客户端也不动)", playerdata.SAVE == pinned)
os.environ.pop("KLEINS_PLAYER_JSON", None)

print()
print("=== 10. 版本号变了 ⇒ 换目录(同渠道不同版本也是两份) ===")
reseed()
set_chan({"subPlatType": "steam-GL", "resVer": "1.5.0.0"})
playerdata.refresh_save()
v1 = playerdata.SAVE
set_chan({"subPlatType": "steam-GL", "resVer": "1.6.0.0"})
_sp4, ch4 = playerdata.refresh_save()
v2 = playerdata.SAVE
check("★ 版本一变就换目录", ch4 is True and v1 != v2, "%s -> %s" % (v1, v2))
check("★ 目录名带上了新版本",
      os.path.basename(os.path.dirname(v2)) == "steam-GL1.6.0.0",
      os.path.dirname(v2))

print()
print("=== 11. ★★★ 字段补齐之前不动存档, 补齐了再一起搬(这就是那次真事故) ===")
reseed()
os.makedirs(SAVES, exist_ok=True)
with open(os.path.join(SAVES, "player.json"), "w", encoding="utf-8") as f:
    f.write('{"who":"原始存档"}')

# ① 只有渠道 —— 就是"上一版状态文件"的样子
set_chan({"subPlatType": "steam-GL"})
playerdata.refresh_save()
flat = os.path.join(SAVES, "player.json")
check("★ 字段不全 ⇒ 继续在平面目录用着那份老存档",
      playerdata.SAVE == flat, playerdata.SAVE)
check("★★ 而且**没有**造出 saves\\steam-GL\\ 这种半成品(就是它害的孤儿)",
      not os.path.isdir(os.path.join(SAVES, "steam-GL")))

# ② resVer 到了
set_chan({"subPlatType": "steam-GL", "resVer": "1.5.0.0"})
_sp, _ch = playerdata.refresh_save()
full = os.path.join(SAVES, "steam-GL1.5.0.0", "player.json")
check("★ 字段补齐了 ⇒ 作用域才生效", playerdata.SAVE == full, playerdata.SAVE)
check("★★ 老存档**跟着搬过来了**, 没被落在后面",
      os.path.exists(full) and open(full, encoding="utf-8").read() == '{"who":"原始存档"}',
      open(full, encoding="utf-8").read() if os.path.exists(full) else "(没有)")
check("★ 平面目录里已经没有它了", not os.path.exists(flat))
check("★ 全程没留下半成品目录",
      not os.path.isdir(os.path.join(SAVES, "steam-GL")))

print()
print("=== 12. ★ settings.json 也按客户端分(用户报的「舞台背景没分开」) ===")
reseed()
set_chan({"subPlatType": "steam-GL", "resVer": "1.5.0.0"})
p = paths.scoped_state_path("settings.json")
check("★ 落到 state\\<渠道><版本>\\ 下",
      os.path.basename(os.path.dirname(p)) == "steam-GL1.5.0.0", p)
check("★ 而且就在 state\\ 之下", os.path.dirname(os.path.dirname(p)) == STATE, p)
reseed()
p2 = paths.scoped_state_path("settings.json")
check("★ 认不出客户端 ⇒ 退回平面 state\\settings.json",
      p2 == os.path.join(STATE, "settings.json"), p2)

print()
print("=== 13. ★★★ 登录页用户名: 无数据⇒空(客户端显示「向导」), 有数据⇒用户名 ===")
reseed()
set_chan({"subPlatType": "steam-GL", "resVer": "1.5.0.0"})
paths.publish_local_name()
ln = paths.local_name_file()
check("★ 存档还没有 ⇒ 写空字符串(客户端会显示兜底名「向导」)",
      open(ln, encoding="utf-8").read() == "",
      repr(open(ln, encoding="utf-8").read()))

def _put_save(name):
    d = os.path.join(paths.saves_dir(), "player.json")
    os.makedirs(os.path.dirname(d), exist_ok=True)
    with open(d, "w", encoding="utf-8") as f:
        json.dump({"actor": {"name": name}}, f, ensure_ascii=False)

_put_save("Steam玩家")
paths.publish_local_name()
check("★★ 有存档 ⇒ 写它的 actor.name",
      open(ln, encoding="utf-8").read() == "Steam玩家",
      repr(open(ln, encoding="utf-8").read()))

# ★★ 这就是真事故: 换客户端之后, 登录页必须显示**这一个**客户端的名字
set_chan({"subPlatType": "bingkolo-Win", "resVer": "1.5.0.35"})
_put_save("国服玩家")
paths.publish_local_name()
check("★★ 换客户端 ⇒ 登录页名字跟着换(不再串成上一个)",
      open(ln, encoding="utf-8").read() == "国服玩家",
      repr(open(ln, encoding="utf-8").read()))
check("★ 而每个客户端自己的存档都还在",
      os.path.exists(os.path.join(SAVES, "steam-GL1.5.0.0", "player.json"))
      and os.path.exists(os.path.join(SAVES, "bingkolo-Win1.5.0.35", "player.json")))

print()
print("=" * 62)
print("通过 %d 项, 失败 %d 项" % (len(OK), len(BAD)))
for b in BAD:
    print("  [!!] " + b)
sys.exit(1 if BAD else 0)
