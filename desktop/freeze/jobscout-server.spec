# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['entry.py'],
    pathex=['.'],
    binaries=[],
    datas=[('/Users/user/Downloads/Personal Project/jobscout/jobscout/webapp/templates', 'jobscout/webapp/templates'), ('/Users/user/Downloads/Personal Project/jobscout/jobscout/webapp/static', 'jobscout/webapp/static'), ('/Users/user/Downloads/Personal Project/jobscout/jobscout/defaults', 'jobscout/defaults')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='jobscout-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    name='jobscout-server',
)
