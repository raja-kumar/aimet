# /usr/bin/env python3.6
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2020, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================

""" Utilities for ConnectedGraph """

from typing import Tuple, Union, List, Dict
import torch

# Import AIMET specific modules
from aimet_common.utils import AimetLogger
from aimet_torch.meta.connectedgraph import ConnectedGraph
from aimet_torch.utils import create_rand_tensors_given_shapes, get_device

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Utils)

ActivationTypes = (torch.nn.ReLU6, torch.nn.ReLU, torch.nn.PReLU, torch.nn.RReLU, torch.nn.LeakyReLU,
                   torch.nn.Sigmoid, torch.nn.LogSigmoid, torch.nn.Softmin, torch.nn.Softmax, torch.nn.LogSoftmax,
                   torch.nn.Tanh, torch.nn.Hardtanh)


def get_module_act_func_pair(model: torch.nn.Module, model_input: Union[Tuple[torch.Tensor], List[torch.Tensor]]) -> \
        Dict[torch.nn.Module, Union[torch.nn.Module, None]]:
    """
    For given model, returns dictionary of module to immediate following activation function else maps
    module to None.

    Activation functions should be defined as nn.Modules in model and not as functional in the forward pass.

    :param model: Pytorch model
    :param model_input:  Model input, Can be a list/tuple of input tensor(s)
    :return: Dictionary of module to activation function
    """
    # Keep model in evaluation mode
    model.eval()

    # Create ConnectedGraph
    graph = ConnectedGraph(model, model_input)

    # Maps module to next following activation function else None
    module_act_func_pair = {}

    # Get all the ops
    all_ops = graph.get_all_ops()

    for op in all_ops.values():

        # Get module associated with op
        cur_module = op.get_module()

        if cur_module:
            module_act_func_pair[cur_module] = None

            if op.output:
                assert op.output.consumers, 'op output should have at least one consumer op.'
                # Get the next op
                next_op = op.output.consumers[0]
                # Get module associated with next op
                next_module = next_op.get_module()

                # Get the appropriate activation function
                if isinstance(next_module, ActivationTypes):
                    module_act_func_pair[cur_module] = next_module
                    logger.debug("Module: %s is followed by activation function: %s", op.dotted_name,
                                 next_op.dotted_name)

    return module_act_func_pair


def create_connected_graph_with_input_shapes(model: torch.nn.Module, input_shapes: Union[Tuple, List[Tuple]]) \
        -> ConnectedGraph:
    """
    Create connected graph, using random inputs generated from given input shapes.
    :param model: torch model to create a connected graph from
    :param input_shapes: input shapes to the torch model
    :return: ConnectedGraph representation of the model
    """
    random_inputs = create_rand_tensors_given_shapes(input_shapes)
    device = get_device(model)
    random_inputs = tuple([inp.to(device) for inp in random_inputs])
    return ConnectedGraph(model, random_inputs)
