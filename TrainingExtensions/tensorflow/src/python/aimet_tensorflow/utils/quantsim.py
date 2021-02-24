# /usr/bin/env python3.5
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
""" utilities for quantsim """

from typing import List

import tensorflow as tf

from aimet_common.utils import AimetLogger
from aimet_tensorflow.common.connectedgraph import ConnectedGraph
from aimet_tensorflow.common.operation import Op
from aimet_tensorflow.common import core
from aimet_tensorflow.quantsim_config.quantsim_config import OpToQuantOpsDictType

_logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)

DTYPES_QUANTIZE_NOT_REQUIRED = [tf.dtypes.int8, tf.dtypes.uint8, tf.dtypes.int16, tf.dtypes.uint16,
                                tf.dtypes.int32, tf.dtypes.uint32, tf.dtypes.int64, tf.dtypes.uint64,
                                tf.bool, tf.dtypes.string, tf.dtypes.resource]


def get_param_quantizer(op: tf.Operation, index: int) -> tf.Operation:
    """
    utility to get param quantizer inserted for given op
    :param op: TensorFlow operation
    :param index: input index to get param from
    :return: AIMET Quantizer op associated with the param
    """

    # MatMul param is directly connected input node, get quantized readVarOp
    quantized_op = op.inputs[index].op
    # handle MatMuls where the param is fed via strided_slice or split op types
    if op.inputs[index].op.type in ['StridedSlice', 'Split']:
        matmul_input_op = op.inputs[index].op
        # get quantized readVarOp
        for inp in matmul_input_op.inputs:
            if inp.op.type in ['QcQuantize', 'QcQuantizeRecurrentParam']:
                quantized_op = inp.op

    return quantized_op


def create_op_to_quant_ops_dict(graph: tf.Graph, conn_graph: ConnectedGraph, ops_with_param_names: List[str],
                                indices: List[int], activation_op_names: List[str]) -> OpToQuantOpsDictType:
    """
    Create an op to quant ops dictionary mapping connected graph ops to a list consisting of the activation quantizer
    and a dictionary mapping param type string to param quantizers.
    :param graph: Tensorflow graph containing inserted quantizers
    :param conn_graph: Connected graph of the original unquantized model
    :param ops_with_param_names: List of tf operation names for which parameter quantizers were inserted for
    :param indices: Indices of tf operations of which parameter quantizers were inserted for
    :param activation_op_names: List of tf operation names for which activation quantizers were inserted for
    :return: Dictionary mapping connected graph ops to a list consisting of the activation quantizer and a dictionary
    mapping param type string to param quantizers.
    """

    op_to_quant_ops_dict = {}
    for op_with_param_name, index in zip(ops_with_param_names, indices):
        op_with_param = graph.get_operation_by_name(op_with_param_name)
        conn_graph_op = conn_graph.get_op_from_module_name(op_with_param_name)
        param_type = 'weight'
        if op_with_param.type == 'BiasAdd':
            param_type = 'bias'
        param_quantizer = get_param_quantizer(op_with_param, index)
        assert param_quantizer.type in ['QcQuantize', 'QcQuantizeRecurrentParam']
        add_op_to_quant_ops_dict_entry(param_quantizer, conn_graph_op, True, param_type, op_to_quant_ops_dict)
    for activation_op_name in activation_op_names:
        activation_op = graph.get_operation_by_name(activation_op_name)
        conn_graph_op = conn_graph.get_op_from_module_name(activation_op_name)
        activation_quantizer = \
            [consumer for consumer in activation_op.outputs[0].consumers() if consumer.type == 'QcQuantize']
        if len(activation_quantizer) != 1:
            _logger.error('Expected one activation quantizer but found %s', len(activation_quantizer))
            raise AssertionError
        add_op_to_quant_ops_dict_entry(activation_quantizer[0], conn_graph_op, False, '', op_to_quant_ops_dict)
    return op_to_quant_ops_dict


def add_op_to_quant_ops_dict_entry(qc_quantize_op: tf.Operation, conn_graph_op: Op, is_param: bool, param_type: str,
                                   op_to_quant_ops_dict: OpToQuantOpsDictType):
    """
    Add an entry to the op_to_quant_ops_dict
    :param qc_quantize_op: Qc quantize op to add to the dictionary
    :param conn_graph_op: Connected graph Op associated with the qc quantize op
    :param is_param: True if the qc quantize op was created for a parameter, False otherwise
    :param param_type: Type of parameter (unused for activation quantizers)
    :param op_to_quant_ops_dict: Dictionary mapping connected graph op to a two item list consisting of a dictionary
    of param types to param qc quantize ops, and activation qc quantize op
    """
    if is_param:
        if conn_graph_op in op_to_quant_ops_dict:
            param_quant_op_dict, _ = op_to_quant_ops_dict[conn_graph_op]
            if param_type in param_quant_op_dict:
                param_quant_op_dict[param_type].add(qc_quantize_op)
            else:
                param_quant_op_dict[param_type] = {qc_quantize_op}
        else:
            param_quant_op_dict = {param_type: {qc_quantize_op}}
            op_to_quant_ops_dict[conn_graph_op] = [param_quant_op_dict, None]
    else:
        if conn_graph_op in op_to_quant_ops_dict:
            op_to_quant_ops_dict[conn_graph_op][1] = qc_quantize_op
        else:
            op_to_quant_ops_dict[conn_graph_op] = [dict(), qc_quantize_op]


def get_op_input_indices(graph: tf.Graph, ops_with_param_names: List) -> List[int]:
    """
    Get input indices of ops
    :param graph: Tensorflow graph as tf.Graph
    :param ops_with_param_names: List of op names with params to insert quantize ops for
    :return: list of indices of parameters for each op
    """

    query = core.OpQuery(graph, ops_to_ignore=None)
    ops_with_params = [graph.get_operation_by_name(op_name) for op_name in ops_with_param_names]
    input_indices = query.get_weight_inputs(ops_with_params)
    if len(ops_with_param_names) != len(input_indices):
        _logger.error("Length of ops with params and input indices differ")
        raise AssertionError
    return input_indices


def is_op_quantizable(op: tf.Operation) -> bool:
    """
    utility to check if the quantization can be supported for this op
    :param op: op as tf.Operation type
    :return: True if the op can be quantized, False otherwise
    """

    if op.outputs:
        if op.outputs[0].dtype not in DTYPES_QUANTIZE_NOT_REQUIRED:
            return True

    return False


def get_time_steps_tensor_from_rnn_inner_ops(inner_ops: List[tf.Operation]) -> tf.Tensor:
    """
    returns the time steps tensor from RNN inner op list
    :param inner_ops: list of tf.Operations inside a RNN op
    :return: tf.Tensor corresponding to time_steps param
    """
    for op in inner_ops:
        if op.type in ['LoopCond']:
            tf_less_op = op.inputs[0].op
            assert tf_less_op.type == 'Less'
            less_enter_op = tf_less_op.inputs[1].op
            assert less_enter_op.type == 'Enter'
            time_steps_tensor = less_enter_op.inputs[0]

    return time_steps_tensor
