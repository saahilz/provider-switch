"""Double-click launcher for Provider Switch.

A .pyw file is run by pythonw.exe on Windows, which starts Python without a
console window. All the real work lives in gui.py.
"""
import runpy
import sys
from pathlib import Path

sys.argv = [str(Path(__file__).with_name("gui.py"))]
runpy.run_path(sys.argv[0], run_name="__main__")
