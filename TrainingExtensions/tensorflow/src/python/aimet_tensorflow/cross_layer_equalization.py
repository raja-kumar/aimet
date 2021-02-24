# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2019, Qualcomm Innovation Center, Inc. All rights reserved.
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

""" Auto Mode TF Cross Layer Equalization """

from typing import Tuple, List, Union, Dict
import numpy as np
import tensorflow as tf
import libpymo

from aimet_tensorflow.common.connectedgraph import ConnectedGraph
from aimet_tensorflow.common.operation import Op
from aimet_tensorflow.batch_norm_fold import fold_all_batch_norms
from aimet_tensorflow.utils.graph_saver import save_and_load_graph
from aimet_tensorflow.utils.op.conv import WeightTensorUtils, BiasUtils
import aimet_tensorflow.utils.op.relu as ReluUtils
from aimet_tensorflow.utils.op.fusedbatchnorm import BNUtils
from aimet_common.utils import AimetLogger

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.CrosslayerEqualization)

ScaleFactor = Union[np.ndarray, Tuple[np.ndarray]]

ClsSet = Union[Tuple[tf.Operation, tf.Operation],
               Tuple[tf.Operation, tf.Operation, tf.Operation]]


class GraphSearchUtils:

    """ Implements graph search utils required by CLE feature"""

    def __init__(self, model: tf.Graph, start_op_names: Union[str, List[str]], output_op_names: Union[str, List[str]]):
        if isinstance(start_op_names, str):
            start_op_names = [start_op_names]

        if isinstance(output_op_names, str):
            output_op_names = [output_op_names]

        self._connected_graph = ConnectedGraph(model, start_op_names, output_op_names)

    def find_and_replace_relu6_with_relu(self, sess: tf.compat.v1.Session) -> tf.compat.v1.Session:
        """
        finds and replaces Relu6 ops with Relu
        :return: updated session
        """
        for op in self._connected_graph.get_all_ops().values():
            if op.type in ['Relu6']:
                # send the session here, so we make the update on sess.graph (active graph)
                ReluUtils.replace_relu6_with_relu(sess, op.get_module())

        # in the end update the session
        after_relu_replace_sess = save_and_load_graph('./replace_relu6_with_relu', sess)

        return after_relu_replace_sess

    @staticmethod
    def find_downstream_layer_groups_to_scale(op, layer_groups, visited_nodes, current_group=None):
        """
        Populates all the layer groups eligible for cross layer scaling
        :param op: starting  op
        :param layer_groups: layer_groups as empty list
        :param visited_nodes: all the ops that have been visited
        :param current_group: op groups
        :return: None. Updates layer_groups[] if groups are found.
        """

        if not current_group:
            current_group = []

        if op in visited_nodes:
            return

        visited_nodes.append(op)
        logger.debug("Visiting node: {%s}", op.dotted_name)

        # If current node is Conv2D, add to the current group
        if op.type in ['Conv2D', 'DepthwiseConv2dNative']:
            current_group.append(op)

        # Terminating condition for current group
        if not (op.type in ['Conv2D', 'DepthwiseConv2dNative', 'Relu', 'PReLU', 'Pad', 'Identity']):
            if (len(current_group) > 1) and (current_group not in layer_groups):
                layer_groups.append(current_group)
                node_set = [op.dotted_name for op in current_group]
                logger.debug("Added new set of nodes: {%s}", node_set)
            current_group = []

        if op.output:
            for consumer in op.output.consumers:
                GraphSearchUtils.find_downstream_layer_groups_to_scale(consumer, layer_groups, visited_nodes,
                                                                       current_group)

        # Reached a leaf.. See if the current group has something to grab
        if (len(current_group) > 1) and (current_group not in layer_groups):
            layer_groups.append(current_group)
            node_set = [op.dotted_name for op in current_group]
            logger.debug("Added new set of nodes: {%s}", node_set)

    def find_layer_groups_to_scale_as_conn_ops(self) -> List[List[Op]]:
        """
        :return: List of groups of layers. Each group can be independently equalized
        """

        # Find the input node(s) in the graph
        input_nodes = []
        for op in self._connected_graph.get_all_ops().values():
            if op.inputs and op.inputs[0].is_model_input:
                input_nodes.append(op)

        layer_groups = []
        visited_nodes = []

        for op in input_nodes:
            self.find_downstream_layer_groups_to_scale(op=op, layer_groups=layer_groups,
                                                       visited_nodes=visited_nodes)

        return layer_groups

    def find_layer_groups_to_scale(self):
        """
        Find layer groups for scaling as tf ops
        :return: groups for scaling as tf ops
        """

        layer_groups_as_conn_graph_ops = self.find_layer_groups_to_scale_as_conn_ops()
        layer_groups_as_tf_ops, tf_op_to_conn_graph_op_map = self.convert_conn_graph_ops_to_tf_op(layer_groups_as_conn_graph_ops)

        return tf_op_to_conn_graph_op_map, layer_groups_as_tf_ops

    @staticmethod
    def convert_conn_graph_ops_to_tf_op(op_groups: List[List[Op]]) -> \
            List[List[tf.Operation]]:
        """
         Helper function to get op list as tf.Operation type to be usable for updating/scaling weights and biases
         using generic apis for tensor updates.
        :param op_groups: list of op groups as TfOperation type of used by Connected Graph
        :return: lis of op groups as tf.Operation  (standard TF op type)
        """
        tf_op_to_conn_graph_op_map = {}
        layer_groups_as_tf_ops = []
        for ops in op_groups:
            curr_group = []
            for op in ops:
                tf_op_to_conn_graph_op_map[op.get_module()] = op
                curr_group.append(op.get_module())
            layer_groups_as_tf_ops.append(curr_group)

        return layer_groups_as_tf_ops, tf_op_to_conn_graph_op_map

    @staticmethod
    def convert_layer_group_to_cls_sets(layer_group):
        """
        Helper function to convert a layer group to a list of cls sets
        :param layer_group: Given layer group to convert
        :return: List of cls sets
        """
        cls_sets = []

        prev_layer_to_scale = layer_group.pop(0)
        while layer_group:
            next_layer_to_scale = layer_group.pop(0)

            if next_layer_to_scale.type in ['DepthwiseConv2dNative']:
                next_non_depthwise_conv_layer = layer_group.pop(0)
                cls_sets.append((prev_layer_to_scale, next_layer_to_scale, next_non_depthwise_conv_layer))
                prev_layer_to_scale = next_non_depthwise_conv_layer

            else:
                cls_sets.append((prev_layer_to_scale, next_layer_to_scale))
                prev_layer_to_scale = next_layer_to_scale

        return cls_sets

    @staticmethod
    def is_relu_activation_present_in_cls_sets(cls_sets: List[ClsSet],
                                               tf_op_to_conn_graph_op_map: Dict) -> List[bool]:
        """
        check if there is Relu activations between cls sets
        :param cls_sets: cls conv op pairs
        :param tf_op_to_conn_graph_op_map: Map of tf-op => connected graph op
        :return: list of relu activation preset flags(True or False)
        corresponding to input cls_sets list
        """
        is_relu_activation_in_cls_sets = []
        for cls_set in cls_sets:
            # We need to check activation functions for all layers but the last one in the set
            # Because we are only interested in checking activation functions between the layers we will scale
            cls_set = cls_set[:-1]

            is_relu_activation_in_cls_set = ()
            for conv_op in cls_set:
                conn_graph_conv_op = tf_op_to_conn_graph_op_map[conv_op]
                is_relu_activation_in_cls_set += (ReluUtils.does_conv_have_relu_activation(conn_graph_conv_op), )

            if len(is_relu_activation_in_cls_set) == 1:
                is_relu_activation_in_cls_set = is_relu_activation_in_cls_set[0]

            is_relu_activation_in_cls_sets.append(is_relu_activation_in_cls_set)

        return is_relu_activation_in_cls_sets

    @staticmethod
    def map_op_names_to_ops(sess: tf.compat.v1.Session) -> Dict[str, tf.Operation]:
        """
        After the fold and cls , the graph is updated, so are the ops
        So, we need a way to map ops we stored on graph we began with, to perform
        high bias fold operation on latest ops in the updated graph.
        :param sess: active tf.compat.v1.Session (tf.compat.v1.Session type)
        :return: a dictionary of op names mapped to ops in the given new session.
        Note : only stores infor pertaining to bn and conv ops required by high bias fold.
        """

        tf_names_op_dict = {}
        with sess.graph.as_default():
            op_list = sess.graph.get_operations()
            for op in op_list:
                if op.type in ['Conv2D', 'DepthwiseConv2dNative', 'FusedBatchNormV3']:
                    tf_names_op_dict[op.name] = op

        return tf_names_op_dict


