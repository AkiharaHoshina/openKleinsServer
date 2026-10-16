#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「打过补丁的 Lua」做成假服的**远程更新下发**内容（产 `res_update.json` + `res\`）。

    python tools\\make_res_update.py --patched <已打补丁的客户端根> --pristine <干净客户端根>
    python tools\\make_res_update.py --patched ... --pristine ... --dry-run
    python tools\\make_res_update.py --clear          # 撤下更新（回到空表）

## 为什么需要这条通道

有的客户端版本（实测 **Steam 版**）每次启动都会执行 HotUpdate 的 Decompress 步骤，
把 `环行旅舍_Data\\StreamingAssets\\bk.zip`（Lua 源包）**整包解开覆盖 `Win\\lua`**
（实测每次 672 个文件，一个不少）。打进 `Win\\lua` 的 Lua 补丁活不过一次启动。

但 HotUpdate 的任务顺序是：

    CheckEnvironment -> CheckVersion -> **Decompress（覆盖）** -> **Download（最后落地）**

而 Download 的地址和清单都来自本服的 `baseapi/v1/update`（`routes.json`，热读）。
⇒ 在这里下发一份更新清单，客户端就会**在解压之后**把我们的补丁下载回来覆盖。
这是游戏自带的通道，**不用改客户端一个字节**。

两者都**不进 git**（里面含游戏代码）。`fakeserver.py` 只提供能力，数据由本脚本按部署生成。

## 下发的两类文件

1. `<module>.lua`       —— 我们的 wrapper
2. `<module>_orig.lua`  —— 原版副本。wrapper 靠 `require("..._orig")` 拿原模块，
                           而 `bk.zip` 里没有它 ⇒ **必须一起下发**，否则 wrapper 加载即失败。
   ★ 这份取自**你自己那份干净客户端**，是服务端本地喂给本机客户端的。

## ★★★ 字段名（2026-10-16 反汇编定案，别猜）

`HotUpdate.DownloadUserData.ResInfo$$.ctor` 的签名就是 **`ResInfo(JsonData updateIt)`**
—— 它拿整条 JSON 自己解析。反汇编 `GameAssembly.dll`（Steam 版 RVA **0x5B4090**）得到它读的键：

```
ResInfo      : { ver, updateFlag, fileList[ { fileName, md5, size } ] }
```

| 想当然的写法 | ✗ | 实际必须写 | 备注 |
|---|---|---|---|
| `parts`   | ✗ | **`fileList`** | |
| `Version` | ✗ | **`ver`**      | |
| `UpdateFlag`（大写） | ✗ | **`updateFlag`** | 这个大小写恰好对了 |
| `resName` / `ResName` / `name` | ✗ | **`fileName`** | |
| `md5` | ✓ | `md5` | |
| `size` | ✓ | `size` | |

客户端用 **LitJson**，它的 `JsonData` 下标器在**键不存在时直接抛 `KeyNotFoundException`**
（不是返回 nil）。⇒ 上面这 6 个键**一个都不能少**，写法也一个都不能错。
之前用别名混写的版本（`parts`/`Version`/`resName`）会让客户端卡在「检查中」，
错误上报里只有一行 `KeyNotFoundException ... DownloadCondition.<Execute>d__0.MoveNext`。

★ `ver` 做成**内容派生**（对全部文件 md5 再哈希）—— 否则改了补丁内容但版本号不变时，
客户端可能按版本号判定"已是最新"而跳过下载。

多余的键**无害**（LitJson 只查它要的），所以不必也不该再写别名糊弄。

## ★★★ `fileName` 必须带 `Win/` 前缀（2026-10-16 第二次踩）

`HotUpdate.DownloadTask$$PathCombine(url1, url2)` 就是
`String.Format("{0}/{1}", url1.TrimEnd('/'), url2.TrimStart('/'))` —— 纯前缀拼接。
客户端的落盘路径 = **`localUrl` + "/" + fileName**，而实测 `localUrl` 是
**客户端根目录（exe 所在处）**，不是 `Win\\`：

    写 `lua/logic/common/uimgr.lua`     → F:\\KleinsTest1\\lua\\logic\\common\\uimgr.lua.download   ✗
    写 `Win/lua/logic/common/uimgr.lua` → F:\\KleinsTest1\\Win\\lua\\logic\\common\\uimgr.lua.download ✓

