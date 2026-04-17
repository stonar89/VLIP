# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)
import os

project_dir = os.path.abspath(".")
script_name = os.path.join(project_dir, "vlip.py")
icon_path = os.path.join(project_dir, "icon.ico")

datas = []
binaries = []
hiddenimports = []

# ---- Bundle Vina ----
vina_path = os.path.join(project_dir, "vina.exe")
if os.path.exists(vina_path):
    binaries.append((vina_path, "."))

# ---- Matplotlib ----
try:
    hiddenimports += [
        m for m in collect_submodules("matplotlib")
        if ".tests" not in m and ".testing" not in m
    ]
except Exception:
    hiddenimports += ["matplotlib"]

hiddenimports += ["mpl_toolkits.mplot3d"]

# ---- PLIP ----
try:
    hiddenimports += collect_submodules("plip")
    datas += collect_data_files("plip")
except Exception:
    pass

# ---- OpenBabel ----
try:
    hiddenimports += collect_submodules("openbabel")
except Exception:
    pass

try:
    datas += collect_data_files("openbabel")
except Exception:
    pass

try:
    binaries += collect_dynamic_libs("openbabel")
except Exception:
    pass

manual_dlls = [
    # (r"C:\path\to\openbabel.dll", "."),
    # (r"C:\path\to\libopenbabel-7.dll", "."),
]
for dll in manual_dlls:
    if os.path.exists(dll[0]):
        binaries.append(dll)

a = Analysis(
    [script_name],
    pathex=[project_dir],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib.tests",
        "numpy.tests",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VLIP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=icon_path if os.path.exists(icon_path) else None,
)