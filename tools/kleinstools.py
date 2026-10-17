#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地服务端的命令行小工具 —— 打包成 `kleinstools.exe` 发给用户，免得他装 Python。

    kleinstools.exe roles        重建角色名索引 -> <用户数据>\\resources\\roles_index.json
    kleinstools.exe roles -f     强制重建（默认：文件已经在就跳过）
    kleinstools.exe cfg          配置表体检：渠道目录在哪、缺哪几张表
    kleinstools.exe save         打印当前存档摘要（名字 / 等级 / 角色数 / 道具种数）
    kleinstools.exe              不带参数 = 打印本帮助

★ 这几个功能服务端运行时都会**自动**做（bizserver 启动就生成角色名索引，
  fakeserver 打开「角色工具」页发现缺文件也会现场补），这个 exe 是给
  "想手动看一眼 / 手动重建一次 / 排查问题" 的情况准备的。

★ 冻结成 exe 时 `cfgdata` / `paths` 会被 PyInstaller 打进包里
  （见 kleinstools.spec），所以它**不依赖**同目录的 .py。
"""
import json
import os
import sys

# ★ 冻结成 exe 后 stdio 会退回系统 ANSI 代码页(cp936) —— 和补丁器那次是同一个坑
#   (PyInstaller 打出来的程序不认 PYTHONIOENCODING)。真终端保持控制台编码(中文
#   才不乱); 管道/重定向强制 utf-8(给机器读)。
for _s in (sys.stdout, sys.stderr):
    try:
        if _s.isatty():
            _s.reconfigure(errors="replace")
        else:
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 源码方式运行时，把仓库根加进搜索路径；冻结后这两个模块已经在包里了
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cfgdata                                                  # noqa: E402
import paths                                                    # noqa: E402


def cmd_roles(argv):
    force = any(x in ("-f", "--force") for x in argv)
    wrote, n = cfgdata.ensure_roles_index(force=force, log=print)
    out = paths.data_path("KLEINS_ROLES_INDEX", "resources",
                          "roles_index.json")
    if not n:
        print("！配置表还没有 —— 先启动服务端、进一次游戏，让客户端把表 dump 出来。")
        return 1
    print("%s\n  -> %s（%d 个角色）" % (out, "已重建" if wrote else "已存在，未改动", n))
    return 0


def cmd_cfg(argv):
    ok, rep = cfgdata.cfg_report()
    print(rep)
    return 0 if ok else 1


def cmd_save(argv):
    root = paths.data_dir("saves")
    # ★ 存档是**按「渠道 + 版本」分作用域**的（saves\<scope>\player.json）——
    #   以前这里写死读 saves\player.json，作用域一上就永远"还没有存档"。
    cands = []
    for dp, _dn, fn in os.walk(root):
        if "player.json" in fn:
            cands.append(os.path.join(dp, "player.json"))
    print("存档目录: %s" % root)
    if not cands:
        print("  （还没有存档 —— 进一次游戏就有了）")
        return 1
    p = max(cands, key=os.path.getmtime)          # 最新的那份
    print("  当前作用域: %s" % os.path.basename(os.path.dirname(p)))
    with open(p, "r", encoding="utf-8") as f:
        d = json.load(f)
    a = d.get("actor", {}) or {}
    print("  名字   : %s" % a.get("name"))
    print("  等级   : %s" % a.get("level"))
    print("  角色数 : %d" % len(d.get("roles", {}) or {}))
    print("  道具种 : %d" % len(d.get("items", {}) or {}))
    print("  已通关 : %d 关" % len(d.get("stages", {}) or {}))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help", "/?"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    table = {"roles": cmd_roles, "cfg": cmd_cfg, "save": cmd_save}
    fn = table.get(cmd)
    if fn is None:
        print("不认识的命令: %s\n" % cmd)
        print(__doc__)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
