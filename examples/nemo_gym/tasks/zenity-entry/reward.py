"""CUA-Gym-contract scorer: the dialog's OK/Return prints the entry to stdout."""
from pathlib import Path

try:
    content = Path("/tmp/vendor.txt").read_text()
except OSError:
    content = ""
print(f"REWARD: {1.0 if content.strip() == 'ACME GmbH' else 0.0}")
