#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检 `/res/` 资源更新通道（res_update.json + fakeserver.apply_res_update）。

    python tools\\_selftest_resupdate.py

## 为什么要有它 —— 2026-10-16 踩的坑

第一版清单把键名写成了 `parts` / `Version` / `ResName`（"想当然"的名字）。
客户端用 **LitJson**，它的 `JsonData` 下标器在**键不存在时直接抛 `KeyNotFoundException`**
（不是返回 nil）⇒ 客户端卡在「检查中」，错误上报只有一行
`KeyNotFoundException ... DownloadCondition.<Execute>d__0.MoveNext`。

反汇编 `HotUpdate.DownloadUserData.ResInfo$$.ctor`（`ResInfo(JsonData updateIt)`）才知道真正的键是：

    updateList[i] = { ver, updateFlag, fileList[ { fileName, md5, size } ] }

**本自检第 2 节就是这条的回归护栏**：`parts` / `Version` / `ResName` 这些别名
一旦重新出现就报错。

## 怎么跑才安全

不启服务端、不碰任何游戏文件：直接把 `fakeserver.py` 当模块 import，
喂两种请求体（Steam 版 / 老客户端）调 `apply_res_update`，再把清单里的
每个文件跟 `res\\` 下的实际字节核对。
"""
import hashlib
import json
import os
import sys

sys.stdout.reconfigure(errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

OK = 0
BAD = 0


def ck(cond, msg):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK]   %s" % msg)
    else:
        BAD += 1
        print("  [FAIL] %s" % msg)
    return bool(cond)


# ── 两种客户端的 update 请求体（取自 probe.log 的真实抓包）───────────────
STEAM_BODY = json.dumps({
    "gameId": "1000", "binName": "com.bingkolo.kleins.cn", "binVer": "1.5.0",
    "platType": "bingkolo", "subPlatType": "steam-GL", "device": "x", "os": "Win",
    "resVer": "1.5.0.0", "proj": "kleins_master", "timestamp": 1, "sign": "x",
})
OLD_BODY = json.dumps({
    "gameId": "1000", "binName": "com.bingkolo.kleins.cn", "binVer": "1.5.0",
    "platType": "bingkolo", "subPlatType": "bingkolo-Win", "device": "x", "os": "Win",
    "resVer": "1.5.0.35", "proj": "kleins_master", "timestamp": 1, "sign": "x",
})

PLAIN = {"code": 0, "msg": "ok", "data": {"updateType": "res", "updateList": [],
                                          "resUrl": "http://127.0.0.1:10300/res/", "bin": ""}}


def main():
    print("=" * 70)
    print("1) 清单文件本身")
    fu = os.path.join(ROOT, "res_update.json")
    if not os.path.exists(fu):
        print("  [SKIP] %s 不存在 —— 先跑 tools\\make_res_update.py" % fu)
        print("\n跳过后续（没有清单可校验）。")
        return 0
    raw = json.load(open(fu, encoding="utf-8"))
    ck(isinstance(raw, dict), "res_update.json 是对象")
    ck(raw.get("updateType") == "res", "updateType == 'res'")
    ck(raw.get("resUrl") == "http://127.0.0.1:10300/res/", "resUrl 指向本服 /res/")
    ck(isinstance(raw.get("updateList"), list) and len(raw["updateList"]) >= 1,
       "updateList 非空 (%d 组)" % len(raw.get("updateList") or []))
    ck("bin" in raw, "顶层有 bin")

    import fakeserver                                    # noqa: E402

    print("=" * 70)
    print("2) ★ 键名回归护栏 —— ResInfo 只认 ver/updateFlag/fileList，"
          "fileList[i] 只认 fileName/md5/size")
    BAD_ALIAS = {"parts", "Parts", "Version", "version", "UpdateFlag",
                 "resName", "ResName", "name", "Md5", "MD5", "Size", "SIZE"}
    seen_bad = set()
    n_parts = 0
    for e in raw["updateList"]:
        seen_bad |= (set(e) & BAD_ALIAS)
        ck(set(e) == {"ver", "updateFlag", "fileList"},
           "ResInfo 字段恰好是 {ver,updateFlag,fileList}，实为 %s" % sorted(e))
        ck(isinstance(e.get("ver"), str) and e["ver"], "ver 是非空字符串")
        ck(isinstance(e.get("updateFlag"), int), "updateFlag 是整数")
        ck(isinstance(e.get("fileList"), list) and e["fileList"], "fileList 非空")
        for p in e["fileList"]:
            n_parts += 1
            seen_bad |= (set(p) & BAD_ALIAS)
            if set(p) != {"fileName", "md5", "size"}:
                ck(False, "fileList 项字段应为 {fileName,md5,size}，实为 %s" % sorted(p))
                break
    ck(not seen_bad, "没有出现任何旧别名键 %s" % (sorted(seen_bad) or "（无）"))
    ck(n_parts and n_parts % 2 == 0, "fileList 文件数 %d 是偶数（每个模块 wrapper+_orig）" % n_parts)
    print("    共 %d 个文件" % n_parts)

    print("=" * 70)
    print("2b) ★ fileName 必须以 `Win/` 开头")
    print("    客户端落盘路径 = localUrl + '/' + fileName，而 localUrl 是**客户端根目录**")
    print("    （实测 F:\\KleinsTest1），且它**不会自动建目录** —— 少了 Win/ 就是")
    print("    DirectoryNotFoundException ...config_data_center.lua.download")
    bad_prefix = [p["fileName"] for e in raw["updateList"] for p in e["fileList"]
                  if not p["fileName"].startswith("Win/")]
    ck(not bad_prefix, "全部 %d 个 fileName 都带 Win/ 前缀%s"
       % (n_parts, "" if not bad_prefix else " —— 违规样例: %s" % bad_prefix[:3]))

    print("=" * 70)
    print("3) 清单里的 md5/size 与 res\\ 下的实际字节一致（客户端靠它决定要不要下）")
    res_root = os.path.join(ROOT, "res")
    n_ok = n_miss = n_diff = 0
    for e in raw["updateList"]:
        for p in e["fileList"]:
            fp = os.path.join(res_root, p["fileName"].replace("/", os.sep))
            if not os.path.exists(fp):
                n_miss += 1
                if n_miss <= 3:
                    print("    缺文件: %s" % p["fileName"])
                continue
            data = open(fp, "rb").read()
            if hashlib.md5(data).hexdigest() == p["md5"] and len(data) == p["size"]:
                n_ok += 1
            else:
                n_diff += 1
                print("    ★不一致: %s (清单 %s/%d, 实际 %s/%d)"
                      % (p["fileName"], p["md5"][:8], p["size"],
                         hashlib.md5(data).hexdigest()[:8], len(data)))
    ck(n_ok == n_parts, "全部 %d 个文件都对上（缺 %d, 不符 %d）" % (n_ok, n_miss, n_diff))

    print("=" * 70)
    print("4) 分流：只有 Steam 版拿得到清单，老客户端原样返回（它没进过那个循环体）")
    steam = fakeserver.apply_res_update(json.loads(json.dumps(PLAIN)), STEAM_BODY)
    old = fakeserver.apply_res_update(json.loads(json.dumps(PLAIN)), OLD_BODY)
    ck(len(steam["data"]["updateList"]) >= 1, "Steam 版 → 拿到 updateList")
    ck(old["data"]["updateList"] == [], "老客户端 → updateList 仍为空")
    ck("when" not in steam["data"], "when 不会下发给客户端")
    ck(steam["data"]["resUrl"] == PLAIN["data"]["resUrl"], "旁路字段不变")

    print("=" * 70)
    print("5) 没有 res_update.json 时保持原样（降级不炸）")
    saved = fakeserver.RES_UPDATE
    try:
        fakeserver.RES_UPDATE = os.path.join(ROOT, "_no_such_file.json")
        same = fakeserver.apply_res_update(json.loads(json.dumps(PLAIN)), STEAM_BODY)
        ck(same == PLAIN, "文件缺失 → 原样返回")
    finally:
        fakeserver.RES_UPDATE = saved

    print("=" * 70)
    print("通过 %d / 失败 %d" % (OK, BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
