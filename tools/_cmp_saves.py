#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出「按客户端分开」的每一份存档, 一眼看清谁是谁。

    python tools\\_cmp_saves.py [数据根]

为什么要它: 存档现在是 `saves\\<渠道><版本>\\player.json` —— 多客户端测试时
很容易搞不清"我现在玩的是哪一份""那份老的跑哪去了"。
(2026-10-17 就出过一回: 作用域从"只有渠道"补成"渠道+版本"之后,
 老存档被孤立在 `saves\\steam-GL\\` 里, 客户端看起来像存档重置了。)

★ 只读。
"""
import io
import json
import os
import sys

sys.stdout.reconfigure(errors="replace")


def summarize(path):
    """挑几个有代表性的字段, 别把整份存档倒出来。"""
    try:
        with io.open(path, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception as e:
        return {"err": repr(e)}
    a = d.get("actor") or {}
    roles = d.get("roles") or {}
    items = d.get("items") or {}
    stages = d.get("stages") or {}
    cleared = [k for k, v in stages.items() if isinstance(v, int) and v > 0]
    return {
        "level": a.get("level"),
        "name": a.get("name"),
        "currency": (a.get("diamond"), a.get("gold"), a.get("copper")),
        "roles": len(roles),
        "items": len(items),
        "stages": len(stages),
        "cleared": len(cleared),
    }


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("USERPROFILE", ""), "Saved Games", "freeKleins")
    sd = os.path.join(root, "saves")
    if not os.path.isdir(sd):
        print("没有 %s" % sd)
        return 1
    print("数据根: %s" % root)
    print("=" * 78)
    ents = sorted(os.listdir(sd))
    rows = []
    for name in ents:
        p = os.path.join(sd, name)
        if os.path.isfile(p) and name.startswith("player.json"):
            rows.append(("(平面目录)", p))
        elif os.path.isdir(p):
            f = os.path.join(p, "player.json")
            if os.path.exists(f):
                rows.append((name, f))
    if not rows:
        print("  saves\\ 下没有找到任何 player.json")
    for name, f in rows:
        st = os.stat(f)
        s = summarize(f)
        import time
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        print("  %-22s %8d B  %s" % (name, st.st_size, when))
        if "err" in s:
            print("      !! 读不了: %s" % s["err"])
        else:
            print("      等级 %s  角色 %s 个  道具 %s 种  关卡 %s(通关 %s)  货币 %s"
                  % (s["level"], s["roles"], s["items"], s["stages"],
                     s["cleared"], s["currency"]))
        nb = len([x for x in os.listdir(os.path.dirname(f))
                  if x.startswith("player.json.bak")])
        if nb:
            print("      另有 %d 个 .bak_* 备份" % nb)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
