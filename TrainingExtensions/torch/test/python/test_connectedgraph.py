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
""" This file contains unit tests for testing ConnectedGraph module for PyTorch. """

import unittest
import torch
from aimet_common.connected_graph.connectedgraph_utils import get_all_input_ops, get_all_output_ops
from aimet_torch.examples.test_models import TinyModel, SingleResidual, MultiInput, ConcatModel, ModuleListModel,\
    ModelWithDropouts, SequentialModel, HierarchicalModel, PassThroughOpLastLayerModel, MultiOutputModel,\
    TupleOutputModel, ConfigurableTupleOutputModel, BasicConv2d, DictInputModel, NestedSequentialModel

from aimet_torch.meta.connectedgraph import ConnectedGraph
from aimet_torch.meta.connectedgraph_utils import get_module_act_func_pair
from aimet_torch.utils import create_rand_tensors_given_shapes


class TestConnectedGraph(unittest.TestCase):
    """ Unit tests for testing ConnectedGraph module"""

    def test_single_residual(self):
        """ Test building ConnectedGraph on single residual model """
        # pylint: disable=protected-access
        model = SingleResidual()
        model.eval()
        inp_shape = (1, 3, 32, 32)
        inp_tensor_list = create_rand_tensors_given_shapes(inp_shape)
        conn_graph = ConnectedGraph(model, inp_tensor_list)
        self.assertEqual(17, len(conn_graph.ordered_ops))
        # Split count of 2 due to residual as well as reshape having a split
        self.assertEqual(2, conn_graph._split_count)
        # All ops will include 2 inserted split ops
        self.assertEqual(19, len(conn_graph.get_all_ops().keys()))
        input_ops = get_all_input_ops(conn_graph)
        self.assertEqual(1, len(input_ops))
        self.assertEqual(model.conv1, input_ops[0].get_module())
        output_ops = get_all_output_ops(conn_graph)
        self.assertEqual(1, len(output_ops))
        self.assertEqual(model.fc, output_ops[0].get_module())

    def test_multi_input(self):
        """ Test building ConnectedGraph on a model with multiple inputs """
        # pylint: disable=protected-access
        model = MultiInput()
        model.eval()
        inp_shape_1 = (1, 3, 32, 32)
        inp_shape_2 = (1, 3, 20, 20)
        inp_tensor_list = create_rand_tensors_given_shapes([inp_shape_1, inp_shape_2])
        conn_graph = ConnectedGraph(model, inp_tensor_list)
        self.assertEqual(11, len(conn_graph.ordered_ops))
        # Split count of 1 due to reshape having a split
        self.assertEqual(1, conn_graph._split_count)
        conv1 = conn_graph.get_op_from_module_name('MultiInput.conv1')
        self.assertEqual(model.conv1, conv1.get_module())
        self.assertEqual(2, len(conv1.inputs))
        conv2 = conn_graph.get_op_from_module_name('MultiInput.conv2')
        self.assertEqual(model.conv2, conv2.get_module())
        self.assertEqual(3, len(conv2.inputs))
        conv3 = conn_graph.get_op_from_module_name('MultiInput.conv3')
        self.assertEqual(model.conv3, conv3.get_module())
        self.assertEqual(3, len(conv3.inputs))

        input_ops = get_all_input_ops(conn_graph)
        input_modules = [op.get_module() for op in input_ops]
        self.assertEqual(2, len(input_ops))
        self.assertTrue(model.conv1 in input_modules)
        self.assertTrue(model.conv3 in input_modules)
        output_ops = get_all_output_ops(conn_graph)
        self.assertEqual(1, len(output_ops))
        self.assertEqual(model.fc, output_ops[0].get_module())

    def test_module_list(self):
        """ Test building ConnectedGraph on a model with module list """
        model = ModuleListModel()
        model.eval()
        inp_data_1 = torch.rand(1, 3, 8, 8)
        conn_graph = ConnectedGraph(model, (inp_data_1,))
        self.assertEqual(10, len(conn_graph.ordered_ops))
        self.assertEqual(conn_graph.get_op_from_module_name('ModuleListModel.mod_list.4'), conn_graph.ordered_ops[0])
        self.assertEqual(conn_graph.get_op_from_module_name('ModuleListModel.seq_list.2'), conn_graph.ordered_ops[1])
        self.assertEqual(conn_graph.get_op_from_module_name('ModuleListModel.mod_list.1'), conn_graph.ordered_ops[2])
        self.assertEqual(conn_graph.get_op_from_module_name('ModuleListModel.mod_list.0'), conn_graph.ordered_ops[3])
        self.assertEqual(conn_graph.get_op_from_module_name('ModuleListModel.mod_list.2'), conn_graph.ordered_ops[4])
        self.assertEqual(conn_graph.get_op_from_module_name('ModuleListModel.seq_list.0'), conn_graph.ordered_ops[5])

    def test_concat(self):
        """ Test building ConnectedGraph on a model with concat """
        model = ConcatModel()
        model.eval()
        inp_shape_1 = (1, 3, 8, 8)
        inp_shape_2 = (1, 3, 8, 8)
        inp_shape_3 = (1, 3, 8, 8)
        inp_tensor_list = create_rand_tensors_given_shapes([inp_shape_1, inp_shape_2, inp_shape_3])
        conn_graph = ConnectedGraph(model, inp_tensor_list)
        concat_op = conn_graph.get_all_ops()['cat_3']
        self.assertEqual(3, len(concat_op.inputs))
        self.assertEqual(14, concat_op.output_shape[1])

    def test_dropouts(self):
        """ Test building ConnectedGraph on a model with dropouts """
        # pylint: disable=protected-access
        model = ModelWithDropouts()
        model.eval()
        inp_shape = (1, 3, 32, 32)
        inp_tensor_list = create_rand_tensors_given_shapes(inp_shape)
        conn_graph = ConnectedGraph(model, inp_tensor_list)
        self.assertEqual(9, len(conn_graph.ordered_ops))
        # Split count of 2 due to residual as well as reshape having a split
        self.assertEqual(1, conn_graph._split_count)
        # All ops will include 2 inserted split ops
        self.assertEqual(10, len(conn_graph.get_all_ops().keys()))
        dropout_1_op = conn_graph.get_all_ops()['dropout_3']
        dropout_2_op = conn_graph.get_all_ops()['feature_dropout_4']
        self.assertEqual(model.dropout1, dropout_1_op.get_module())
        self.assertEqual(model.dropout2, dropout_2_op.get_module())

    def test_sequential(self):
        # pylint: disable=protected-access
        """ Test building ConnectedGraph on a model constructed with nn.Sequential Module """
        model = SequentialModel()
        model.eval()
        inp_data_1 = torch.rand(1, 3, 8, 8)
        conn_graph = ConnectedGraph(model, (inp_data_1,))
        self.assertEqual(10, len(conn_graph.ordered_ops))
        # Expect 1 split for the reshape operation
        self.assertEqual(1, conn_graph._split_count)

    def test_hierarchial_model(self):
        """ Test building ConnectedGraph on model which multi-level aggregation of nn.Modules  """
        # pylint: disable=protected-access
        model = HierarchicalModel()
        model.eval()
        conv_shape = (1, 64, 32, 32)
        inp_shape = (1, 3, 32, 32)
        seq_shape = (1, 3, 8, 8)
        inp_tensor_list = create_rand_tensors_given_shapes([conv_shape, inp_shape, conv_shape, inp_shape, seq_shape])
        conn_graph = ConnectedGraph(model, inp_tensor_list)
        self.assertEqual(95, len(conn_graph.ordered_ops))
        self.assertEqual(5, conn_graph._split_count)
        self.assertEqual(conn_graph.get_op_from_module_name('HierarchicalModel.conv1.conv'), conn_graph.ordered_ops[0])
        self.assertEqual(conn_graph.get_op_from_module_name('HierarchicalModel.nm1.tm1.conv1'), conn_graph.ordered_ops[5])
        self.assertEqual(conn_graph.get_op_from_module_name('HierarchicalModel.nm1.tm2.conv1'), conn_graph.ordered_ops[20])
        self.assertEqual(conn_graph.get_op_from_module_name('HierarchicalModel.conv2.conv'), conn_graph.ordered_ops[36])
        self.assertEqual(conn_graph.get_op_from_module_name('HierarchicalModel.multi_conv.seq_list.0.conv'), conn_graph.ordered_ops[40])
        self.assertEqual(conn_graph.get_op_from_module_name('HierarchicalModel.nm2.tm1.conv1'), conn_graph.ordered_ops[53])
        self.assertEqual(conn_graph.get_op_from_module_name('HierarchicalModel.nm2.tm2.conv1'), conn_graph.ordered_ops[68])
        self.assertEqual(conn_graph.get_op_from_module_name('HierarchicalModel.sq.seq_list.0'), conn_graph.ordered_ops[84])

    def test_passthrough_op_last_module(self):
        """ Test building a connected graph on a model where a PassThroughOp is the last module in the graph. """
        model = PassThroughOpLastLayerModel()
        model.eval()
        inp_shape = (1, 3, 32, 32)
        inp_tensor_list = create_rand_tensors_given_shapes(inp_shape)
        conn_graph = ConnectedGraph(model, inp_tensor_list)
        self.assertEqual(1, len(conn_graph.ordered_ops))

    def test_get_module_act_func_pair_with_modules(self):
        """ Test get module activation function pair - activations are nn.Modules """

        model = TinyModel().eval()
        inp_tensor_list = [torch.randn(1, 3, 32, 32)]

        module_act_func_pair = get_module_act_func_pair(model, inp_tensor_list)

        # 12 modules
        self.assertEqual(len(module_act_func_pair), 12)

        # followed by relu case
        self.assertTrue(isinstance(module_act_func_pair[model.bn1], torch.nn.ReLU))
        self.assertTrue(isinstance(module_act_func_pair[model.bn2], torch.nn.ReLU))
        self.assertTrue(isinstance(module_act_func_pair[model.conv3], torch.nn.ReLU))

        # not followed by relu case
        self.assertEqual(module_act_func_pair[model.conv1], None)
        self.assertEqual(module_act_func_pair[model.conv2], None)

        # final module case
        self.assertEqual(module_act_func_pair[model.fc], None)

    def test_multi_output_model(self):
        """ Test multi-output model with Tuple Tensor as intermediate  output. """
        model = MultiOutputModel()
        inp_data = torch.rand(1, 3, 8, 8)
        conn_graph = ConnectedGraph(model, (inp_data,))
        self.assertEqual(7, len(conn_graph.ordered_ops))
        self.assertEqual(6, len([op for op in conn_graph.get_all_ops().keys() if 'convolution' in op]))
        self.assertEqual(0, len([op for op in conn_graph.get_all_ops().keys() if 'Tuple' in op]))
        self.assertEqual(0, len([product for product in conn_graph.get_all_products().keys() if 'Tuple' in product]))
        self.assertEqual('cat', conn_graph.ordered_ops[-1].type)

    def test_multi_output_with_unuse_model(self):
        """ Test multi-output model with Tuple Tensor as intermediate output and with one of tuple tensor not used """

        class MultiOutputWithUnuseModel(torch.nn.Module):
            """
            Model with Tuple of Tensors as output with one output tensor unused
            """
            def __init__(self):
                super(MultiOutputWithUnuseModel, self).__init__()
                self.layer = TupleOutputModel()
                self.conv1 = torch.nn.Conv2d(2, 4, kernel_size=3, padding=1)
                self.conv2 = torch.nn.Conv2d(6, 4, kernel_size=3, padding=1)

            def forward(self, *inputs):
                x, _, z = self.layer(inputs[0])
                x1 = self.conv1(x)
                z1 = self.conv2(z)
                return torch.cat([x1, z1], 1)

        inp_data = torch.rand(1, 3, 8, 8)
        model = MultiOutputWithUnuseModel()
        conn_graph = ConnectedGraph(model, (inp_data,))
        self.assertEqual(6, len(conn_graph.ordered_ops))
        self.assertEqual(5, len([op for op in conn_graph.get_all_ops().keys() if 'convolution' in op]))
        self.assertEqual(0, len([op for op in conn_graph.get_all_ops().keys() if 'Tuple' in op]))
        self.assertEqual('cat', conn_graph.ordered_ops[-1].type)

        product_names = conn_graph.get_all_products().keys()
        self.assertEqual(0, len([product for product in product_names if 'Tuple' in product]))

        expected_products = [
            # layer #1 to conv1,conv2
            'convolution_0_to_convolution_3',
            'convolution_2_to_convolution_4',

            # conv1,conv2 to cat
            'convolution_3_to_cat_5',
            'convolution_4_to_cat_5']

        products = conn_graph.get_all_products()
        for product_name in product_names:
            if product_name in expected_products:
                product = products[product_name]
                self.assertEqual(product.shape, product.producer.output_shape)
                expected_products.remove(product_name)
        self.assertEqual(0, len(expected_products))

    def test_multi_output_with_matched_layers(self):
        """ Test a multiple layer multi-output model with intermediate Tuple Tensors shuffled """
        class MultiOutputLayersModel(torch.nn.Module):
            """
            Model with Tuple of Tensors as output shuffled between layers
            """
            def __init__(self):
                super(MultiOutputLayersModel, self).__init__()
                self.layer1 = ConfigurableTupleOutputModel(channels=(1, 2, 3))
                self.layer2 = ConfigurableTupleOutputModel(channels=(1, 2, 3))
                self.layer3 = ConfigurableTupleOutputModel(channels=(1, 2, 3))

            def forward(self, *inputs):
                x1, x2, x3 = self.layer1(inputs[0], inputs[1], inputs[2])
                y1, y2, y3 = self.layer2(x1, x2, x3)
                z1, z2, z3 = self.layer3(y1, y2, y3)
                return torch.cat([z1, z2, z3], 1)

        model = MultiOutputLayersModel()
        inp_tensor_list = create_rand_tensors_given_shapes([(1, 1, 8, 8), (1, 2, 8, 8), (1, 3, 8, 8)])
        conn_graph = ConnectedGraph(model, inp_tensor_list)
        self.assertEqual(10, len(conn_graph.ordered_ops))
        self.assertEqual(9, len([op for op in conn_graph.get_all_ops().keys() if 'convolution' in op]))
        self.assertEqual(0, len([op for op in conn_graph.get_all_ops().keys() if 'Tuple' in op]))
        self.assertEqual('cat', conn_graph.ordered_ops[-1].type)

        product_names = conn_graph.get_all_products().keys()
        self.assertEqual(0, len([product for product in product_names if 'Tuple' in product]))

        expected_products = [
            # layer #1 to layer #2
            'convolution_0_to_convolution_3',
            'convolution_1_to_convolution_4',
            'convolution_2_to_convolution_5',

            # layer #2 to layer #3
            'convolution_3_to_convolution_6',
            'convolution_4_to_convolution_7',
            'convolution_5_to_convolution_8',

            # layer #3 to cat
            'convolution_6_to_cat_9',
            'convolution_7_to_cat_9',
            'convolution_8_to_cat_9']

        products = conn_graph.get_all_products()
        for product_name in product_names:
            if product_name in expected_products:
                product = products[product_name]
                self.assertEqual(product.shape, product.producer.output_shape)
                expected_products.remove(product_name)
        self.assertEqual(0, len(expected_products))

    def test_multi_output_with_shuffled_layers(self):
        """ Test a multiple layer multi-output model with intermediate Tuple Tensors shuffled """
        class MultiOutputShuffledModel(torch.nn.Module):
            """
            Model with Tuple of Tensors as output shuffled between layers
            """
            def __init__(self):
                super(MultiOutputShuffledModel, self).__init__()
                self.layer1 = ConfigurableTupleOutputModel(channels=(1, 2, 3))
                self.layer2 = ConfigurableTupleOutputModel(channels=(2, 3, 1))
                self.layer3 = ConfigurableTupleOutputModel(channels=(3, 1, 2))

            def forward(self, *inputs):
                x1, x2, x3 = self.layer1(inputs[0], inputs[1], inputs[2])
                y2, y3, y1 = self.layer2(x2, x3, x1)
                z3, z1, z2 = self.layer3(y3, y1, y2)
                return torch.cat([z1, z2, z3, x1], 1)

        model = MultiOutputShuffledModel()
        inp_tensor_list = create_rand_tensors_given_shapes([(1, 1, 8, 8), (1, 2, 8, 8), (1, 3, 8, 8)])
        conn_graph = ConnectedGraph(model, inp_tensor_list)
        self.assertEqual(10, len(conn_graph.ordered_ops))
        self.assertEqual(9, len([op for op in conn_graph.get_all_ops().keys() if 'convolution' in op]))
        self.assertEqual(0, len([op for op in conn_graph.get_all_ops().keys() if 'Tuple' in op]))
        self.assertEqual('cat', conn_graph.ordered_ops[-1].type)

        product_names = conn_graph.get_all_products().keys()
        self.assertEqual(0, len([product for product in product_names if 'Tuple' in product]))

        expected_products = [
            # TODO fix order of products

            # layer #1 to layer #2
            'convolution_0__to__Split_0',
            'convolution_1_to_convolution_3',
            'convolution_2_to_convolution_4',

            # layer #2 to layer #3
            'convolution_3_to_convolution_8',
            'convolution_4_to_convolution_6',
            'convolution_5_to_convolution_7',

            # layer #3, layer#1.conv1 to cat
            'convolution_6_to_cat_9',
            'convolution_7_to_cat_9',
            'convolution_8_to_cat_9']

        products = conn_graph.get_all_products()
        for product_name in product_names:
            if product_name in expected_products:
                product = products[product_name]
                self.assertEqual(product.shape, product.producer.output_shape)
                expected_products.remove(product_name)
        self.assertEqual(0, len(expected_products))
        split_product = conn_graph.get_all_products()['Split_0__to__multiple_ops']
        self.assertTrue(conn_graph.get_all_ops()['convolution_5'] in split_product.consumers)
        self.assertTrue(conn_graph.get_all_ops()['cat_9'] in split_product.consumers)

    def test_submodules_with_sequence_and_module_list(self):
        """ Test building ConnectedGraph on a model with sequence and module list """

        class ModuleListAndSequentialModel(torch.nn.Module):
            def __init__(self):
                super(ModuleListAndSequentialModel, self).__init__()
                self.mod_list = torch.nn.ModuleList([
                    torch.nn.Sequential(
                        BasicConv2d(kernel_size=3),
                        BasicConv2d(kernel_size=3)
                    ),
                    torch.nn.Sequential(
                        torch.nn.Sequential(
                            BasicConv2d(kernel_size=3),
                            BasicConv2d(kernel_size=3)
                        ),
                    ),
                    torch.nn.ModuleList([
                        torch.nn.ModuleList([
                       BasicConv2d(kernel_size=3)
                        ])
                    ]),
                    ModuleListModel()]
                )

            def forward(self, *inputs):
                s1 = self.mod_list[0](inputs[0])
                s2 = self.mod_list[1](inputs[0])
                m1 = self.mod_list[2][0][0](inputs[0])
                m2 = self.mod_list[3](inputs[1])
                return s1, s2, m1,m2
        inp_data_1 = torch.rand(1, 64, 8, 8)
        inp_data_2 = torch.rand(1, 3, 8, 8)
        conn_graph = ConnectedGraph(ModuleListAndSequentialModel(), (inp_data_1, inp_data_2))
        self.assertEqual(30, len(conn_graph.ordered_ops))
        self.assertEqual(0, len([op for op in conn_graph.get_all_ops().keys() if 'Tuple' in op]))

    def test_module_reuse_model(self):
        class ReuseReluLeafModel(torch.nn.Module):
            """ A model with Relu instance used multiple times
            Expected one input of size (1, 64, 8, 8) """

            def __init__(self):
                super(ReuseReluLeafModel, self).__init__()
                self.conv1 = torch.nn.Conv2d(64, 64, kernel_size=3, padding=1)
                self.conv2 = torch.nn.Conv2d(64, 64, kernel_size=3, padding=1)
                self.relu = torch.nn.ReLU(inplace=True)

            def forward(self, *inputs):
                x = self.conv1(inputs[0])
                x = self.relu(x)
                x = self.conv2(x)
                return self.relu(x)

        inp_data = torch.rand(1, 64, 8, 8)
        model = ReuseReluLeafModel()
        conn_graph = ConnectedGraph(model, (inp_data,))
        self.assertEqual(4, len(conn_graph.ordered_ops))
        self.assertEqual(2, len([op for name, op in conn_graph.get_all_ops().items()
                                 if 'relu' in name and
                                 op.get_module() == model.relu]))

        class ReluModel(torch.nn.Module):
            def __init__(self):
                super(ReluModel, self).__init__()
                self.relu = torch.nn.ReLU(inplace=True)

            def forward(self, *inputs):
                return self.relu( inputs[0])

        class ReuseReluLayerModel(torch.nn.Module):
            """ A model with Relu Layer instance used multiple times
            Expected one input of size (1, 64, 8, 8) """

            def __init__(self):
                super(ReuseReluLayerModel, self).__init__()
                self.conv = torch.nn.Conv2d(64, 64, kernel_size=3, padding=1)
                self.layer = ReluModel()

            def forward(self, *inputs):
                x = self.layer(inputs[0])
                x = self.conv(x)
                return self.layer(x)

        layer_model = ReuseReluLayerModel()
        conn_graph = ConnectedGraph(layer_model, (inp_data,))
        self.assertEqual(3, len(conn_graph.ordered_ops))
        self.assertEqual(2, len([op for name, op in conn_graph.get_all_ops().items()
                                 if 'relu' in name and
                                 op.get_module() == layer_model.layer.relu]))

    def test_dict_input(self):
        """ Test building ConnectedGraph on a model with multiple inputs """
        # pylint: disable=protected-access
        model = DictInputModel()
        model.eval()
        inp_shape_1 = (1, 3, 32, 32)
        inp_shape_2 = (1, 3, 20, 20)
        inp_tensor_list = create_rand_tensors_given_shapes([inp_shape_1, inp_shape_2])
        dict_input = {'inp_1': inp_tensor_list[0], 'inp_2': inp_tensor_list[1]}
        conn_graph = ConnectedGraph(model, dict_input)
        self.assertEqual(11, len(conn_graph.ordered_ops))

        # Split count of 1 due to reshape having a split
        self.assertEqual(1, conn_graph._split_count)
        conv1 = conn_graph.get_op_from_module_name('DictInputModel.conv1')
        self.assertEqual(model.conv1, conv1.get_module())
        self.assertEqual(2, len(conv1.inputs))
        conv2 = conn_graph.get_op_from_module_name('DictInputModel.conv2')
        self.assertEqual(model.conv2, conv2.get_module())
        self.assertEqual(3, len(conv2.inputs))
        conv3 = conn_graph.get_op_from_module_name('DictInputModel.conv3')
        self.assertEqual(model.conv3, conv3.get_module())
        self.assertEqual(3, len(conv3.inputs))

        input_ops = get_all_input_ops(conn_graph)
        input_modules = [op.get_module() for op in input_ops]
        self.assertEqual(2, len(input_ops))
        self.assertTrue(model.conv1 in input_modules)
        self.assertTrue(model.conv3 in input_modules)
        output_ops = get_all_output_ops(conn_graph)
        self.assertEqual(1, len(output_ops))
        self.assertEqual(model.fc, output_ops[0].get_module())

    def test_nested_sequential(self):
        # pylint: disable=protected-access
        """ Test building ConnectedGraph on a model constructed with nested nn.Sequential Module """
        model = NestedSequentialModel()
        model.eval()
        inp_data_1 = torch.rand(1, 3, 8, 8)
        conn_graph = ConnectedGraph(model, (inp_data_1,))
        self.assertEqual(10, len(conn_graph.ordered_ops))
        # Expect 1 split for the reshape operation
        self.assertEqual(1, conn_graph._split_count)
