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

import os
import signal
import unittest
import tensorflow as tf

tf.compat.v1.logging.set_verbosity(tf.logging.WARN)
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

import numpy as np
from aimet_common.utils import start_bokeh_server_session
from tensorflow.keras.applications.resnet50 import ResNet50

from aimet_tensorflow import plotting_utils


class TFVisualization(unittest.TestCase):
    """ Test methods for BatchNormFold"""

    def test_visualize_weight_ranges_single_layer(self):
        tf.compat.v1.reset_default_graph()
        _ = ResNet50(weights=None)

        model = tf.compat.v1.get_default_graph()
        init = tf.compat.v1.global_variables_initializer()
        sess = tf.compat.v1.Session(graph=model)
        sess.run(init)

        conv_op = sess.graph.get_operation_by_name('conv1_conv/Conv2D')

        visualization_url, process = start_bokeh_server_session(8001)
        plotting_utils.visualize_weight_ranges_single_layer(sess, conv_op, visualization_url)
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)

        sess.close()

    def test_visualize_relative_weight_ranges_single_layer(self):

        tf.compat.v1.reset_default_graph()
        _ = ResNet50(weights=None)

        model = tf.compat.v1.get_default_graph()
        init = tf.compat.v1.global_variables_initializer()
        sess = tf.compat.v1.Session(graph=model)
        sess.run(init)

        conv_op = sess.graph.get_operation_by_name('conv1_conv/Conv2D')

        visualization_url, process = start_bokeh_server_session(8001)
        plotting_utils.visualize_relative_weight_ranges_single_layer(sess, conv_op, visualization_url)
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)

        sess.close()

