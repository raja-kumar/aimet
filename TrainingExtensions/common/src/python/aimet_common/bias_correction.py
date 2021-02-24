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
"""  holds common code for bias correction """

from aimet_common.defs import ActivationType
from aimet_common.utils import AimetLogger

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Utils)


class ConvBnInfoType:
    """
    Type for hoding convs with bn info and activation types
    Activation types supported are Relu and Relu6
    """
    def __init__(self,
                 input_bn=None,
                 output_bn=None,
                 in_activation_type: ActivationType = ActivationType.no_activation,
                 out_activation_type: ActivationType = ActivationType.no_activation):
        """
        :param input_bn: Reference to Input BatchNorm to layer
        :param output_bn: Reference to Output BatchNorm to layer
        :param in_activation_type: Type of Activation
        :param out_activation_type: Type of Activation
        """

        self.input_bn = input_bn
        self.output_bn = output_bn
        self.in_activation_type = in_activation_type
        self.out_activation_type = out_activation_type


class ConvBnPatternHandler:
    """
    common handler for matched patterns for bias correction and batchnorm fold.
    """

    def __init__(self):
        self.conv_linears_with_bn_dict = {}

    def get_conv_linear_bn_info_dict(self):
        """
        returns the dictionary created
        :return: dictionary of convs/linears with bn and activation info
        """
        return self.conv_linears_with_bn_dict

    def __call__(self, *args, **kwargs):
        """
         custom pattern match handler that keeps a dictionary of convs/linears with bn and activation info.
        """

        _, op_subset = args

        bn_activation_info = ConvBnInfoType()

        activation_type = ActivationType.no_activation
        conv_op = None
        bn_op = None
        convolution_types = ['Conv2D', 'DepthwiseConv2dNative', 'convolution']
        linear_types = ['Dense', 'addmm', 'matmul']
        bn_types = ['FusedBatchNormV3', 'batch_norm']

        for op in op_subset:
            if op.type in convolution_types + linear_types:
                conv_op = op
                if conv_op.get_module() in self.conv_linears_with_bn_dict.keys():
                    bn_activation_info = self.conv_linears_with_bn_dict[conv_op.get_module()]
            elif op.type in bn_types:
                bn_op = op
            elif op.type in ['Relu6', 'hardtanh']:
                activation_type = ActivationType.relu6
            elif op.type in ['Relu', 'relu']:
                activation_type = ActivationType.relu

        if len(op_subset) >= 2:
            if op_subset[0].type in bn_types:
                bn_activation_info.input_bn = bn_op
                bn_activation_info.in_activation_type = activation_type
            # we do not match linear layers with preceding bn for bias correction
            elif op_subset[0].type in convolution_types + linear_types:
                bn_activation_info.output_bn = bn_op
                bn_activation_info.out_activation_type = activation_type
            # in tf linear layer has two ops together [flatten/reshape -- dense] , check for len 3
            elif len(op_subset) >= 3 and op_subset[1].type in ['Dense']:
                bn_activation_info.output_bn = bn_op
                bn_activation_info.out_activation_type = activation_type

        self.conv_linears_with_bn_dict[conv_op.get_module()] = bn_activation_info
