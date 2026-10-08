# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[('LICENSE', '.'), *collect_data_files('trafilatura'),
           *collect_data_files('justext'), *copy_metadata('socksio')],
    hiddenimports=['ddgs.ddgs', *collect_submodules('ddgs.engines')],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 依赖留给 COLLECT 组装，生成 exe 与 _internal 并列的目录发行版。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='search-mcp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    contents_directory='_internal',
    # stdio MCP 需要标准输入输出，保留控制台模式。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='search-mcp',
)
