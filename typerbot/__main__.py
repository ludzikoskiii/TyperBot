import sys

if len(sys.argv) == 1:
    # Bez argumentów – interfejs graficzny.
    from typerbot.ui.app import run

    sys.exit(run())

from typerbot.cli import main

sys.exit(main())