class ClsSetInfo:
    """
    This class hold information about the layers in a CLS set, along with corresponding scaling factors
    for CLS set layers
    """

    class ClsSetLayerPairInfo:
        """
         Models a pair of layers that were scaled using CLS. And related information.

        :param layer1: layer as tf.Operation
        :param layer2: layer as tf.Operation
        :param scale_factor: scale factors as np.ndarray
        :param relu_activation_between_layers: list of flags per layer set indicating\
        if they have Relu activations in-between.

        """
        def __init__(self, layer1: tf.Operation, layer2: tf.Operation,
                     scale_factor: np.ndarray, relu_activation_between_layers):

            self.layer1 = layer1
            self.layer2 = layer2
            self.scale_factor = scale_factor
            self.relu_activation_between_layers = relu_activation_between_layers

    def __init__(self, cls_pair_1: ClsSetLayerPairInfo, cls_pair_2: ClsSetLayerPairInfo = None):
        if cls_pair_2:
            self.cls_pair_info_list = [cls_pair_1, cls_pair_2]
        else:
            self.cls_pair_info_list = [cls_pair_1]

    @staticmethod
    def map_cls_sets_to_new_session(tf_names_op_dict: Dict[str, tf.Operation], cls_set_info_list):
        """
         Helper function to updates ops stored during cls to be used by high bias fold with updated session.

        :param tf_names_op_dict:  map of tf op names to ops
        :param cls_set_info_list: list of ClsSetInfo type
        :return: None /cls_set_info_list updated in-place

        """
        for cls_set_info in cls_set_info_list:
            for cls_pair_info in cls_set_info.cls_pair_info_list:
                # refresh the ops, so we can perform high bias fold with info saved during cls.
                cls_pair_info.layer1 = tf_names_op_dict[cls_pair_info.layer1.name]
                cls_pair_info.layer2 = tf_names_op_dict[cls_pair_info.layer2.name]


