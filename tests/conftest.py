import sys
from pathlib import Path

# Let the tests import the dashboard modules from the folder above
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
