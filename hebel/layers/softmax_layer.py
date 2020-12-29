# Copyright (C) 2013  Hannes Bretschneider

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import numpy as np
import cPickle
from pycuda import gpuarray
from pycuda import cumath
from math import sqrt
from .. import sampler, memory_pool
from .top_layer import TopLayer
from ..pycuda_ops import eps, linalg
from ..pycuda_ops.elementwise import sign, nan_to_zeros, substract_matrix
from ..pycuda_ops.reductions import matrix_sum_out_axis
from ..pycuda_ops.matrix import add_vec_to_mat
from ..pycuda_ops.softmax import softmax, cross_entropy


class SoftmaxLayer(TopLayer):
    r""" A multiclass classification layer, using
    cross-entropy loss function and softmax activations.

    **Parameters:**
    
    n_in : integer
        Number of input units.

    n_out : integer
        Number of output units (classes).

    parameters : array_like of ``GPUArray``
        Parameters used to initialize the layer. If this is omitted,
        then the weights are initialized randomly using *Bengio's rule*
        (uniform distribution with scale :math:`4 \cdot \sqrt{6 /
        (\mathtt{n\_in} + \mathtt{n\_out})}`) and the biases are
        initialized to zero. If ``parameters`` is given, then is must
        be in the form ``[weights, biases]``, where the shape of
        weights is ``(n_in, n_out)`` and the shape of ``biases`` is
        ``(n_out,)``. Both weights and biases must be ``GPUArray``.
    
    weights_scale : float, optional
        If ``parameters`` is omitted, then this factor is used as
        scale for initializing the weights instead of *Bengio's rule*.

    l1_penalty_weight : float, optional
        Weight used for L1 regularization of the weights.

    l2_penalty_weight : float, optional
       Weight used for L2 regularization of the weights.

    lr_multiplier : float, optional
        If this parameter is omitted, then the learning rate for the
        layer is scaled by :math:`2 / \sqrt{\mathtt{n\_in}}`. You may
        specify a different factor here.

    test_error_fct : {``class_error``, ``kl_error``, ``cross_entropy_error``}, optional
        Which error function to use on the test set. Default is
        ``class_error`` for classification error. Other choices are
        ``kl_error``, the Kullback-Leibler divergence, or
        ``cross_entropy_error``.

    **See also:**

    :class:`hebel.layers.LogisticLayer`,
    :class:`hebel.models.NeuralNet`,
    :class:`hebel.models.NeuralNetRegression`,
    :class:`hebel.layers.LinearRegressionLayer`

    **Examples**::

        # Use the simple initializer and initialize with random weights
        softmax_layer = SoftmaxLayer(1000, 10)

        # Sample weights yourself, specify an L1 penalty, and don't
        # use learning rate scaling
        import numpy as np
        from pycuda import gpuarray

        n_in = 1000
        n_out = 10
        weights = gpuarray.to_gpu(.01 * np.random.randn(n_in, n_out))
        biases = gpuarray.to_gpu(np.zeros((n_out,)))
        softmax_layer = SoftmaxLayer(n_in, n_out,
                                       parameters=(weights, biases),
                                       l1_penalty_weight=.1,
                                       lr_multiplier=1.)
    """

    n_parameters = 2

    def __init__(self, n_in, n_out,
                 parameters=None,
                 weights_scale=None,
                 l1_penalty_weight=0., l2_penalty_weight=0.,
                 lr_multiplier=None,
                 test_error_fct='class_error'):

        # Initialize weight using Bengio's rule
        self.weights_scale = 4 * sqrt(6. / (n_in + n_out)) \
                             if weights_scale is None \
                                else weights_scale

        if parameters is not None:
            self.W, self.b = parameters
        else:
            self.W = gpuarray.empty((n_in, n_out), dtype=np.float32,
                                    allocator=memory_pool.allocate)
            sampler.fill_uniform(self.W)
            self.W = self.weights_scale * (self.W - .5)

            self.b = gpuarray.zeros((n_out,), dtype=np.float32)

        self.n_in = n_in
        self.n_out = n_out

        self.test_error_fct = test_error_fct

        self.l1_penalty_weight = l1_penalty_weight
        self.l2_penalty_weight = l2_penalty_weight

        self.lr_multiplier = 2 * [1. / np.sqrt(n_in, dtype=np.float32)] \
          if lr_multiplier is None else lr_multiplier

    @property
    def architecture(self):
        return {'class': self.__class__,
                'n_in': self.n_in,
                'n_out': self.n_out}

    def feed_forward(self, input_data, prediction=False):
        """Propagate forward through the layer.

        **Parameters:**

        input_data : ``GPUArray``
            Inpute data to compute activations for.

        prediction : bool, optional
            Whether to use prediction model. Only relevant when using
            dropout. If true, then weights are multiplied by
            1 - dropout if the layer uses dropout.

        **Returns:**
        
        activations : ``GPUArray``
            The activations of the output units.
        """

        if input_data.shape[1] != self.W.shape[0]:
            raise ValueError('Number of outputs from previous layer (%d) '
                            'does not match number of inputs to this layer (%d)' %
                             (input_data.shape[1], self.W.shape[0]))

        lin_activations = linalg.dot(input_data, self.W)
        lin_activations = add_vec_to_mat(lin_activations, self.b, inplace=True)
        activations = softmax(lin_activations)

        return activations

    def backprop(self, input_data, targets,
                 cache=None):
        """ Backpropagate through the logistic layer.

        **Parameters:**

        input_data : ``GPUArray``
            Inpute data to compute activations for.

        targets : ``GPUArray``
            The target values of the units.

        cache : list of ``GPUArray``
            Cache obtained from forward pass. If the cache is
            provided, then the activations are not recalculated.

        **Returns:**

        gradients : tuple of ``GPUArray``
            Gradients with respect to the weights and biases in the
            form ``(df_weights, df_biases)``.

        df_input : ``GPUArray``
            Gradients with respect to the input.
        """

        if cache is not None:
            activations = cache
        else:
            activations = self.feed_forward(input_data, prediction=False)

        if activations.shape != targets.shape:
            raise ValueError('Activations (shape = %s) and targets (shape = %s) are different sizes' %
                             (activations.shape, targets.shape))

        delta = substract_matrix(activations, targets)
        nan_to_zeros(delta, delta)

        # Gradient wrt weights
        df_W = linalg.dot(input_data, delta, transa='T')
        # Gradient wrt bias
        df_b = matrix_sum_out_axis(delta, 0)

        # Gradient wrt input
        df_input = linalg.dot(delta, self.W, transb='T')

        # L1 penalty
        if self.l1_penalty_weight:
            df_W += self.l1_penalty_weight * sign(self.W)

        # L2 penalty
        if self.l2_penalty_weight:
            df_W += self.l2_penalty_weight * self.W

        return (df_W, df_b), df_input

    def test_error(self, input_data, targets, average=True,
                   cache=None, prediction=True):
        """Compute the test error function given some data and targets.

        Uses the error function defined in
        :class:`SoftmaxLayer.test_error_fct`, which may be different
        from the cross-entropy error function used for
        training'. Alternatively, the other test error functions may
        be called directly.

        **Parameters:**

        input_data : ``GPUArray``
            Inpute data to compute the test error function for.

        targets : ``GPUArray``
            The target values of the units.

        average : bool
            Whether to divide the value of the error function by the
            number of data points given.

        cache : list of ``GPUArray``
            Cache obtained from forward pass. If the cache is
            provided, then the activations are not recalculated.

        prediction : bool, optional
            Whether to use prediction model. Only relevant when using
            dropout. If true, then weights are multiplied by
            1 - dropout if the layer uses dropout.

        **Returns:**
        test_error : float
        """    
        if self.test_error_fct == 'class_error':
            test_error = self.class_error
        elif self.test_error_fct == 'kl_error':
            test_error = self.kl_error
        elif self.test_error_fct == 'cross_entropy_error':
            test_error = self.cross_entropy_error
        else:
            raise ValueError('unknown test error function "%s"'
                             % self.test_error_fct)

        return test_error(input_data, targets, average,
                          cache, prediction)

    def cross_entropy_error(self, input_data, targets, average=True,
                            cache=None, prediction=False):
        """ Return the cross entropy error
        """

        if cache is not None:
            activations = cache
        else:
            activations = \
              self.feed_forward(input_data, prediction=prediction)

        loss = cross_entropy(activations, targets)

        if average: loss /= targets.shape[0]
        return loss.get()
        
    train_error = cross_entropy_error

    def class_error(self, input_data, targets, average=True,
                    cache=None, prediction=False):
        """ Return the classification error rate
        """

        if cache is not None:
            activations = cache
        else:
            activations = \
              self.feed_forward(input_data, prediction=prediction)

        targets = targets.get().argmax(1)
        class_error = np.sum(activations.get().argmax(1) != targets)

        if average: class_error = float(class_error) / targets.shape[0]
        return class_error

    def kl_error(self, input_data, targets, average=True,
                 cache=None, prediction=True):
        """ The KL divergence error
        """

        if cache is not None:
            activations = cache
        else:
            activations = \
              self.feed_forward(input_data, prediction=prediction)

        targets_non_nan = gpuarray.empty_like(targets)
        nan_to_zeros(targets, targets_non_nan)
        kl_error = gpuarray.sum(targets_non_nan *
                                (cumath.log(targets_non_nan + eps) -
                                 cumath.log(activations + eps)))
        if average:
            kl_error /= targets.shape[0]
        return kl_error.get()
