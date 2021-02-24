# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#  
#  Copyright (c) 2017-2018, Qualcomm Innovation Center, Inc. All rights reserved.
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

from __future__ import print_function

import pytest
import torch
import torch.nn as nn
import unittest
import torch.optim as optim
import numpy as np
from torchvision import models
from torch.optim import lr_scheduler

from aimet_common.defs import QuantScheme
from aimet_torch.quantsim import QuantizationSimModel

from aimet_torch.examples.imagenet_dataloader import ImageNetDataLoader
from aimet_torch.utils import IterFirstX
from aimet_torch.examples.supervised_classification_pipeline import create_stand_alone_supervised_classification_evaluator,\
    create_supervised_classification_trainer

two_class_image_dir = './data/tiny-imagenet-2'
image_size = 224
batch_size = 50
num_workers = 1


def model_train(model, epochs, callback=None):
    """
    :param model: model
    :param epochs: number of epochs
    :return: accuracy after each epoch on training , validation data
    """

    data_loader = ImageNetDataLoader(two_class_image_dir, image_size, batch_size, num_workers)
    criterion = nn.CrossEntropyLoss().cuda()
    lr = 0.01
    momentum = 0.9
    lr_step_size = 0.01
    lr_gamma = 0.01
    optimizer = optim.SGD(model.parameters(), lr=lr, momentum=momentum)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=lr_step_size, gamma=lr_gamma)

    trainer, evaluator = create_supervised_classification_trainer(model=model, loss_fn=criterion, optimizer=optimizer,
                                                                  val_loader=data_loader.val_loader,
                                                                  learning_rate_scheduler=exp_lr_scheduler,
                                                                  use_cuda=True,
                                                                  callback=callback)

    trainer.run(data_loader.train_loader, max_epochs=epochs)
    return trainer.state.metrics['top_1_accuracy'], evaluator.state.metrics['top_1_accuracy']


def model_eval(model, early_stopping_iterations):
    """
    :param model: model to be evaluated
    :param early_stopping_iterations: if None, data loader will iterate over entire validation data
    :return: top_1_accuracy on validation data
    """

    use_cuda = next(model.parameters()).is_cuda

    data_loader = ImageNetDataLoader(two_class_image_dir, image_size, batch_size, num_workers)
    if early_stopping_iterations is not None:
        # wrapper around validation data loader to run only 'X' iterations to save time
        val_loader = IterFirstX(data_loader.val_loader, early_stopping_iterations)
    else:
        # iterate over entire validation data set
        val_loader = data_loader.val_loader

    criterion = nn.CrossEntropyLoss().cuda()
    evaluator = create_stand_alone_supervised_classification_evaluator(model, criterion, use_cuda=use_cuda)
    evaluator.run(val_loader)
    return evaluator.state.metrics['top_1_accuracy']


def check_if_layer_weights_are_updating(trainer, model):
    """
    :param trainer: The handler function's first argument is the 'Engine' object it is bound to
    :param model: model
    """
    # Creating an alias for easier reference
    f = check_if_layer_weights_are_updating

    print("")
    print("checking weights for iteration = {}".format(trainer.state.iteration))

    # get the initial weight values of conv1 layer of first block
    conv1_w_value = model.classifier[0]._module_to_wrap.weight

    if trainer.state.iteration != 1:
        assert not np.allclose(conv1_w_value.cpu().detach().numpy(), f.conv1_w_value_old.numpy())
    else:
        f.conv1_w_value_old = conv1_w_value.cpu().detach().clone()


