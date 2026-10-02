#!/usr/bin/env python3
"""Load the built zip exactly the way Electrum loads external plugins (alias package name, zipimport).

Usage: check_zip_import.py <electrum_src_dir> <plugin.zip>
Catches import forms that work from source but break under the zip loader.
"""
import importlib.util
import os
import sys
import zipimport

sys.path.insert(0, sys.argv[1])
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication  # noqa: E402
app = QApplication([])
zip_path = sys.argv[2]
name = 'lowtide'
base = f'electrum_external_plugins.{name}'
zf = zipimport.zipimporter(zip_path)
init_spec = zf.find_spec(name)
mod = importlib.util.module_from_spec(init_spec)
sys.modules[base] = mod
init_spec.loader.exec_module(mod)
print('init ok; module __name__ =', mod.__name__)
spec = importlib.util.find_spec(f'{base}.qt')
qt = importlib.util.module_from_spec(spec)
sys.modules[f'{base}.qt'] = qt
spec.loader.exec_module(qt)
print('qt ok:', qt.Plugin)
