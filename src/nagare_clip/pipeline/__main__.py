"""Run the pipeline orchestrator via ``python -m nagare_clip.pipeline``."""

import sys

from nagare_clip.pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())
