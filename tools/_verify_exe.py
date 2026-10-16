#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认**打好的 exe 里真的有最新的代码**（不是只看时间戳）。

    python _verify_exe.py                    # 检查 bizserver.exe
    python _verify_exe.py fakeserver.exe fakeserver

为什么需要: PyInstaller 把模块体 **zlib 压缩**塞进 PYZ，直接拿字节在 exe 里搜字符串
永远搜不到（踩过 —— 一度以为没打包进去）。这里正经解 PYZ。

★ PYZ 格式（读 `PyInstaller/archive/writers.py` 得到的，别猜）:
    ZlibArchiveWriter.MAGIC = b'PYZ\\0'
    HDRLEN = ArchiveWriter.HDRLEN(12) + 5 = **17**
    TOCPOS = 8      -- 头里这个位置存的是 **TOC 的文件偏移(uint32 大端)**
    头之后紧跟第一条目的 zlib 数据；TOC 在**文件末尾**，
    内容是 `marshal` 的 **list of (name, (typecode, pos, length))**，
    其中 `pos` **就是绝对文件偏移**（lib.tell() 从头 17 字节之后算起）。
"""
import marshal
import os
import struct
import sys
import zlib

sys.stdout.reconfigure(errors="replace")
# 仓库根 = tools/ 的上一级
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXE_NAME = sys.argv[1] if len(sys.argv) > 1 else "bizserver.exe"
BUILD = sys.argv[2] if len(sys.argv) > 2 else EXE_NAME.replace(".exe", "")
# 构建布局（与 tools/build_release.ps1 一致）:
#   workpath = <根>\_build\<bizserver|fakeserver>\   (PYZ 在这里)
#   distpath = <根>\release\                          (exe 在这里)
PYZ = os.path.join(HERE, "_build", BUILD, "PYZ-00.pyz")
EXE = os.path.join(HERE, "release", EXE_NAME)

WANT = {
    "bizserver": {
        "playerdata": [
            "SWORD_CHAPTERS", "SWORD_TYPE_TO", "SWORD_MINOR_CHAPTERS",
            "stage_alias_type", "stage_alias_minor",
            "AWARD_TYPE_ACTIVITY_FINISH_ALL", "ACTIVITY_FINISH_ALL_TAG",
            # ★ 2026-10-16: 小窝(活动任务界面)崩溃的修复 —— 1111 里 type=8 必须发 tag=2。
            #   这两个名字只在那一版出现过, 是"装的是新包"的硬标记。
            "ACTIVITY_TAB_TAG",
            "PROLOGUE_STAGE", "LT_TYPE_LIMIT_TASK",
        ],
        # ★ 搬迁改造的标记（0.9.1）—— 有它们才说明装的是「去硬编码」那一版
        "paths": ["base_dir", "env_path"],
        "cfgdata": ["cfg_report", "_find_cfg_dir", "KLEINS_CFG_DIR"],
        # ★ 入口脚本(bizserver.py) **不在 PYZ 里** —— 它是 CArchive 的一条压缩条目。
        #   想验它得再解 PKG-00.pkg，这里先不折腾。
    },
    # fakeserver 的 PYZ 里没有我们关心的模块（入口脚本也在 CArchive 里）⇒ 跳过
    "fakeserver": {},
}

# ★★ 数值常量检查（源码里的**裸字面量**）—— 比搜字符串更硬:
#    注释不进字节码, 但 `_extra[30001] = ...` 里的 30001 会作为常量留在 code object 里。
#    2026-10-16: 上一版是"循环补 dataconfig_activity[].time"，**没有** 30001 这个字面量；
#    这一版改成只补小窝那一个 ⇒ 出现裸的 30001 就说明装的是**新**包。
#    （playerdata.py 里 30001 只在这一行是代码，别处都在注释里 —— 已 grep 确认。）
WANT_CONSTS = {
    "bizserver": {
        "playerdata": [30001],
    },
}


def magic_matches(blob):
    return blob[:4] == b"PYZ\0"


def main():
    print("exe : %s  (%d 字节)" % (EXE, os.path.getsize(EXE)))
    print("PYZ : %s  (%d 字节)" % (PYZ, os.path.getsize(PYZ)))
    blob = open(PYZ, "rb").read()
    if not magic_matches(blob):
        print("!! PYZ 开头不对: %r" % blob[:8])
        return 1
    tocpos = struct.unpack_from(">I", blob, 8)[0]
    print("   python magic @4 = 0x%08X   TOC @ %d" % (
        struct.unpack_from("<I", blob, 4)[0], tocpos))
    toc = marshal.loads(blob[tocpos:])
    print("   TOC 里 %d 个条目" % len(toc))

    def find(name):
        for e in toc:
            n = e[0]
            if n == name or n == name + ".pyc" or n.startswith(name + "."):
                return e
        return None

    OK, BAD = [], []

    def check(cond, msg, extra=""):
        (OK if cond else BAD).append(msg)
        print("  %s %s %s" % ("[OK] " if cond else "[!!] ", msg, extra))

    want = WANT.get(BUILD, {})
    if not want:
        print("\n(这个 exe 没有要验的 PYZ 模块 —— %s 的入口脚本在 CArchive 里, 跳过)" % BUILD)
    for mod, marks in want.items():
        print()
        print("=== %s ===" % mod)
        e = find(mod)
        if e is None:
            check(False, "在 PYZ 里找到 %s" % mod,
                  "样例: %s" % [x[0] for x in toc[:6]])
            continue
        _name, meta = e
        typ, pos, length = meta[0], meta[1], meta[2]
        data = zlib.decompress(blob[pos:pos + length])
        check(True, "%s 解压出来 %d 字节 (typ=%s)" % (mod, len(data), typ))
        for m in marks:
            check(m.encode() in data, "★ 里面有 %r" % m)

        # ★ 数值常量（源码里的裸字面量）—— 比搜字符串更硬，能区分"哪一版"
        for v in WANT_CONSTS.get(BUILD, {}).get(mod, []):
            code = marshal.loads(data)
            seen = set()

            def walk(co):
                for c in co.co_consts:
                    if hasattr(c, "co_consts"):
                        walk(c)
                    elif isinstance(c, (tuple, frozenset)):
                        for x in c:
                            seen.add(x)
                    else:
                        seen.add(c)

            walk(code)
            check(v in seen, "★ 常量表里有裸字面量 %r" % v,
                  "(有它 = 这一版特有的那句代码真的打进包了)")

    print()
    print("=" * 60)
    print("通过 %d 项, 失败 %d 项" % (len(OK), len(BAD)))
    for x in BAD:
        print("   !! " + x)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
