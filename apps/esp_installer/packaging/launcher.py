"""Entry script of the frozen app; the same main() as ``python -m sl_installer``.

PyInstaller runs its entry script as ``__main__``, so the real module is
imported by name to keep its package context.
"""
import sys

from sl_installer.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
