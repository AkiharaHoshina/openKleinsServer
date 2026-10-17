# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec —— 命令行小工具 kleinstools.exe
#
#   kleinstools.exe roles / cfg / save      （用法见 tools\kleinstools.py 的说明）
#
# ★ 构建（和另外两个 spec 一个布局）:
#     cd <仓库根>
#     python -m PyInstaller --distpath release --workpath _build\kleinstools ^
#         --noconfirm --log-level ERROR tools\kleinstools.spec
#
# ★ `cfgdata` / `paths` 是**普通 import**（不是函数内动态 import），
#   PyInstaller 的静态分析抓得到 —— 所以这个 exe 单独拿走也能跑，
#   不依赖同目录的 .py 文件。
#
# ★ 路径用 SPECPATH 解析, 不要依赖调用时的当前目录。

import os

_ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # tools/ 的上一级 = 仓库根

block_cipher = None

a = Analysis([os.path.join(_ROOT, 'tools', 'kleinstools.py')],
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
          name='kleinstools',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          upx_exclude=[],
          runtime_tmpdir=None,
          console=True )
