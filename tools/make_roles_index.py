#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 `roles_index.json`（角色 id → 名字），给 fakeserver 的「角色工具」页用。

    python tools\\make_roles_index.py

★ 为什么放在 tools/ 而不是运行时做：
  `dataconfig_role_config.txt` 有 6 MB 多，HTTP 层每个请求都去解析它太慢。
  这里压成一份几 KB 的 JSON，fakeserver 只读产物（和 routes.json 一个套路）。

★ 为什么这份 JSON **不进 git**：
  里面的角色名是《环行旅舍》的游戏文案，属于游戏本体数据，不是本项目的资产。
  它完全可以从你自己的客户端 dump 出来的配置表里重建 —— 就是这个脚本干的事。
  没生成也不影响服务端启动：角色工具页会退化成只显示角色 id。
"""
import json
import os
import sys

sys.stdout.reconfigure(errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cfgdata  # noqa: E402
import paths  # noqa: E402  (运行期数据在用户数据目录, 见 paths.py)


def build():
    """{角色id(str): 名字}。cfg 读不到就返回 {}。"""
    langs = cfgdata.load("dataconfig_language")
    lang = {}
    for r in langs.values():
        if not isinstance(r, dict):
            continue
        i, t = r.get("id"), r.get("text")
        if isinstance(i, int) and isinstance(t, str):
            lang[str(i)] = t

    out = {}
    for r in cfgdata.load("dataconfig_role_config").values():
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not isinstance(rid, int) or rid <= 0:
            continue
        nm = r.get("name")
        out[str(rid)] = lang.get(str(nm), "") if nm is not None else ""
    return out


def main():
    ok, rep = cfgdata.cfg_report()
    if not ok:
        print(rep)
        print()
        print("!! 没找到配置表，先生成配置表再跑本脚本。")
        return 1

    idx = build()
    if not idx:
        print("!! 从配置表里没解析出任何角色，检查 cfg\\dataconfig_role_config.txt")
        return 1

    out = paths.data_path("KLEINS_ROLES_INDEX", "resources", "roles_index.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=0, sort_keys=True)
    print("已写出 %s: %d 个角色, %d 字节" % (out, len(idx), os.path.getsize(out)))
    for rid in ("1001", "1055"):
        print("   %s -> %s" % (rid, idx.get(rid, "<无>")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