class CrossLayerScaling:
    """ implements auto mode cross-layer-scaling technique to a model """

    @staticmethod
    def scale_cls_sets(sess: tf.compat.v1.Session, cls_sets: List[ClsSet]) -> List[ScaleFactor]:

        """
        Scale multiple CLS sets

        :param sess: Current session
        :param cls_sets: List of CLS sets
        :return: Scaling factors calculated and applied for each CLS set in order

        """
        scale_factor_list = []
        for cls_set in cls_sets:
            scale_factor = CrossLayerScaling.scale_cls_set(sess, cls_set)
            scale_factor_list.append(scale_factor)
        return scale_factor_list

    @staticmethod
    def scale_cls_set(sess: tf.compat.v1.Session, cls_set: ClsSet) -> ScaleFactor:
        """
        Scale a CLS set
        :param sess: Current session
        :param cls_set: Either a pair or regular conv layers or a triplet of depthwise separable layers
        :return: Scaling factor calculated and applied
        """

        if len(cls_set) == 3:
            scale_factor = CrossLayerScaling.scale_cls_set_with_depthwise_layers(sess, cls_set)
        else:
            scale_factor = CrossLayerScaling.scale_cls_set_with_conv_layers(sess, cls_set)

        return scale_factor

    @staticmethod
    def scale_cls_set_with_conv_layers(model: tf.compat.v1.Session, cls_set: Tuple[tf.Operation, tf.Operation]) -> np.ndarray:
        """
        API to invoke equalize layer params (update for weights and bias is in place)
        :param model: active tf.compat.v1.Session
        :param cls_set: Consecutive Conv layers Tuple whose weights and biases need to be equalized
        :return: Scaling factor S_12 for each conv layer pair: numpy array
        """

        with model.graph.as_default():
            for module in cls_set:
                if module.type not in ['Conv2D', 'DepthwiseConv2dNative']:
                    raise ValueError("Only conv layers are supported for cross layer equalization")

            # Create structs for holding layer weights and bias parameters
            prev_layer_params = libpymo.EqualizationParams()
            curr_layer_params = libpymo.EqualizationParams()

            # send as [Noc, Nic, kh, kw],  TF format is [kh, kw, Nic, Noc]
            prev_layer_params.weight = WeightTensorUtils.get_tensor_as_numpy_data(model, cls_set[0]). \
                transpose((3, 2, 0, 1)).reshape(-1)
            weight_shape = WeightTensorUtils.get_tensor_shape(cls_set[0])
            prev_layer_params.weightShape = [weight_shape[3], weight_shape[2], weight_shape[0], weight_shape[1]]
            prev_layer_params.isBiasNone = BiasUtils.is_bias_none(cls_set[0])

            # send as [Noc, Nic, kh, kw],  TF format is [kh, kw, Nic, Noc]
            curr_layer_params.weight = WeightTensorUtils.get_tensor_as_numpy_data(model, cls_set[1]). \
                transpose((3, 2, 0, 1)).reshape(-1)
            weight_shape = WeightTensorUtils.get_tensor_shape(cls_set[1])
            curr_layer_params.weightShape = [weight_shape[3], weight_shape[2], weight_shape[0], weight_shape[1]]

            if not BiasUtils.is_bias_none(cls_set[0]):
                prev_layer_params.bias = BiasUtils.get_bias_as_numpy_data(model, cls_set[0]).reshape(-1)
            else:
                prev_layer_params.isBiasNone = True

            scaling_factor = libpymo.scaleLayerParams(prev_layer_params, curr_layer_params)

            # convert received formats back to TF
            # TF format is [kh, kw, Nic, Noc]
            numpy_weight_reshaped = np.reshape(prev_layer_params.weight, prev_layer_params.weightShape). \
                transpose((2, 3, 1, 0))
            WeightTensorUtils.update_tensor_for_op(model, cls_set[0], numpy_weight_reshaped)

            numpy_weight_reshaped = np.reshape(curr_layer_params.weight, curr_layer_params.weightShape). \
                transpose((2, 3, 1, 0))
            WeightTensorUtils.update_tensor_for_op(model, cls_set[1], numpy_weight_reshaped)

            if not BiasUtils.is_bias_none(cls_set[0]):
                numpy_bias_reshaped = np.reshape(prev_layer_params.bias, BiasUtils.get_shape(cls_set[0]))
                BiasUtils.update_bias_for_op(model, cls_set[0], numpy_bias_reshaped)

        return scaling_factor

    @staticmethod
    def scale_cls_set_with_depthwise_layers(model: tf.compat.v1.Session,
                                            cls_set: Tuple[tf.Operation,
                                                           tf.Operation,
                                                           tf.Operation]) -> [np.ndarray, np.ndarray]:
        """
        API to invoke equalize layer params for depth wise separable layers(update for weights and bias is in place)
        :param model: active tf.compat.v1.Session
        :param cls_set: Consecutive Conv layers whose weights and biases need to be equalized.
                        Second Conv layer is a depth-wise conv and third conv layer is point-wise conv
        :return: Scaling factors S_12 and S_23 : numpy arrays
        """

        # make sure you define the session and graph scope before making any graph updates.
        with model.graph.as_default():
            for module in cls_set:
                if module.type not in ['Conv2D', 'DepthwiseConv2dNative']:
                    raise ValueError("Only conv layers are supported for cross layer equalization")

            # Create structs for holding layer weights and bias parameters
            prev_layer_params = libpymo.EqualizationParams()
            curr_layer_params = libpymo.EqualizationParams()
            next_layer_params = libpymo.EqualizationParams()

            # send as [Noc, Nic, kh, kw],  TF format is [kh, kw, Nic, Noc]
            prev_layer_params.weight = WeightTensorUtils.get_tensor_as_numpy_data(model, cls_set[0]). \
                transpose((3, 2, 0, 1)).reshape(-1)
            weight_shape = WeightTensorUtils.get_tensor_shape(cls_set[0])
            prev_layer_params.weightShape = [weight_shape[3], weight_shape[2], weight_shape[0], weight_shape[1]]
            prev_layer_params.isBiasNone = BiasUtils.is_bias_none(cls_set[0])

            # depthwise layer outputs is set to 1 in TF
            # send as [Nic, Noc, kh, kw],  TF format is [kh, kw, Nic, Noc]
            curr_layer_params.weight = WeightTensorUtils.get_tensor_as_numpy_data(model, cls_set[1]). \
                transpose((2, 3, 0, 1)).reshape(-1)
            weight_shape = WeightTensorUtils.get_tensor_shape(cls_set[1])

            # depthwise layer outputs is set to 1 in TF
            # send as [Nic, Noc, kh, kw],  TF format is [kh, kw, Nic, Noc]
            curr_layer_params.weightShape = [weight_shape[2], weight_shape[3], weight_shape[0], weight_shape[1]]
            curr_layer_params.isBiasNone = BiasUtils.is_bias_none(cls_set[1])

            # send as [Noc, Nic, kh, kw] , TF format is [kh, kw, Nic, Noc]
            next_layer_params.weight = WeightTensorUtils.get_tensor_as_numpy_data(model, cls_set[2]). \
                transpose((3, 2, 0, 1)).reshape(-1)
            weight_shape = WeightTensorUtils.get_tensor_shape(cls_set[2])
            next_layer_params.weightShape = [weight_shape[3], weight_shape[2], weight_shape[0], weight_shape[1]]

            if not BiasUtils.is_bias_none(cls_set[0]):
                prev_layer_params.bias = BiasUtils.get_bias_as_numpy_data(model, cls_set[0]).reshape(-1)
            else:
                prev_layer_params.isBiasNone = True

            if not BiasUtils.is_bias_none(cls_set[1]):
                curr_layer_params.bias = BiasUtils.get_bias_as_numpy_data(model, cls_set[1]).reshape(-1)
            else:
                curr_layer_params.isBiasNone = True

            scaling_params = libpymo.scaleDepthWiseSeparableLayer(prev_layer_params, curr_layer_params,
                                                                  next_layer_params)

            # convert received formats back to TF
            # TF format is [kh, kw, Nic, Noc]
            numpy_weight_reshaped_0 = np.reshape(prev_layer_params.weight, prev_layer_params.weightShape). \
                transpose((2, 3, 1, 0))
            WeightTensorUtils.update_tensor_for_op(model, cls_set[0], numpy_weight_reshaped_0)

            # depthwise layer
            numpy_weight_reshaped_1 = np.reshape(curr_layer_params.weight, curr_layer_params.weightShape). \
                transpose((2, 3, 0, 1))
            WeightTensorUtils.update_tensor_for_op(model, cls_set[1], numpy_weight_reshaped_1)

            numpy_weight_reshaped_2 = np.reshape(next_layer_params.weight, next_layer_params.weightShape). \
                transpose((2, 3, 1, 0))
            WeightTensorUtils.update_tensor_for_op(model, cls_set[2], numpy_weight_reshaped_2)

            if not BiasUtils.is_bias_none(cls_set[0]):
                numpy_bias_reshaped = np.reshape(prev_layer_params.bias, BiasUtils.get_shape(cls_set[0]))
                BiasUtils.update_bias_for_op(model, cls_set[0], numpy_bias_reshaped)

            if not BiasUtils.is_bias_none(cls_set[1]):
                numpy_bias_reshaped = np.reshape(curr_layer_params.bias, BiasUtils.get_shape(cls_set[1]))
                BiasUtils.update_bias_for_op(model, cls_set[1], numpy_bias_reshaped)

        return scaling_params.scalingMatrix12, scaling_params.scalingMatrix23

    @staticmethod
    def create_cls_set_info_list(cls_sets: List[ClsSet], scale_factors: List[ScaleFactor],
                                 is_relu_activation_in_cls_sets):
        """
        Binds information from there separate lists into one [ClsInfoSet] data-structure

        :param cls_sets: List of CLS sets
        :param scale_factors: Scale-factors for each cls-set
        :param is_relu_activation_in_cls_sets: Information if there is relu activation in each cls-set
        :return: List of ClsSetInfo
        """
        cls_set_info_list = []
        assert len(cls_sets) == len(scale_factors) == len(is_relu_activation_in_cls_sets)

        for index, cls_set in enumerate(cls_sets):

            if isinstance(scale_factors[index], tuple):
                # If we are dealing with a triplet of layers, then we should have 2 scale factors and 2 relu flags
                # Assert that this is true
                assert len(cls_set) == 3
                assert len(scale_factors[index]) == len(is_relu_activation_in_cls_sets[index]) == 2

                cls_pair_1 = ClsSetInfo.ClsSetLayerPairInfo(cls_set[0], cls_set[1], scale_factors[index][0],
                                                            is_relu_activation_in_cls_sets[index][0])
                cls_pair_2 = ClsSetInfo.ClsSetLayerPairInfo(cls_set[1], cls_set[2], scale_factors[index][1],
                                                            is_relu_activation_in_cls_sets[index][1])
                cls_set_info = ClsSetInfo(cls_pair_1, cls_pair_2)

            else:
                cls_pair = ClsSetInfo.ClsSetLayerPairInfo(cls_set[0], cls_set[1], scale_factors[index],
                                                          is_relu_activation_in_cls_sets[index])
                cls_set_info = ClsSetInfo(cls_pair)

            cls_set_info_list.append(cls_set_info)

        return cls_set_info_list

    @staticmethod
    def scale_model(sess: tf.compat.v1.Session, input_op_names: Union[str, List[str]], output_op_names: Union[str, List[str]])\
            -> (tf.compat.v1.Session, List[ClsSetInfo]):
        """
        Uses cross-layer scaling to scale all applicable layers in the given model

        :param sess: Session containing graph to scale
        :param input_op_names: Names of starting ops in the model
        :param output_op_names: List of output op names of the model, used to help ConnectedGraph determine valid ops
               (to ignore training ops for example).  If None, all ops in the model are considered valid.
        :return: updated session, CLS information for each CLS set

        """

        if isinstance(input_op_names, str):
            input_op_names = [input_op_names]

        if isinstance(output_op_names, str):
            output_op_names = [output_op_names]

        # Find layer groups
        graph_search = GraphSearchUtils(sess.graph, input_op_names, output_op_names)
        tf_op_to_conn_graph_op_map, layer_groups_as_tf_ops = graph_search.find_layer_groups_to_scale()

        # Find cls sets from the layer groups
        cls_sets = []
        for layer_group in layer_groups_as_tf_ops:
            cls_set = graph_search.convert_layer_group_to_cls_sets(layer_group)
            cls_sets += cls_set

        # Scale the CLS sets
        scale_factors = CrossLayerScaling.scale_cls_sets(sess, cls_sets)

        # Find if there were relu activations between layers of each cls set
        is_relu_activation_in_cls_sets = graph_search.is_relu_activation_present_in_cls_sets(cls_sets,
                                                                                             tf_op_to_conn_graph_op_map)

        # Convert to a list of cls-set-info elements
        cls_set_info_list = CrossLayerScaling.create_cls_set_info_list(cls_sets, scale_factors,
                                                                       is_relu_activation_in_cls_sets)

        # save and load the updated graph after scaling
        after_cls_sess = save_and_load_graph('./temp_cls', sess)

        return after_cls_sess, cls_set_info_list


