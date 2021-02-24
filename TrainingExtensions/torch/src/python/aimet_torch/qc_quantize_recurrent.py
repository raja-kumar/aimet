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

""" Custom PyTorch Op for quantizing weights and activations for Recurrent Layers """
from typing import Tuple, List, Union, Dict

import torch
from torch.nn.utils.rnn import PackedSequence, pad_packed_sequence, pack_padded_sequence
from aimet_common.defs import QuantScheme
from aimet_common.utils import AimetLogger
from aimet_torch.defs import OpToIOTensors
from aimet_torch.qc_quantize_op import MAP_ROUND_MODE_TO_PYMO, MAP_QUANT_SCHEME_TO_PYMO
from aimet_torch.qc_quantize_op import QcQuantizeOpMode, tensor_quantizer_factory
from aimet_torch.tensor_quantizer import PostTrainingTensorQuantizer

import libpymo


_logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)


# grouped quantizers configuration with tensor that share the same quantizer

# tensor names for input and output is defined in initialization of QcQuantizeRecurrent Module and params are
# defined in torch module that is being quantized.

# *Note* Group Quantizer cfg below are defined based on eAI HW configuration
grouped_quantizer_map = {
    'hidden_l{}': ['initial_h_l{}', 'h_l{}'],
    'cell_l{}': ['initial_c_l{}', 'c_l{}'],
    'bias_l{}': ['bias_ih_l{}', 'bias_hh_l{}', 'bias_ih_l{}_reverse', 'bias_hh_l{}_reverse'],
    'W_l{}': ['weight_ih_l{}', 'weight_ih_l{}_reverse'],
    'R_l{}': ['weight_hh_l{}', 'weight_hh_l{}_reverse']
}

onnx_inputs_to_quantizers = [
    # X: The input sequences packed (and potentially padded) into one 3-D tensor
    # with the shape of `[seq_length, batch_size, input_size]`.
    'input_l{}',
    # W: The weight tensor for the gates. Concatenation of `W[iofc]` and `WB[iofc]`
    # (if bidirectional) along dim 0. The tensor has shape `[num_directions, 4*hidden_size, input_size]`.
    'W_l{}',
    # R: The recurrence weight tensor. Concatenation of `R[iofc]` and `RB[iofc]`
    # (if bidirectional) along dim 0. This tensor has shape `[num_directions, 4*hidden_size, hidden_size]`.
    'R_l{}',
    # B: The bias tensor for input gate. Concatenation of `[Wb[iofc], Rb[iofc]]`, and `[WBb[iofc], RBb[iofc]]`
    # (if bidirectional) along dim 0. This tensor has shape `[num_directions, 8*hidden_size]`.
    'bias_l{}',
    # sequence_lens: Optional tensor specifying lengths of the sequences in a batch.
    None,
    # initial_h: Optional initial value of the hidden.
    'initial_h_l{}',
    # initial_c: Optional initial value of the cell.
    'initial_c_l{}',
    # P: The weight tensor for peepholes. Concatenation of `P[iof]` and `PB[iof]` (if bidirectional)
    # along dim 0. It has shape `[num_directions, 3*hidden_size]`.
    None
]

onnx_outputs_to_quantizers = [
    # Y: A tensor that concats all the intermediate output values of the hidden.
    # It has shape `[seq_length, num_directions, batch_size, hidden_size]`.
    'h_l{}',
    # Y_h: The last output value of the hidden. It has shape `[num_directions, batch_size, hidden_size]`.
    'h_l{}',
    # Y_c: The last output value of the cell. It has shape `[num_directions, batch_size, hidden_size]`.
    'c_l{}'
]


class PackedSequenceInfo:
    """
    Holds information for packed sequence inputs
    """
    def __init__(self, sequence_lens, batch_sizes, unsorted_indices, sorted_indices):
        self.sequence_lens = sequence_lens
        self.batch_sizes = batch_sizes
        self.unsorted_indices = unsorted_indices
        self.sorted_indices = sorted_indices
        self._sorted_sequence_lens, _ = torch.sort(self.sequence_lens, descending=True)

    @property
    def sorted_sequence_lens(self):
        """ Return sequence_lens sorted in descending order """
        return self._sorted_sequence_lens


