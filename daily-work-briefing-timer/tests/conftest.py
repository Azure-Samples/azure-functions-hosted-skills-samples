import sys
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TEMPLATE_DIR / "src" / "tools"))
sys.path.insert(0, str(TEMPLATE_DIR / "scripts"))
