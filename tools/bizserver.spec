# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec —— 业务层 bizserver (TCP 10301)
#
# ★ 构建布局（与 build_release.ps1 一致）:
#     workpath = <仓库根>\_build\bizserver\   ← PYZ 在这里, tools\_verify_exe.py 按这个路径找
#     distpath = <仓库根>\release\            ← exe 在这里（已 gitignore）
#
# ★ datas=[] 是**故意的** —— 配置表(cfg\)、routes.json、player.json 等都不打进 exe,
#   全部靠 exe 同目录读取（见 paths.py）。改这些不用重打包。
#
# ★ 路径用 SPECPATH 解析, 不要依赖调用时的当前目录。

import os

_ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # tools/ 的上一级 = 仓库根

block_cipher = None

a = Analysis([os.path.join(_ROOT, 'bizserver.py')],
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
          name='bizserver',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          upx_exclude=[],
          runtime_tmpdir=None,
          console=True )
