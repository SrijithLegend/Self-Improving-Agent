"""python -m sia ... is the same as the `sia` command (see sia_devtools/cli.py)."""
import sys

from sia_devtools.cli import main

sys.exit(main())
