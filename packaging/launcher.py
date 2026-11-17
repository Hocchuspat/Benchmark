"""PyInstaller needs a script entry point with absolute package imports."""
from benchmark.app import main

raise SystemExit(main())
