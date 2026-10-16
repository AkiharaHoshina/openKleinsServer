# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec —— HTTP 层 fakeserver (10300)
#
# ★ fakeserver **故意不 import playerdata**（两边靠文件标记 + 环境变量通信），
#   所以它的 PYZ 比 bizserver 小、也没有 playerdata 模块 —— 这是设计，不是漏打包。
#
# ★ 构建布局（与 build_release.ps1 一致）:
#     workpath = <仓库根>\_build\fakeserver\
#     distpath = <仓库根>\release\
#
# ★ 路径用 SPECPATH 解析, 不要依赖调用时的当前目录。

import os

_ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # tools/ 的上一级 = 仓库根

block_cipher = None

a = Analysis([os.path.join(_ROOT, 'fakeserver.py')],
             pathex=[_ROOT],
             binaries=[],
             datas=[],
             hiddenimports=[],
             hookspath=[],
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          [],
          name='fakeserver',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          upx_exclude=[],
          runtime_tmpdir=None,
          console=True )
