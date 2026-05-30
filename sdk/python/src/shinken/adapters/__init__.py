"""Off-the-shelf computer-use provider adapters (#75 / #76).

Each adapter translates a provider's computer-use tool contract into canonical ACI
actions (and Shinken observations back into the provider's result shape), so an
off-the-shelf CU agent can drive Shinken. Fixture-tested, no live API calls.
"""

from .anthropic import AnthropicComputerUseAdapter
from .base import AdapterError

__all__ = ["AdapterError", "AnthropicComputerUseAdapter"]