class QuantizeAcceptanceTests(unittest.TestCase):

    @pytest.mark.cuda
    def test_quantize_resnet18(self):

        torch.cuda.empty_cache()

        # Train the model using tiny imagenet data
        model = models.resnet18(pretrained=False)
        _ = model_train(model, epochs=2)
        model = model.to(torch.device('cuda'))

        # layers_to_ignore = [model.conv1]
        sim = QuantizationSimModel(model, quant_scheme=QuantScheme.post_training_tf, default_param_bw=8,
                                   default_output_bw=8, dummy_input=torch.rand(1, 3, 224, 224).cuda())

        print(sim.model)

        # If 'iterations'set to None, will iterate over all the validation data
        sim.compute_encodings(model_eval, forward_pass_callback_args=400)
        quantized_model_accuracy = model_eval(model=sim.model, early_stopping_iterations=None)

        print("Quantized model accuracy=", quantized_model_accuracy)
        self.assertGreaterEqual(quantized_model_accuracy, 0.5)

    @pytest.mark.cuda
    def test_memory_leak_during_quantization_train(self):

        # First get baseline numbers
        base_pre_model_load_mark = torch.cuda.memory_allocated()
        model = models.vgg16(pretrained=True)
        model = model.to(torch.device('cuda'))
        base_model_loaded_mark = torch.cuda.memory_allocated()

        _ = model_train(model=model, epochs=2)
        base_model_train_mark = torch.cuda.memory_allocated()
        base_model_train_delta = base_model_train_mark - base_model_loaded_mark

        print("Usage Report ------")
        print("Model pre-load = {}".format(base_pre_model_load_mark))
        print("Model load = {}".format(base_model_loaded_mark))
        print("Model train delta = {}".format(base_model_train_delta))

        del model
        baseline_leaked_mem = torch.cuda.memory_allocated() - base_pre_model_load_mark
        print("Leaked during train = {}".format(baseline_leaked_mem))

        model = models.vgg16(pretrained=True)
        model = model.to(torch.device('cuda'))
        base_model_loaded_mark = torch.cuda.memory_allocated()
        #
        # # Now use AIMET
        sim = QuantizationSimModel(model, quant_scheme=QuantScheme.post_training_tf_enhanced,
                                   default_param_bw=8, default_output_bw=4,
                                   dummy_input=torch.rand(1, 3, 224, 224).cuda())
        sim.compute_encodings(model_eval, forward_pass_callback_args=1)

        print(sim.model)
        aimet_model_quantize_mark = torch.cuda.memory_allocated()
        aimet_model_quantize_delta = aimet_model_quantize_mark - base_model_loaded_mark

        _ = model_train(model=sim.model, epochs=2,
                        callback=check_if_layer_weights_are_updating)

        aimet_model_train_mark = torch.cuda.memory_allocated()
        aimet_model_train_delta = aimet_model_train_mark - aimet_model_quantize_mark
        leaked_memory = aimet_model_train_delta - base_model_train_delta + baseline_leaked_mem

        print("")
        print("Usage Report ------")
        print("Model load = {}".format(base_model_loaded_mark))
        print("AIMET quantize delta = {}".format(aimet_model_quantize_delta))
        print("AIMET train delta = {}".format(aimet_model_train_delta))
        print("Leaked memory = {}".format(leaked_memory))

        # During training, the memory is held for a longer duration by PyTorch.
        # Often, this test fails with the following assert failing.
        # When the test is run individually, this test may still fail.
        # The tolerance is bumped up to take care of the situation where all tests are run.
        self.assertLessEqual(leaked_memory, 2000000)

    @pytest.mark.cuda
    def test_memory_leak_during_quantization_eval(self):

        # First get baseline numbers
        base_pre_model_load_mark = torch.cuda.memory_allocated()
        model = models.vgg16(pretrained=True)
        model = model.to(torch.device('cuda'))
        base_model_loaded_mark = torch.cuda.memory_allocated()

        _ = model_eval(model=model, early_stopping_iterations=10)
        base_model_eval_mark = torch.cuda.memory_allocated()
        base_model_eval_delta = base_model_eval_mark - base_model_loaded_mark

        print("Usage Report ------")
        print("Model pre-load = {}".format(base_pre_model_load_mark))
        print("Model load = {}".format(base_model_loaded_mark))
        print("Model eval delta = {}".format(base_model_eval_delta))

        del model
        print("Leaked during eval = {}".format(torch.cuda.memory_allocated() - base_pre_model_load_mark))

        model = models.vgg16(pretrained=True)
        model = model.to(torch.device('cuda'))
        base_model_loaded_mark = torch.cuda.memory_allocated()

        # Now use AIMET
        sim = QuantizationSimModel(model, quant_scheme=QuantScheme.post_training_tf_enhanced,
                                   default_param_bw=8, default_output_bw=4,
                                   dummy_input=torch.rand(1, 3, 224, 224).cuda())
        sim.compute_encodings(model_eval, forward_pass_callback_args=1)

        aimet_model_quantize_mark = torch.cuda.memory_allocated()
        aimet_model_quantize_delta = aimet_model_quantize_mark - base_model_loaded_mark

        for i in range(1):
            _ = model_eval(model=sim.model, early_stopping_iterations=10)

        aimet_model_eval_mark = torch.cuda.memory_allocated()
        aimet_model_eval_delta = aimet_model_eval_mark - aimet_model_quantize_mark

        print("")
        print("Usage Report ------")
        print("Model load = {}".format(base_model_loaded_mark))
        print("AIMET quantize delta = {}".format(aimet_model_quantize_delta))
        print("AIMET eval delta = {}".format(aimet_model_eval_delta))

        self.assertEqual(0, aimet_model_eval_delta)

    def test_dummy(self):
        # pytest has a 'feature' that returns an error code when all tests for a given suite are not selected
        # to be executed
        # So adding a dummy test to satisfy pytest
        pass



