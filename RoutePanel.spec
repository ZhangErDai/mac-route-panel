# Build on the target Mac; bundles the Python runtime and static UI.
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'run.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'static'), 'static')],
    hiddenimports=['privileged', 'uvicorn.logging', 'uvicorn.loops.asyncio',
                   'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='RoutePanel', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='RoutePanel')
app = BUNDLE(
    coll, name='本机路由管理.app', bundle_identifier='local.routepanel.app',
    info_plist={
        'CFBundleDisplayName': '本机路由管理',
        'CFBundleShortVersionString': '1.0.1',
        'CFBundleVersion': '2',
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
    },
)