class HighBiasFold:
    """
    Class to apply the high-bias-fold technique to a given model
    """

    ActivationIsReluForFirstModule = bool
    ScaleForFirstModule = np.ndarray

    @staticmethod
    def get_bn_params_for_bias_fold(sess: tf.compat.v1.Session, bn_op: tf.Operation, scaling_parameter: np.ndarray):
        """

        :param sess: active tf.compat.v1.Session
        :param bn_op: tf Operation type fused batchnorm op.
        :param scaling_parameter: scaling param as np.ndarray
        :return: bn_params as BNParamsHighBiasFold type.
        """

        bn_params = libpymo.BNParamsHighBiasFold()
        # Scaling gamma and beta parameter of batch norm layer
        gamma = BNUtils.get_gamma_as_numpy_data(sess, bn_op).reshape(-1)
        bn_params.gamma = np.divide(gamma, scaling_parameter)
        beta = BNUtils.get_beta_as_numpy_data(sess, bn_op).reshape(-1)
        bn_params.beta = np.divide(beta, scaling_parameter)

        return bn_params

    @staticmethod
    def _refresh_layer_set_info_before_hbf(sess: tf.compat.v1.Session,
                                           folded_pairs: List[Tuple[tf.Operation, tf.Operation]],
                                           cls_set_info_list: List[ClsSetInfo])\
            -> (List[ClsSetInfo], Dict[str, tf.Operation]):
        """
        As the tensorflow session gets updated, info on op references need to be refreshed.
        :param folded_pairs: bn conv op pairs saved during batchnorm fold.
        :param cls_set_info_list: conv layer info saved during cross layer scaling
        :return: refreshes both data sets to reflect references on new tf.compat.v1.Session.
        """

        bn_dict = {}
        dict_names_to_tf_ops = GraphSearchUtils.map_op_names_to_ops(sess)

        # update info saved during batchnorm fold
        for conv_bn in folded_pairs:
            # get the new op ref from it's name
            bn_dict[conv_bn[0].name] = dict_names_to_tf_ops[conv_bn[1].name]

        # update info saved during cls
        ClsSetInfo.map_cls_sets_to_new_session(dict_names_to_tf_ops, cls_set_info_list)

        return cls_set_info_list, bn_dict

    @staticmethod
    def bias_fold(sess: tf.compat.v1.Session, folded_pairs: List[Tuple[tf.Operation, tf.Operation]],
                  cls_set_info_list: List[ClsSetInfo]) -> tf.compat.v1.Session:

        """
        Folds bias values greater than 3 * sigma to next layer's bias

        :param sess: Current session
        :param folded_pairs: Key: Conv/Linear layer Value: Corresponding folded BN layer
        :param cls_set_info_list: List of info elements for each cls set
        :return: updated session after graph updates from hbf

        """

        with sess.graph.as_default():

            # refresh the references saved during bn fold and cls.
            cls_set_info_list, bn_layers = HighBiasFold._refresh_layer_set_info_before_hbf(sess, folded_pairs,
                                                                                           cls_set_info_list)

            if not bn_layers:
                logger.error('High Bias folding is not supported for models without BatchNorm Layers')
                return sess

            for cls_set_info in cls_set_info_list:

                for cls_pair_info in cls_set_info.cls_pair_info_list:

                    # check if we have a corresponding bn layer
                    if cls_pair_info.layer1.name in bn_layers.keys():

                        # check if bias present in given conv2D(s)
                        if BiasUtils.is_bias_none(cls_pair_info.layer1) or BiasUtils.is_bias_none(cls_pair_info.layer2):
                            continue

                        prev_layer_params = libpymo.LayerParams()
                        curr_layer_params = libpymo.LayerParams()

                        scaling_parameter = cls_pair_info.scale_factor

                        prev_layer_bn_params =\
                            HighBiasFold.get_bn_params_for_bias_fold(sess,
                                                                     bn_layers[cls_pair_info.layer1.name],
                                                                     scaling_parameter)

                        prev_layer_params.activationIsRelu = cls_pair_info.relu_activation_between_layers
                        prev_layer_params.bias =\
                            BiasUtils.get_bias_as_numpy_data(sess, cls_pair_info.layer1).reshape(-1)
                        prev_bias_shape = BiasUtils.get_shape(cls_pair_info.layer1)

                        weight_shape = WeightTensorUtils.get_tensor_shape(cls_pair_info.layer1)
                        prev_layer_params.weightShape = [weight_shape[3], weight_shape[2], weight_shape[0],
                                                         weight_shape[1]]

                        curr_layer_params.bias =\
                            BiasUtils.get_bias_as_numpy_data(sess, cls_pair_info.layer2).reshape(-1)
                        curr_bias_shape = BiasUtils.get_shape(cls_pair_info.layer2)

                        weight_shape = WeightTensorUtils.get_tensor_shape(cls_pair_info.layer2)

                        # Handle depthwise layer case
                        # for a depthwise layer num outputs is set to 1 in TF
                        # send as [Nic, Noc, kh, kw],  TF format is [kh, kw, Nic, Noc]
                        if cls_pair_info.layer2.type in ['DepthwiseConv2dNative']:
                            c_wt = WeightTensorUtils.get_tensor_as_numpy_data(
                                sess, cls_pair_info.layer2).transpose((2, 3, 0, 1))
                            curr_layer_params.weight = c_wt.reshape(-1)
                            curr_layer_params.weightShape = [weight_shape[2], weight_shape[3], weight_shape[0],
                                                             weight_shape[1]]

                        else:
                            # send as [Noc, Nic, kh, kw],  TF format is [kh, kw, Nic, Noc]
                            c_wt = WeightTensorUtils.get_tensor_as_numpy_data(
                                sess, cls_pair_info.layer2).transpose((3, 2, 0, 1))
                            curr_layer_params.weight = c_wt.reshape(-1)
                            curr_layer_params.weightShape = [weight_shape[3], weight_shape[2], weight_shape[0],
                                                             weight_shape[1]]

                        libpymo.updateBias(prev_layer_params, curr_layer_params, prev_layer_bn_params)

                        BiasUtils.update_bias_for_op(sess, cls_pair_info.layer1, np.reshape(prev_layer_params.bias,
                                                                                            prev_bias_shape))

                        BiasUtils.update_bias_for_op(sess, cls_pair_info.layer2, np.reshape(curr_layer_params.bias,
                                                                                            curr_bias_shape))
                    else:
                        logger.info("skipping layer: {%s}", cls_pair_info.layer1.name)

        # save and load the updated graph after high bias fold update
        aftr_hbf_sess = save_and_load_graph('./temp_hbf', sess)

        return aftr_hbf_sess