class QcQuantizeRecurrent(torch.nn.Module):
    """
    Learns Min and Max for Encodings of Enabled quantizers for a recurrent layer
    """
    # pylint: disable = too-many-arguments
    # pylint: disable = too-many-instance-attributes
    def __init__(self, module_to_quantize: Union[torch.nn.RNN, torch.nn.LSTM, torch.nn.GRU],
                 weight_bw: int, activation_bw: int, round_mode: str,
                 quant_scheme: Union[QuantScheme, libpymo.QuantizationMode], is_symmetric: bool = False):
        """
        Constructor
        :param module_to_quantize: Module that needs to be quantized
        :param weight_bw: Quantization bitwidth for weights
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_symmetric: Symmetric or asymmetric quantization
        """
        super(QcQuantizeRecurrent, self).__init__()

        self._mode = QcQuantizeOpMode.PASSTHROUGH
        # clone parameter
        self._clone_module_params(module_to_quantize)
        self.module_to_quantize = module_to_quantize

        round_mode = MAP_ROUND_MODE_TO_PYMO[round_mode]
        quant_scheme = MAP_QUANT_SCHEME_TO_PYMO[quant_scheme]

        self._grouped_quantizers = {}
        self._param_quantizers = {}
        self._grouped_param_quantizers = set()

        hasCellState: bool = isinstance(self.module_to_quantize, torch.nn.LSTM)
        outputs = ['h_l{}', 'c_l{}'] if hasCellState else ['h_l{}']
        self._output_quantizers = self._create_activation_quantizers(outputs, activation_bw, round_mode,
                                                                     quant_scheme, is_symmetric)

        inputs = ['input_l{}', 'initial_h_l{}', 'initial_c_l{}'] if hasCellState else ['input_l{}', 'initial_h_l{}']
        self._input_quantizers = self._create_activation_quantizers(inputs, activation_bw, round_mode,
                                                                    quant_scheme, is_symmetric)

        self._create_param_quantizers(weight_bw, round_mode, quant_scheme, is_symmetric)
        self._set_default_eai_quantizer_state()

        # flag to control if initial hidden state quantization during analysis should be done post computation
        # the reason for forcing this sequence is that in TF Enhanced mode, when initial_h and ht are grouped, the
        # initial_h tensor at times is substantial different from subsequent ht causing the quantizer initialization
        # to be sub-optimal.
        self._reorder_initial_h_c_stats_update = QcQuantizeRecurrent.is_initial_h_c_stats_update_reordered()

    @staticmethod
    def is_initial_h_c_stats_update_reordered() -> bool:
        """
        :return: True if initial_h quantization analysis requires re-ordering
        """
        for tensor_names in grouped_quantizer_map.values():
            if 'h_l{}' in tensor_names and 'initial_h_l{}' in tensor_names or \
                    'c_l{}' in tensor_names and 'initial_c_l{}' in tensor_names:
                return True
        return False

    @staticmethod
    def _get_group_name(tensor_name: str, layer: Union[int, None] = None) -> Union[str, None]:
        """
        :return: a group name if the tensor belongs to a group else none
        """
        for group_name, tensors in grouped_quantizer_map.items():
            tensors = [tensor.format(layer) for tensor in tensors]
            if tensor_name in tensors:
                return group_name.format(layer)
        return None

    # property decorator is used for all the quantizer sets to keep the dictionary entries static post construction

    @property
    def grouped_quantizers(self):
        """ Return dictionary of grouped quantizer """
        return self._grouped_quantizers

    @property
    def param_quantizers(self):
        """ Return dictionary of param quantizer """
        return self._param_quantizers

    @property
    def output_quantizers(self):
        """ Return dictionary of param quantizer """
        return self._output_quantizers

    @property
    def input_quantizers(self):
        """ Return dictionary of param quantizer """
        return self._input_quantizers

    def _create_activation_quantizers(self, tensor_names: List[str], activation_bw: int,
                                      round_mode: libpymo.RoundingMode, quant_scheme: libpymo.QuantizationMode,
                                      is_symmetric: bool) -> Dict[str, PostTrainingTensorQuantizer]:
        """
        helper method to construct activation quantizers
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_symmetric: Symmetric or asymmetric quantization
        """
        quantizers = {}
        for layer in range(self.num_layers):
            for name in tensor_names:
                name_in_layer = name.format(layer)
                group_name = QcQuantizeRecurrent._get_group_name(name_in_layer, layer)
                if group_name:
                    if group_name not in self._grouped_quantizers:
                        self._grouped_quantizers[group_name] = \
                            tensor_quantizer_factory(activation_bw,
                                                     round_mode,
                                                     quant_scheme,
                                                     is_symmetric,
                                                     enabled_by_default=False)
                    quantizers[name_in_layer] = self._grouped_quantizers[group_name]
                else:
                    quantizers[name_in_layer] = tensor_quantizer_factory(
                        activation_bw,
                        round_mode,
                        quant_scheme,
                        is_symmetric,
                        enabled_by_default=False)
        return quantizers

    def _create_param_quantizers(self, weight_bw: int, round_mode: libpymo.RoundingMode,
                                 quant_scheme: libpymo.QuantizationMode, is_symmetric: bool):
        """
        helper method to construct param quantizers
        :param weight_bw: Quantization bitwidth for weights
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_symmetric: Symmetric or asymmetric  quantization
        """
        tensor_grouped_quantizer_map = {}
        for layer in range(self.num_layers):
            for group_name, tensor_names in grouped_quantizer_map.items():
                name = group_name.format(layer)
                if name not in self._grouped_quantizers:
                    self._grouped_quantizers[name] = \
                        tensor_quantizer_factory(weight_bw,
                                                 round_mode,
                                                 quant_scheme,
                                                 is_symmetric,
                                                 enabled_by_default=False)
                tensor_names = [tensor_name.format(layer) for tensor_name in tensor_names]
                for tensor_name in tensor_names:
                    assert tensor_name not in tensor_grouped_quantizer_map
                    tensor_grouped_quantizer_map[tensor_name] = \
                        self._grouped_quantizers[name]

        for name, _ in self.module_to_quantize.named_parameters():
            _logger.debug("Adding quantizer for parameter: %s", name)
            if name in tensor_grouped_quantizer_map:
                self._param_quantizers[name] = tensor_grouped_quantizer_map[name]
                self._grouped_param_quantizers.add(self._param_quantizers[name])
            else:
                self._param_quantizers[name] = tensor_quantizer_factory(
                    weight_bw,
                    round_mode,
                    quant_scheme,
                    is_symmetric,
                    enabled_by_default=False)

    def _set_default_eai_quantizer_state(self):
        """
        Disables/Enables quantizer for eAI HW configuraiton
        """

        default_group_quantizer_state = {
            'hidden_l{}': True,
            'cell_l{}': False,
            'bias_l{}': False,
            'W_l{}':  True,
            'R_l{}': True
        }

        default_group_quantizer_state = {name.format(layer): state
                                         for layer in range(self.num_layers)
                                         for name, state in default_group_quantizer_state.items()
                                         }

        tensor_names_with_grouped_quantizers = [name.format(layer)
                                                for layer in range(self.num_layers)
                                                for tensor_names in grouped_quantizer_map.values()
                                                for name in tensor_names
                                                ]

        for name, quantizer in self._grouped_quantizers.items():
            quantizer.enabled = default_group_quantizer_state[name]

        for name, quantizer in self._output_quantizers.items():
            if name not in tensor_names_with_grouped_quantizers:
                quantizer.enabled = False

        for name, quantizer in self._param_quantizers.items():
            if name not in tensor_names_with_grouped_quantizers:
                quantizer.enabled = True

        for name, quantizer in self._input_quantizers.items():
            if name not in tensor_names_with_grouped_quantizers:
                quantizer.enabled = True

    def _clone_module_params(self, module: Union[torch.nn.RNN, torch.nn.LSTM, torch.nn.GRU]):
        """
        helper method to clone params from the original module
        :param module: Module from which the custom quantize Op will be clone from.
        """
        self.hidden_size = module.hidden_size
        self.num_layers = module.num_layers
        self.batch_first = module.batch_first
        self.num_directions = 2 if module.bidirectional else 1
        self.mode = module.mode
        self.bias = module.bias
        for layer in range(module.num_layers):
            for direction in range(self.num_directions):
                for param_name in self._get_param_names(direction, layer):
                    setattr(self, param_name, torch.nn.Parameter(getattr(module, param_name).data.clone()))

    def set_output_bw(self, output_bw: int):
        """
        Sets (overrides) the output bitwidth for a particular layer
        :param output_bw: Bitwidth from (4-32)
        """
        for output_quantizer in self._output_quantizers:
            output_quantizer.bitwidth = output_bw

    def set_mode(self, mode: QcQuantizeOpMode):
        """
        Sets a working mode for the custom op
        :param mode:
        """
        self._mode = mode

    def _quantize_dequantize_params(self) -> Dict[str, torch.Tensor]:
        """
        Quantizes and dequantizes a parameter
        @returns A dictionary of parameters (with quantization noise if enabled.)
        """
        params = dict()

        for param_quantizer in self._grouped_param_quantizers:
            if self.training or param_quantizer.encoding is None:
                param_quantizer.reset_encoding_stats()

        grouped_param_to_quantize_dequantize = {}
        # Quantize the parameters, if present
        for name, param in self.named_parameters(recurse=False):

            data = param.clone()
            param_quantizer = self._param_quantizers[name]

            # If we are in training mode with quant-sim nodes,
            # then we want to calculate encodings for the parameters in every pass
            if self.training or param_quantizer.encoding is None:
                if param_quantizer in self._grouped_param_quantizers:
                    param_quantizer.update_encoding_stats(data)
                    grouped_param_to_quantize_dequantize[name] = param
                    continue
                else:
                    param_quantizer.reset_encoding_stats()
                    param_quantizer.update_encoding_stats(data)
                    param_quantizer.compute_encoding()

            params[name] = self._param_quantize_dequantize(data, param_quantizer)

        for param_quantizer in self._grouped_param_quantizers:
            if self.training or param_quantizer.encoding is None:
                param_quantizer.compute_encoding()

        for name, param in grouped_param_to_quantize_dequantize.items():
            params[name] = self._param_quantize_dequantize(param.clone(), self._param_quantizers[name])

        return params

    def _param_quantize_dequantize(self, data: torch.Tensor, param_quantizer: PostTrainingTensorQuantizer) -> \
            torch.Tensor:
        """
        Helper method for quantizing-dequantizing parameters
        :param: data: Parameter Tensor input to which quantization noise is added.
        :param: param_quantizer: tensor quantizer to use for quantization.
        :return: quantized and dequantized param tensor
        """
        # if we are not in training, then only nearest rounding should be used
        # else we should use whatever the user desires (i.e.. stochastic rounding is a valid option)
        if self.training:
            round_mode = param_quantizer.round_mode
        else:
            round_mode = libpymo.RoundingMode.ROUND_NEAREST
        return param_quantizer.quantize_dequantize(data, round_mode)

    def compute_weight_encodings(self) -> Dict[str, libpymo.TfEncoding]:
        """
        Compute quantized model weight encoding.
        :return: dictionary of weight encodings value (libpymo.TfEncoding type)
        """

        encodings = {}
        for name, quantizer in self._param_quantizers.items():
            if 'weight' in name:
                quantizer.compute_encoding()
                encodings[name] = quantizer.encoding

        return encodings

    def compute_encoding(self):
        """
        Compute the quantization encoding for this layer
        :return: None
        """
        for input_quantizer in self._input_quantizers.values():
            input_quantizer.compute_encoding()
        for output_quantizer in self._output_quantizers.values():
            output_quantizer.compute_encoding()

    def get_activation_param_quantizers_for_onnx_tensors(
            self, io_tensor_map: Union[OpToIOTensors, List[OpToIOTensors]]) -> \
            Dict[str, PostTrainingTensorQuantizer]:
        """
        Retrieve mapping from onnx tensor names and quantizers
        :param io_tensor_map: ONNX IO tensor maps,
                provided as a list of entries if recurrent module has more than one layer i.e. onnx node info per layer.
        :return: pair of map for activation and param quantizers indexed by onnx tensor name
        """

        activations_quantizer_map = {}
        params_quantizer_map = {}

        if not isinstance(io_tensor_map, list):
            io_tensor_map = [io_tensor_map]

        for layer in range(self.module_to_quantize.num_layers):
            inputs = io_tensor_map[layer].inputs
            for index, quantizer_name in enumerate(onnx_inputs_to_quantizers):
                if quantizer_name is None:
                    continue
                quantizer_name = quantizer_name.format(layer)
                quantizer = self._input_quantizers[quantizer_name] \
                    if quantizer_name in self._input_quantizers else self._grouped_quantizers[quantizer_name]
                if quantizer.enabled:
                    if quantizer in self._param_quantizers.values():
                        params_quantizer_map[inputs[index]] = quantizer
                    else:
                        activations_quantizer_map[inputs[index]] = quantizer

            outputs = io_tensor_map[layer].outputs
            for index, quantizer_name in enumerate(onnx_outputs_to_quantizers):
                if quantizer_name is None:
                    continue
                quantizer_name = quantizer_name.format(layer)
                quantizer = self._output_quantizers[quantizer_name] \
                    if quantizer_name in self._output_quantizers else self._grouped_quantizers[quantizer_name]
                if quantizer.enabled:
                    activations_quantizer_map[outputs[index]] = quantizer

        return activations_quantizer_map, params_quantizer_map

    def _quantize_activation(self, tensor_quantizer: PostTrainingTensorQuantizer,
                             tensors_to_quantize: Union[List[torch.Tensor], torch.Tensor]) -> \
            Union[List[torch.Tensor], torch.Tensor]:
        """
        Forward-pass routine. quantizes input and output tensor
        :param tensor_quantizer: Tensor quantizer to use for updating stats or quantizing
        :param tensors_to_quantize: Inputs passed to the module in the forward pass
        :return: Quantized output of type Tensor or Tuple of Tensors
        """

        if not tensor_quantizer.enabled:
            return tensors_to_quantize

        outputs = []
        if not isinstance(tensors_to_quantize, list):
            tensors_to_quantize = [tensors_to_quantize]
        for input_tensor in tensors_to_quantize:

            if self._mode is QcQuantizeOpMode.ANALYSIS:

                if isinstance(input_tensor, tuple):
                    for tensor in input_tensor:
                        tensor_quantizer.update_encoding_stats(tensor)
                else:
                    tensor_quantizer.update_encoding_stats(input_tensor)
                output = input_tensor

            elif self._mode is QcQuantizeOpMode.ACTIVE:
                # if we are not in training, then only nearest rounding should be used
                if self.training:
                    round_mode = tensor_quantizer.round_mode
                else:
                    round_mode = libpymo.RoundingMode.ROUND_NEAREST
                output = tensor_quantizer.quantize_dequantize(input_tensor, round_mode)

            else:
                output = input_tensor

            outputs.append(output)

        # Flatten if there is only one output
        return outputs[0] if len(outputs) == 1 else tuple(outputs)

    def reset_encodings(self):
        """
        Reset encoding stats and set encodings to None for all quantizers
        """
        group_quantizers = self._grouped_quantizers.values()
        for quantizer in group_quantizers:
            quantizer.reset_encoding_stats()
        for param_quantizer in self._param_quantizers.values():
            if param_quantizer not in group_quantizers:
                param_quantizer.reset_encoding_stats()
        for input_quantizer in self._input_quantizers.values():
            if input_quantizer not in group_quantizers:
                input_quantizer.reset_encoding_stats()
        for output_quantizer in self._output_quantizers.values():
            if output_quantizer not in group_quantizers:
                output_quantizer.reset_encoding_stats()

    def update_params(self):
        """
        Copy params into corresponding parameters in self.module_to_quantize
        """
        for layer in range(self.num_layers):
            for direction in range(self.num_directions):
                for param_name in self._get_param_names(direction, layer):
                    param = getattr(self.module_to_quantize, param_name)
                    param.data = getattr(self, param_name).data

        _logger.debug('Updated params for QcQuantizeRNN')

    def _get_param_names(self, direction: int, layer: int) -> List[str]:
        """
        get list of parameter names for given layer & direction
        :param direction: set 0 for forward and 1 for reverse direction
        :param layer: layer number
        :return: List of parameter names for the reference layer,direction
        """
        suffix = '_reverse' if direction == 1 else ''
        param_names = ['weight_ih_l{}{}', 'weight_hh_l{}{}']
        if self.bias:
            param_names += ['bias_ih_l{}{}', 'bias_hh_l{}{}']
        param_names = [x.format(layer, suffix) for x in param_names]
        return param_names

    # mapping of Recurrent Type to functional op
    # pylint: disable=protected-access
    # pylint: disable=no-member
    rnn_impl_map = {'RNN_TANH': torch.nn._VF.rnn_tanh_cell,
                    'RNN_RELU': torch.nn._VF.rnn_relu_cell,
                    'LSTM': torch.nn._VF.lstm_cell,
                    'GRU': torch.nn._VF.gru_cell
                    }

    @staticmethod
    def _format_hx_output(stacked_hx: Union[List[Tuple[torch.Tensor]], List[torch.Tensor]]) \
            -> Union[Tuple[torch.Tensor], torch.Tensor]:
        """
        Helper method to reformat h value into tuple of tensor or tensor
        if LSTM the hidden state has to reformatted from ((h_0,c_0),...) to ([h_0,h_1,..],[c_0, c_1,..] )
        :param stacked_hx: list of Tensor in case of RNN & GRU or list of Tensor Tuple in case of LSTM
        :return: Tensor (RNN,GRU) or Tensor Tuple(LSTM)
        """
        if isinstance(stacked_hx[0], tuple):
            h = [h[0] for h in stacked_hx]
            c = [h[1] for h in stacked_hx]
            hx = (torch.stack(h), torch.stack(c))
        else:
            hx = torch.stack(stacked_hx)
        return hx

    # pylint: disable=too-many-locals
    # pylint: disable=arguments-differ
    def forward(self, inputs: Union[torch.Tensor, PackedSequence], hx: torch.Tensor = None) -> \
            Tuple[torch.Tensor, Union[Tuple[torch.Tensor], torch.Tensor]]:
        """
        forward pass function -- implements Recurrent Layer (RNN,LSTM,GRU) with quantization enabled.
        :param inputs:  input Tensor
        :param hx: initial hidden state Tensor
        :return: output tensor and hidden state tensor -- (RNN,GRU) or Tensor Tuple(LSTM)
        """
        inputs, packed_sequence_info = _get_inputs_and_packed_sequence_info(inputs, self.batch_first)

        # if input is set to batch first, reformat to set timestep as to first dim, followed by batch
        if self.batch_first:
            inputs = inputs.permute(1, 0, 2)

        steps = inputs.shape[0]
        batches = inputs.shape[1]
        stacked_hx = []
        output = []

        quantized_params = self._quantize_dequantize_params()

        for layer in range(self.num_layers):
            # Quantize the inputs
            quantized_input = self._quantize_activation(self._input_quantizers['input_l{}'.format(layer)], inputs)

            output = []
            reverse_pass_output = []
            for direction in range(self.num_directions):
                permutation = None if not packed_sequence_info else packed_sequence_info.unsorted_indices
                update_initial_hx_encoding_stats, initial_hx = \
                    self._intialize_quantize_hidden_state(batches, inputs, layer, hx, permutation=permutation)
                cell_hx = initial_hx

                param = [quantized_params[p] for p in self._get_param_names(direction, layer)]
                weight_ih, weight_hh, *bias = param
                bias_ih, bias_hh = bias if bias else (None, None)

                if direction == 1:
                    quantized_input = _get_flipped_input_for_reverse_pass(quantized_input, packed_sequence_info, steps)

                for iteration in range(steps):

                    new_cell_hx = self.rnn_impl_map[self.mode](quantized_input[iteration],
                                                               cell_hx,
                                                               weight_ih,
                                                               weight_hh,
                                                               bias_ih,
                                                               bias_hh)

                    # Replace rows in the hidden state corresponding to valid inputs in the batch
                    cell_hx = _replace_appropriate_hidden_state_rows(cell_hx, new_cell_hx, packed_sequence_info,
                                                                     iteration, batches)
                    # Quantize the outputs
                    cell_hx = self._quantize_hidden_cell_state(layer, cell_hx)

                    if direction == 0:
                        output.append(cell_hx[0] if isinstance(cell_hx, tuple) else cell_hx)
                    else:
                        if not reverse_pass_output:
                            reverse_pass_output = [None] * (steps * batches)
                        _fill_appropriate_rows_in_reverse_pass_output(reverse_pass_output,
                                                                      packed_sequence_info,
                                                                      steps,
                                                                      batches,
                                                                      iteration,
                                                                      cell_hx)
                stacked_hx.append(cell_hx)
                if update_initial_hx_encoding_stats:
                    self.update_encoding_stats_with_initial_hidden_state(initial_hx, layer)

                if reverse_pass_output:
                    _concatenate_output_with_reverse_pass_output(output, reverse_pass_output, self.hidden_size, steps,
                                                                 batches, inputs.device)

            # convert a list output tensors to a single tensor
            output = torch.stack(output)

            # if configured for more than one layer, the quantized output is fed back as input to next layer
            if self.num_layers > 1:
                inputs = output

        # if input is set to batch first, reformat to set batch back to first dim
        if self.batch_first:
            output = output.permute(1, 0, 2)

        output, stacked_hx = _reformat_output_and_stacked_hx_for_packed_sequence(output,
                                                                                 stacked_hx,
                                                                                 self.batch_first,
                                                                                 packed_sequence_info)
        hx = QcQuantizeRecurrent._format_hx_output(stacked_hx)

        return output, hx

    def _quantize_hidden_cell_state(self, layer_index: int, cell_hx: Union[torch.Tensor, Tuple[torch.Tensor]]) -> \
            Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        Quatizes hidden (and cell) state with the provided quantizers
        :param layer_index:  layer index
        :param cell_hx:  hidden (and cell) state tensor
        :return: quantized hidden (and cell) state tensor.
        """
        if isinstance(cell_hx, tuple):
            quantized_cell_hx = (self._quantize_activation(self._output_quantizers['h_l{}'.format(layer_index)], cell_hx[0]),
                                 self._quantize_activation(self._output_quantizers['c_l{}'.format(layer_index)], cell_hx[1]))
        else:
            quantized_cell_hx = self._quantize_activation(self._output_quantizers['h_l{}'.format(layer_index)], cell_hx)
        return quantized_cell_hx

    def _intialize_quantize_hidden_state(self, batches: int, inputs: torch.Tensor, layer: int, hx: torch.Tensor,
                                         permutation: Union[List[int], None]) -> \
            Tuple[bool, Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]]:
        """
        provides quantized hidden state for a layer either zero initialized or user provided.
        :param batches:  batch size of input Tensor
        :param inputs:  input Tensor
        :param layer: layer index
        :param hx: user provided hidden state
        :param permutation: ordering to sort the batches in h (no sorting is done if None)
        :return: a tuple - ( bool set to true if hx needs to be used for updating encoding stats and,
                            quantized or un-quantized hidden(,cell) state for the layer)
        """

        # the is_quantized flag is set to false if we need to skip updating of encoding stats to avoid initializing the
        # quantizer with potentially sub-optimal initial_hidden(cell) state tensor e.g. hx is zero filled etc.
        is_quantized = (self._mode != QcQuantizeOpMode.ANALYSIS or not self._reorder_initial_h_c_stats_update)
        if hx is None:
            zeros = torch.zeros(batches, self.hidden_size, dtype=inputs.dtype, device=inputs.device)
            h = (zeros, zeros) if self.mode == 'LSTM' else zeros
        else:
            if isinstance(hx, tuple):
                h = (hx[0][layer], hx[1][layer])
                if is_quantized:
                    h = (self._quantize_activation(self._input_quantizers['initial_h_l{}'.format(layer)], h[0]),
                         self._quantize_activation(self._input_quantizers['initial_c_l{}'.format(layer)], h[1]))
            else:
                assert self.mode != 'LSTM'
                h = hx[layer]
                if is_quantized:
                    h = self._quantize_activation(self._input_quantizers['initial_h_l{}'.format(layer)], h)
        if permutation is not None:
            if isinstance(h, tuple):
                h = (h[0].index_select(0, permutation), h[1].index_select(0, permutation))
            else:
                h = h.index_select(0, permutation)

        # update_initial_encoding_stats flag is set to true if updating stats for initial_h(_c) was skipped.
        # h tensor should be used to update encoding stats after all timestep computation.
        update_initial_hx_encoding_stats = (not is_quantized and hx is not None)
        return update_initial_hx_encoding_stats, h

    def flatten_parameters(self):
        """ In case models call flatten_parameters on the recurrent module, this will effectively serve as a no-op. """

    def update_encoding_stats_with_initial_hidden_state(self,
                                                        cell_hx: Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]],
                                                        layer_index: int):
        """ update encoding stats for initial hidden (and cell) state
        :param cell_hx:  hidden (and cell) state tensor
        :param layer_index:  layer index
        """
        if isinstance(cell_hx, tuple):
            self._input_quantizers['initial_h_l{}'.format(layer_index)].update_encoding_stats(cell_hx[0])
            self._input_quantizers['initial_c_l{}'.format(layer_index)].update_encoding_stats(cell_hx[1])
        else:
            self._input_quantizers['initial_h_l{}'.format(layer_index)].update_encoding_stats(cell_hx)


def _get_inputs_and_packed_sequence_info(inputs: Union[torch.Tensor, PackedSequence], batch_first: bool) -> \
        Tuple[torch.Tensor, Union[PackedSequenceInfo, None]]:
    """
    Given inputs which can either be a torch Tensor or a PackedSequence object, return inputs as a tensor as well as
    a PackedSequenceInfo object. In the case where inputs is already a tensor, it is passed through unchanged, and None
    is returned in place of the PackedSequenceInfo object
    :param inputs: Original input
    :param batch_first: True if batches is the first dimension, False otherwise
    :return: Input in tensor form as well as a PackedSequenceInfo object (None if inputs came in as a tensor)
    """
    packed_sequence_info = None
    if isinstance(inputs, PackedSequence):
        # Extract information contained in PackedSequence
        _, batch_sizes, unsorted_indices, sorted_indices = inputs

        # Unpack the PackedSequence to obtain inputs as a tensor
        inputs, sequence_lens = pad_packed_sequence(inputs, batch_first=batch_first)

        # Create the PackedSequenceInfo object to hold extracted information from the original input
        packed_sequence_info = PackedSequenceInfo(sequence_lens, batch_sizes, unsorted_indices, sorted_indices)

        # If PackedSequence had originally sorted the inputs, extracting them will give inputs back in unsorted format.
        # The batch sizes list above depends on the inputs being sorted, which we use in later computation, so we sort
        # the inputs here.
        if sorted_indices is not None:
            inputs = inputs.index_select(0 if batch_first else 1, unsorted_indices)
    return inputs, packed_sequence_info


def _get_flipped_input_for_reverse_pass(inputs: torch.Tensor, packed_sequence_info: PackedSequenceInfo,
                                        steps: int) -> torch.Tensor:
    """
    Flip the inputs for the bidirectional reverse pass. In case of PackedSequence, inputs with padded rows must be
    left shifted so the reversed inputs remain padded on the right.
    :param inputs: Inputs to flip
    :param packed_sequence_info: Object holding information about the original PackedSequence input
    :param steps: Number of timesteps
    :return: Torch tensor containing the flipped inputs
    """
    # Example showing how flipping is done with PackedSequence padding:
    # Original input (showing a batch of 3 inputs, with input lengths 5, 4, and 3):
    # 0 3 6 9  12
    # 1 4 7 10 X
    # 2 5 8 X  X
    # Instead of simply flipping the second row so it becomes X 1 4 7 10, we shift it as well so the pad remains on the
    # right.
    # Flipped input:
    # 12 9 6 3 0
    # 10 7 4 1 X
    # 8  5 2 X X
    inputs = torch.flip(inputs, [0])
    if packed_sequence_info:
        sorted_lens = packed_sequence_info.sorted_sequence_lens
        for i, sequence_length in enumerate(sorted_lens):
            if sequence_length < steps:
                inputs[:, i][:sequence_length] = \
                    inputs[:, i][steps - sequence_length:]
    return inputs


def _replace_appropriate_hidden_state_rows(hidden_state: Union[Tuple[torch.Tensor, torch.Tensor]],
                                           new_hidden_state: Union[Tuple[torch.Tensor, torch.Tensor], torch.Tensor],
                                           packed_sequence_info: PackedSequenceInfo, iteration: int,
                                           num_batches: int) -> Union[Tuple[torch.Tensor, torch.Tensor]]:
    """
    Given the original hidden state and the newly calculated hidden state, update the original hidden state with only
    rows from the new hidden state that correspond to valid outputs. This is mainly only a concern in the case of
    PackedSequence with padded inputs. Otherwise, we simply replace the old hidden state with the new one entirely.
    :param hidden_state: Original hidden state to replace rows of
    :param new_hidden_state: New hidden state
    :param packed_sequence_info: Object holding information about the original PackedSequence input
    :param iteration: Current iteration count
    :param num_batches: Number of batches
    :return: Hidden state with updated rows
    """
    if packed_sequence_info:
        # In the case of PackedSequence, certain inputs in the batch need to be ignored, depending on
        # sequence length for that input and which timestep we are in.
        # In our implementation, we still feed the full batch into the rnn_impl_map function, but
        # instead of replacing all rows of cell_hx (each row corresponds to an output for an item in the
        # batch), we replace only rows which correspond to valid batch inputs. This is the same as how
        # hx behaves in actual Pytorch implementation when using PackedSequence.
        current_batch_size = packed_sequence_info.batch_sizes[iteration]
        if current_batch_size == num_batches:
            # All items in the input batch are valid, so we can replace the entire hidden state.
            hidden_state = new_hidden_state
        else:
            # Not all items in the input batch are valid. Replace the first number of rows in the hidden
            # state corresponding to the number of valid items, and keep the remaining rows unchanged.
            if isinstance(hidden_state, tuple):
                hidden_state = (torch.cat((new_hidden_state[0][:current_batch_size - num_batches],
                                           hidden_state[0][current_batch_size - num_batches:])),
                                torch.cat((new_hidden_state[1][:current_batch_size - num_batches],
                                           hidden_state[1][current_batch_size - num_batches:])))
            else:
                hidden_state = torch.cat((new_hidden_state[:current_batch_size - num_batches],
                                          hidden_state[current_batch_size - num_batches:]))
    else:
        hidden_state = new_hidden_state
    return hidden_state


def _fill_appropriate_rows_in_reverse_pass_output(reverse_pass_output,
                                                  packed_sequence_info,
                                                  steps,
                                                  num_batches,
                                                  iteration,
                                                  hidden_state):
    """
    Given the output from one batch pass during the reverse pass of bidirectional, update the reverse_pass_output array
    in place, inserting the output in the correct row to correspond with the output from the forward pass.
    reverse_pass_output is modified in place.
    :param reverse_pass_output: Rows of outputs obtained from the reverse pass. Each row is the result of the output of
    one timestep for one element in a batch. Total number of rows is steps * num_batches. The first num_batches of rows
    corresponds to the output obtained from running one timestep through the reverse pass. The second num_batches of
    rows corresponds to the output obtained from running the second timestep, and so on.
    :param packed_sequence_info: Object holding information about the original PackedSequence input
    :param steps: Number of timesteps
    :param num_batches: Number of batches
    :param iteration: Current iteration count
    :param hidden_state: Hidden state containing elements to write into reverse_pass_output
    """
    # For a concrete example, assume we have the following inputs, where X denotes a padded, invalid input (this is the
    # case when PackedSequence is involved. For regular inputs, padded portions are considered valid). Sequence length
    # is 5, and number of batches is 3, 3, 3, 2, and 1 for corresponding timesteps:
    # 0 3 6 9  12
    # 1 4 7 10 X
    # 2 5 8 X  X
    #
    # The forward pass will produce outputs 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, (11), 12, (13), (14), where parentheses
    # denotes outputs to ignore.
    # In the backward pass, due to the behavior of PackedSequence, when reversing the inputs we get the following:
    # 12 9 6 3 0
    # 10 7 4 1 X
    # 8  5 2 X X
    # Note how the second and third sequences are still padded on the right. Then, the reverse pass would produce
    # outputs 12, 10, 8, 9, 7, 5, 6, 4, 2, 3, 1, X, 0, X, X
    # I have numbered the inputs and outputs in such a way that the above outputs are numbered the same index we would
    # need to insert them into reverse_pass_output. Thus it is easy to follow the math below and understand how the
    # calculations are done.
    for i in range(num_batches):
        sequence_length = steps
        if packed_sequence_info:
            sequence_length = packed_sequence_info.sorted_sequence_lens[i]
        # The correct index to write to can be found by first identifying the last set of batches
        # num_batches * (steps - 1), subtracting num_batches a number of times corresponding to which iteration we are
        # on, and then subtracting num_batches a further number of times if the sequence length for the input i we are
        # on is less than the maximum sequence length.
        reverse_pass_output_index = num_batches * (steps - 1) - \
                                    num_batches * iteration - \
                                    num_batches * (steps - sequence_length) + i
        output_to_append = hidden_state[0][i] if isinstance(hidden_state, tuple) else hidden_state[i]
        if reverse_pass_output_index >= 0:
            reverse_pass_output[reverse_pass_output_index] = output_to_append


def _concatenate_output_with_reverse_pass_output(output, reverse_pass_output, hidden_size, steps, num_batches, device):
    """
    Concatenates outputs from the reverse pass with outputs from the original pass in the case of bidirectional, in
    place
    :param output: Output from original pass to concatenate with
    :param reverse_pass_output: Output from reverse pass to concatenate with
    :param hidden_size: Dimension size of the hidden feature
    :param steps: Number of timesteps
    :param num_batches: Number of batches
    """
    reverse_pass_batches = []
    for i, output_tensor in enumerate(reverse_pass_output):
        if output_tensor is None:
            reverse_pass_output[i] = torch.rand(hidden_size).to(device)
    # Group reverse_pass_output into batches
    for i in range(steps):
        reverse_pass_batches.append(
            torch.stack(reverse_pass_output[(i * num_batches):((i + 1) * num_batches)]))
    for i, batch_output in enumerate(reverse_pass_batches):
        output[i] = torch.cat((output[i], batch_output), 1)


def _reformat_output_and_stacked_hx_for_packed_sequence(output: torch.Tensor,
                                                        stacked_hx: List[Union[Tuple[torch.Tensor, torch.Tensor],
                                                                               torch.Tensor]],
                                                        batch_first: bool,
                                                        packed_sequence_info: PackedSequenceInfo) \
        -> Tuple[Union[torch.Tensor, PackedSequence], List[Union[Tuple[torch.Tensor, torch.Tensor], torch.Tensor]]]:
    """
    If PackedSequence is used as input, ensure that output tensors and hx tensors are arranged in the correct ordering,
    and pack output into a PackedSequence object.
    :param output: Output tensor to rearrange and pack
    :param stacked_hx: List of hidden states
    :param batch_first: True if batch is the first dimension, False otherwise
    :param packed_sequence_info: Object holding information about the original PackedSequence input
    :return: Output and stacked hx in the corresponding expected form depending on whether PackedSequence was used as
    input or not
    """
    if not packed_sequence_info:
        return output, stacked_hx

    if packed_sequence_info.sorted_indices is not None:
        output = output.index_select(0 if batch_first else 1, packed_sequence_info.sorted_indices)
        for i, hx_tensor in enumerate(stacked_hx):
            if isinstance(hx_tensor, tuple):
                stacked_hx[i] = (hx_tensor[0].index_select(0, packed_sequence_info.sorted_indices),
                                 hx_tensor[1].index_select(0, packed_sequence_info.sorted_indices))
            else:
                stacked_hx[i] = hx_tensor.index_select(0, packed_sequence_info.sorted_indices)

    output = pack_padded_sequence(input=output,
                                  lengths=packed_sequence_info.sequence_lens,
                                  batch_first=batch_first,
                                  enforce_sorted=False)
    return output, stacked_hx
