#!/usr/bin/env python3.5

#  =============================================================================
#
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
#
#  =============================================================================
"""Search a tf graph for winnowing opportunities and apply all required changes."""

from typing import List, Tuple
import tensorflow as tf

from aimet_tensorflow.winnow.mask_propagation_winnower import MaskPropagationWinnower


def winnow_tf_model(sess: tf.compat.v1.Session, input_op_names: List[str], output_op_names: List[str],
                    list_of_modules_to_winnow: List[Tuple[tf.Operation, List]] = None,
                    reshape=True, in_place=False, verbose=False):

    """ This API is used to winnow a model with Conv2d modules that each have a list of channels to be winnowed.
    There is no need to zero out the modules' input channels before calling this API.

    :param sess: The tf.compat.v1.Session to be winnowed.
    :param input_op_names: Names of input ops to the model.
    :param output_op_names: List of output op names of the model, used to help ConnectedGraph determine valid ops
    (to ignore training ops for example).
    :param list_of_modules_to_winnow: A list of Tuples with each Tuple containing a module and a list of channels
                                             to be winnowed for that module.
    :param reshape: f set to True a Down Sample Layer is added between modules to match the number of channels.
                    If set to False, the modules that need a Down Sample Layer will not be winnowed.
    :param in_place: If set to True, the model will be winnowed in place.
                     If set to False, a copy of the model will be winnowed.
    :param verbose: If set to True, logs detailed winnowing log messages.
    :return: A list of tuples containing information on winnowed modules.
    Tuples contain (original module name, new module, input masks, output masks)
    """

    mask_winnower = MaskPropagationWinnower(sess, input_op_names, output_op_names, list_of_modules_to_winnow, reshape,
                                            in_place, verbose)
    new_sess, ordered_modules_list = mask_winnower.propagate_masks_and_winnow()

    return new_sess, ordered_modules_list