def equalize_model(sess: tf.compat.v1.Session, start_op_names: Union[str, List[str]],
                   output_op_names: Union[str, List[str]]) -> tf.compat.v1.Session:
    """
    High-level API to perform Cross-Layer Equalization (CLE) on the given model. The model is equalized in place.

    :param sess: tf.compat.v1.Session with model to equalize
    :param start_op_names: Names of starting ops in the given model
    :param output_op_names: List of output op names of the model, used to help ConnectedGraph determine valid ops
           (to ignore training ops for example).
    :return: updated session after bn fold, cls and hbf.

    """

    if not isinstance(start_op_names, (str, List)):
        logger.error('start op names must be passed as a string or a List of strings')

    if isinstance(start_op_names, str):
        start_op_names = [start_op_names]

    # fold batchnorm layers
    after_bn_fold_sess, folded_pairs = fold_all_batch_norms(sess, start_op_names, output_op_names)

    # replace any ReLU6 layers with ReLU
    graph_util = GraphSearchUtils(after_bn_fold_sess.graph, start_op_names, output_op_names)
    after_relu_replace_sess = graph_util.find_and_replace_relu6_with_relu(after_bn_fold_sess)

    # perform cross-layer scaling on applicable layer sets
    after_cls_sess, cls_set_info_list = CrossLayerScaling.scale_model(after_relu_replace_sess, start_op_names,
                                                                      output_op_names)

    # high-bias fold
    after_hbf_sess = HighBiasFold.bias_fold(after_cls_sess, folded_pairs, cls_set_info_list)

    return after_hbf_sess
