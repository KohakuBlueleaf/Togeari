"""``TorchEngine``: devices and models as batch units, one per (model, device) (engines/02).

Importing this package registers the tensor digest (``digest.tensor_digest``).
"""

import torch

from togeari.adapters.torch import TorchAdapter
from togeari.core.contracts.digest import register_digest
from togeari.engines.torch.digest import tensor_digest
from togeari.engines.torch.engine import TorchEngine, TorchUnit
from togeari.engines.torch.model import TorchModel, TorchModule

register_digest(torch.Tensor, tensor_digest)

__all__ = [
    "TorchAdapter",
    "TorchEngine",
    "TorchModel",
    "TorchModule",
    "TorchUnit",
    "tensor_digest",
]