写错时的症状：弹「有 300 多 k 更新」→ 确认后仍卡在「检查中」，上报

    DirectoryNotFoundException
    Could not find a part of the path "...\\lua\\logic\\common\\config_data_center.lua.download"
    at HotUpdate.DownloadTask+<Execute>d__6.MoveNext ()

★ 客户端**不会**为下载目标创建缺失目录 ⇒ 前缀必须落在真实存在的目录上。
★ 下载是先写 `<目标>.download` 临时文件再改名，所以中途失败会留下 `.download` 残骸。

## 产出

1. `<server>\\res\\Win\\lua\\...` —— 实际下载的文件，结构**镜像客户端的 `Win\\`**
2. `<server>\\res_update.json` —— 更新清单（`fakeserver.apply_res_update` 热读）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

sys.stdout.reconfigure(errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES_DIR = os.path.join(ROOT, "res")
RES_UPDATE = os.path.join(ROOT, "res_update.json")

# ★★★ 清单里的文件名前缀 —— 必须是 `Win/lua/`，理由见模块 docstring。
#   这是**唯一**一处定义，铺文件和造清单都用它（分两处写就会写歪）。
REL_PREFIX = "Win/lua/"

# ★ 诊断探针，不是产品补丁，不下发（它们只在开发时用，且每次都写日志）
EXCLUDE = {
    "logic/home_ui/view_home_ui.lua",             # inject_homeprobe
    "logic/shop_ui/view_shopping_center_ui.lua",  # inject_shopclick
}


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def clear():
    n = 0
    for p in (RES_UPDATE,):
        if os.path.exists(p):
            os.remove(p)
            n += 1
    if os.path.isdir(RES_DIR):
        shutil.rmtree(RES_DIR)
        n += 1
    print("已撤下更新（删了 %d 项）" % n)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patched")
    ap.add_argument("--pristine")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--when", action="append", default=None, metavar="KEY=SUBSTR",
                    help="分流条件（可重复）。只有 update 请求体里 KEY 的值包含 SUBSTR 才下发。"
                         "默认 subPlatType=steam")
    ap.add_argument("--all-clients", action="store_true",
                    help="不设分流条件，对所有客户端都下发")
    ap.add_argument("--allow-washed", action="store_true",
                    help="★ 别用。允许在「Lua 补丁已被整包冲掉」的客户端上继续生成空补丁")
    a = ap.parse_args()

    if a.clear:
        return clear()
    if not a.patched or not a.pristine:
        print(__doc__)
        return 2

    lua_p = os.path.join(os.path.abspath(a.patched), "Win", "lua")
    lua_q = os.path.join(os.path.abspath(a.pristine), "Win", "lua")
    if not os.path.isdir(lua_p) or not os.path.isdir(lua_q):
        print("客户端目录不对: %s / %s" % (lua_p, lua_q))
        return 2

    # 1) 收集「已打补丁」的模块（有 _orig.lua 兄弟的那些）
    targets, washed = [], []
    for dp, dn, fn in os.walk(lua_p):
        for f in fn:
            if not f.endswith("_orig.lua"):
                continue
            mod = f[:-len("_orig.lua")] + ".lua"
            if not os.path.exists(os.path.join(dp, mod)):
                continue
            rel = os.path.relpath(os.path.join(dp, mod), lua_p).replace("\\", "/")
            if rel in EXCLUDE:
                continue
            # ★★ 空补丁闸：Steam 版每次启动都会用 bk.zip 整包洗 Win\lua，
            #    洗过之后 `xxx.lua` 会和它的 `xxx_orig.lua` 一模一样。
            #    这时生成出来的"补丁"就是原版本身 —— 下发下去等于什么都没做。
            #    （2026-10-16 就这么白跑了一轮。）
            # ★ 局部变量千万别叫 a / b —— `a` 是 argparse 的 Namespace，覆盖了就炸
            f_plain = os.path.join(dp, mod)
            f_back = os.path.join(dp, f)
            if md5_of(f_plain) == md5_of(f_back):
                washed.append(rel)
            else:
                targets.append(rel)
    targets.sort()
    print("发现 %d 个已打补丁的模块" % len(targets))
    if washed:
        print("")
        print("!! ★★ 有 %d 个模块的 `xxx.lua` 和 `xxx_orig.lua` **一模一样** —— "
              "Lua 补丁已被整包解压冲掉:" % len(washed))
        for r in washed:
            print("     %s" % r)
        print("   ⇒ 这样生成的清单是**空补丁**，下发下去等于什么都没做。")
        print("   ⇒ 正确做法：")
        print("        robocopy <真原版>\\Win\\lua %s\\Win\\lua /MIR" % lua_p)
        print("        python D:\\openKleinsPatcher\\devtools\\multiver\\build_test_build.py "
              "%s --workdir <全新的空目录> --skip-binary" % os.path.abspath(a.patched))
        print("      再重新跑本脚本。（--allow-washed 可强行继续，但不该用）")
        if not a.allow_washed:
            return 1

    # 2) 铺文件 + 造清单
    parts, miss = [], []
    for rel in targets:
        src_w = os.path.join(lua_p, rel.replace("/", os.sep))
        src_o = os.path.join(lua_q, rel.replace("/", os.sep))
        if not os.path.exists(src_o):
            miss.append(rel)
            continue
        # ★★ 「铺文件」和「造清单」必须用**同一份**文件名列表。
        #    之前这两处各写了一遍，只改了其中一个 ⇒ res\ 里铺到 res\lua\...、
        #    清单却写 Win/lua/...，客户端一去取就 404（自检 #3 会立刻抓到）。
        for name, src in ((REL_PREFIX + rel, src_w),
                          (REL_PREFIX + rel[:-4] + "_orig.lua", src_o)):
            if not a.dry_run:
                dst = os.path.join(RES_DIR, name.replace("/", os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
            size = os.path.getsize(src)
            digest = md5_of(src)
            # ★ 键名见模块 docstring —— ResInfo.fileList[i] = {fileName, md5, size}
            parts.append({"fileName": name, "md5": digest, "size": size})
    if miss:
        print("★ 干净端缺这些原版文件（已跳过）: %s" % miss)
    print("下发 %d 个文件" % len(parts))

    # ★ ver = 内容派生（把每个文件的 md5 拼起来再哈希）。内容一变 ver 就变，
    #   免得客户端按版本号判定「已是最新」而跳过下载。
    h = hashlib.md5()
    for p in parts:
        h.update(p["fileName"].encode("utf-8"))
        h.update(p["md5"].encode("ascii"))
    ver = "kleinslocal-" + h.hexdigest()[:8]

    entry = {"ver": ver, "updateFlag": 1, "fileList": parts}
    data = {"updateType": "res", "updateList": [entry],
            "resUrl": "http://127.0.0.1:10300/res/", "bin": ""}

    # ★ 分流：默认只发给 Steam 版（只有它会整包解压覆盖 Win\lua）。见 fakeserver.apply_res_update。
    if not a.all_clients:
        specs = a.when if a.when is not None else ["subPlatType=steam"]
        when = {}
        for s in specs:
            if "=" not in s:
                print("!! --when 需要 KEY=SUBSTR 形式: %r" % s)
                return 2
            k, v = s.split("=", 1)
            when[k.strip()] = v.strip()
        data["when"] = when
        print("when = %s" % json.dumps(when, ensure_ascii=False))

    print("ver = %s" % ver)

    if a.dry_run:
        print("(dry-run) 未写盘")
        print("  样例 fileList[0]: %s" % json.dumps(parts[0], ensure_ascii=False)[:260])
        return 0

    with open(RES_UPDATE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("已写出 %s (%d 字节)" % (RES_UPDATE, os.path.getsize(RES_UPDATE)))
    print("★ 服务端**热读**这个文件，改完不用重启")
    return 0


if __name__ == "__main__":
    sys.exit(main())
