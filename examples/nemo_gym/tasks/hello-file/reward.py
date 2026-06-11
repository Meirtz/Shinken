"""CUA-Gym-contract scorer: last stdout line must be 'REWARD: X.X'."""
from pathlib import Path

try:
    content = Path("/tmp/hello.txt").read_text()
except OSError:
    content = ""
print(f"REWARD: {1.0 if content.strip() == 'shinken' else 0.0}")
