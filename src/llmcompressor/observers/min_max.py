from typing import Any, Optional, Tuple

import torch
from compressed_tensors.quantization.quant_args import QuantizationArgs
from compressed_tensors.quantization.utils import calculate_qparams
from compressed_tensors.utils import deprecated

from llmcompressor.observers.base import Observer

__all__ = ["MinMaxObserver", "MovingAverageMinMaxObserver"]


@Observer.register("minmax")
class MinMaxObserver(Observer):
    """
    Implements a quantization observer that calculates scale and zero point based on the
    minimum and maximum values of the tensor being observed. If averaging_constant is
    specified, then the scales are updated using a moving average
    """

    def __init__(
        self,
        quantization_args: QuantizationArgs,
        averaging_constant: float = 0.01,
    ):
        super().__init__(quantization_args=quantization_args)

        self.min_val = {}
        self.max_val = {}
        self.averaging_constant = averaging_constant

    def calculate_qparams(
        self,
        observed: torch.Tensor,
        reduce_dims: Optional[Tuple[int]] = None,
        tensor_id: Optional[Any] = None,
        global_scale: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.FloatTensor, torch.IntTensor]:
        """
        Updates the observed min and max using a moving average smoothed by the
        averaging_constant. Set the averaging_constant to 1.0 to disable averaging.

        :param observed: observed tensor to calculate quantization parameters for
        :param reduce_dims: optional tuple of dimensions to reduce along,
            returned scale and zero point will be shaped (1,) along the
            reduced dimensions
        :param tensor_id: Optional id if different ranges of observed tensors are
            passed, useful for sharding tensors by group_size
        :param global_scale: optional scale to further scale local quantization scales
        :return: tuple of scale and zero point derived from the observed tensor
        """
        tensor_id = tensor_id or "default"

        if not reduce_dims:
            min_val, max_val = torch.aminmax(observed)
        else:
            min_val = torch.amin(observed, dim=reduce_dims, keepdims=True)
            max_val = torch.amax(observed, dim=reduce_dims, keepdims=True)

        # early stopping, save some computation and memory
        if self.averaging_constant == 1.0:
            return calculate_qparams(
                min_vals=min_val,
                max_vals=max_val,
                quantization_args=self.quantization_args,
                global_scale=global_scale,
            )

        running_min_val = self.min_val.get(tensor_id, None)
        running_max_val = self.max_val.get(tensor_id, None)

        if running_min_val is None or running_max_val is None:
            updated_min_val = min_val
            updated_max_val = max_val
        else:
            updated_min_val = running_min_val + self.averaging_constant * (
                min_val - running_min_val
            )
            updated_max_val = running_max_val + self.averaging_constant * (
                max_val - running_max_val
            )

        self.min_val[tensor_id] = updated_min_val
        self.max_val[tensor_id] = updated_max_val

        return calculate_qparams(
            min_vals=updated_min_val,
            max_vals=updated_max_val,
            quantization_args=self.quantization_args,
            global_scale=global_scale,
        )

    def get_qparams_along_dim(
        self,
        observed: torch.Tensor,
        dim: int,
        tensor_id: Optional[Any] = None,
        global_scale: Optional[torch.Tensor] = None,
    ):
        """
        Calculate quantization parameters along the specified dimension
        """
        reduce_dims = tuple(idx for idx in range(observed.ndim) if idx != dim)
        return self.calculate_qparams(
            observed,
            reduce_dims=reduce_dims,
            tensor_id=tensor_id,
            global_scale=global_scale,
        )

    def reset(self):
        """
        Reset the state of the observer, including min and maximum values
        """
        super().reset()
        self.min_val = {}
        self.max_val = {}


class MovingAverageMinMaxObserver(MinMaxObserver):
    @deprecated(
        message=(
            "The class name `MovingAverageMinMaxObserver` has been deprecated, please "
            "initialize with `MinMaxObserver` in the future"
        )
    )
    def __new__(cls, *args, **kwargs):
        return super().__new__(MinMaxObserver, *args, **kwargs)
