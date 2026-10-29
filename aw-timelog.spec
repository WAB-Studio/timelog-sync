# -*- mode: python -*-
# The bundle copies dist/aw-client into ActivityWatch's folder; aw-qt finds aw-timelog inside it.

a = Analysis(
    ["aw_timelog/__main__.py"],
    pathex=[],
    binaries=None,
    datas=None,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="aw-timelog",
    contents_directory=".",
    debug=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=True, name="aw-client")
