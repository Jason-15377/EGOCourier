import importlib, sys
mods = ["PySide6", "PyQt5", "matplotlib", "imageio", "paramiko", "scipy", "numpy"]
for m in mods:
    try:
        mod = importlib.import_module(m)
        print(f"  {m:12} OK  {getattr(mod, '__version__', '')}")
    except Exception as e:
        print(f"  {m:12} MISSING ({type(e).__name__})")
print("pip:", sys.version)
