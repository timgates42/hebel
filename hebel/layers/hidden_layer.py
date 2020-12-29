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
from itertools import izip
from pycuda import gpuarray
from pycuda.gpuarray import GPUArray
from math import sqrt
from .. import sampler, memory_pool
from ..pycuda_ops import eps
from ..pycuda_ops import linalg
from ..pycuda_ops.elementwise import sigmoid, df_sigmoid, \
     tanh, df_tanh, relu, df_relu, linear, df_linear, \
     sample_dropout_mask, apply_dropout_mask, sign, mult_matrix
from ..pycuda_ops.matrix import add_vec_to_mat
from ..pycuda_ops.reductions import matrix_sum_out_axis


class HiddenLayer(object):
    r"""A fully connected hidden layer.

    The ``HiddenLayer`` class represents a fully connected hidden
    layer that can use a multitude of activation functions and supports
    dropout, L1, and L2 regularization.

    **Parameters:**

    n_in : integer
        Number of input units.

    n_out : integer
        Number of hidden units.

    activation_function : {``sigmoid``, ``tanh``, ``relu``, ``linear``}, optional
        Which activation function to use. Default is sigmoid.

    dropout : float in [0, 1)
        Probability of dropping out each hidden unit during training. Default is 0.

    parameters : array_like of ``GPUArray``
        Parameters used to initialize the layer. If this is omitted,
        then the weights are initialized randomly using *Bengio's rule*
        (uniform distribution with scale :math:`4 \cdot \sqrt{6 /
        (\mathtt{n\_in} + \mathtt{n\_out})}` if using sigmoid
        activations and :math:`\sqrt{6 / (\mathtt{n\_in} +
        \mathtt{n\_out})}` if using tanh, relu, or linear activations)
        and the biases are initialized to zero. If ``parameters`` is
        given, then is must be in the form ``[weights, biases]``,
        where the shape of weights is ``(n_in, n_out)`` and the shape
        of ``biases`` is ``(n_out,)``. Both weights and biases must be
        ``GPUArray``.
    
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

    **Examples**::

        # Use the simple initializer and initialize with random weights
        hidden_layer = HiddenLayer(500, 10000)

        # Sample weights yourself, specify an L1 penalty, and don't
        # use learning rate scaling
        import numpy as np
        from pycuda import gpuarray

        n_in = 500
        n_out = 1000
        weights = gpuarray.to_gpu(.01 * np.random.randn(n_in, n_out))
        biases = gpuarray.to_gpu(np.zeros((n_out,)))
        hidden_layer = HiddenLayer(n_in, n_out,
                                   parameters=(weights, biases),
                                   l1_penalty_weight=.1,
                                   lr_multiplier=1.)

    """
    is_master_layer = True
    n_parameters = 2
    W = None
    b = None

    def __init__(self, n_in, n_units,
                 activation_function='sigmoid',
                 dropout=0.,
                 parameters=None,
                 weights_scale=None,
                 l1_penalty_weight=0.,
                 l2_penalty_weight=0.,
                 lr_multiplier=None):

        self._set_activation_fct(activation_function)

        if weights_scale is None:
            self._set_weights_scale(activation_function, n_in, n_units)
        else:
            self.weights_scale = weights_scale

        if parameters is not None:
            if isinstance(parameters, basestring):
                self.parameters = cPickle.loads(open(parameters))
            else:
                self.W, self.b = parameters
        else:
            self.W = gpuarray.empty((n_in, n_units), dtype=np.float32,
                                    allocator=memory_pool.allocate)
            sampler.fill_uniform(self.W)
            self.W = self.weights_scale * (self.W -.5)

            self.b = gpuarray.zeros((n_units,), dtype=np.float32,
                                    allocator=memory_pool.allocate)

        assert self.W.shape == (n_in, n_units)
        assert self.b.shape == (n_units,)

        self.n_in = n_in
        self.n_units = n_units

        self.lr_multiplier = lr_multiplier if lr_multiplier is not None else \
            2 * [1. / np.sqrt(self.n_in, dtype=np.float32)]

        self.l1_penalty_weight = l1_penalty_weight
        self.l2_penalty_weight = l2_penalty_weight

        # This line is for backward compatibility only; dropout was formerly a bool
        if isinstance(dropout, bool): dropout = 0.5 if dropout else 0
        
        self.dropout = float(dropout)
        assert 0 <= self.dropout < 1

    @property
    def parameters(self):
        """Return a tuple ``(weights, biases)``"""
        return (self.W, self.b)

    @parameters.setter
    def parameters(self, value):
        """Update the parameters. ``value`` must have the shape
        ``(weights, biases)``"""
        self.W = value[0] if isinstance(value[0], GPUArray) else \
          gpuarray.to_gpu(value[0])
        self.b = value[1] if isinstance(value[0], GPUArray) else \
          gpuarray.to_gpu(value[1])

    def update_parameters(self, values, stream=None):
        assert len(values) == self.n_parameters

        for (param, (gparam, mult)) \
            in izip((self.W, self.b), values):
            param._axpbyz(1., gparam, mult, param,
                          stream=stream)

    @property
    def architecture(self):
        """Returns a dictionary describing the architecture of the layer."""
        arch = {'class': self.__class__,
                'n_in': self.n_in,
                'n_units': self.n_units,
                'activation_function': self.activation_function
                if hasattr(self, 'activation_function') else None}
        return arch

    @staticmethod
    def _resolve_activation_fct(activation_function):
        if activation_function == 'sigmoid':
            f = sigmoid
            df = df_sigmoid
        elif activation_function == 'tanh':
            f = tanh
            df = df_tanh
        elif activation_function == 'relu':
            f = relu
            df = df_relu
        elif activation_function == 'linear':
            f = linear
            df = df_linear
        else:
            raise ValueError

        return f, df

    def _set_activation_fct(self, activation_function):
        self.activation_function = activation_function
        self.f, self.df = self._resolve_activation_fct(activation_function)

    def _set_weights_scale(self, activation_function, n_in, n_units):
        if activation_function in ('tanh', 'relu', 'linear'):
            self.weights_scale = sqrt(6. / (n_in + n_units))
        elif activation_function == 'sigmoid':
            self.weights_scale = 4 * sqrt(6. / (n_in + n_units))
        else:
            raise ValueError

    @property
    def l1_penalty(self):
        return self.l1_penalty_weight * gpuarray.sum(abs(self.W)).get()

    @property
    def l2_penalty(self):
        return self.l2_penalty_weight * .5 * gpuarray.sum(self.W ** 2.).get()

    def feed_forward(self, input_data, prediction=False):
        """Propagate forward through the layer

        **Parameters:**

        input_data : ``GPUArray``
            Input data to compute activations for.

        prediction : bool, optional
            Whether to use prediction model. Only relevant when using
            dropout. If true, then weights are multiplied by
            1 - dropout if the layer uses dropout.

        **Returns:**
        
        activations : ``GPUArray``
            The activations of the hidden units.
        """

        if input_data.shape[1] != self.W.shape[0]:
            raise ValueError('Number of outputs from previous layer (%d) '
                            'does not match number of inputs to this layer (%d)' %
                             (input_data.shape[1], self.W.shape[0]))

        activations = linalg.dot(input_data, self.W)
        activations = add_vec_to_mat(activations, self.b, inplace=True)

        self.f(activations)

        if self.dropout > 0:
            if prediction:
                activations *= 1 - self.dropout
            else:
                dropout_mask = sample_dropout_mask(activations, self.dropout)
                return activations, dropout_mask

        return (activations,)

    def backprop(self, input_data, df_output, cache=None):
        """ Backpropagate through the hidden layer

        **Parameters:**

        input_data : ``GPUArray``
            Input data to compute activations for.

        df_output : ``GPUArray``
            Gradients with respect to the activations of this layer
            (received from the layer above).

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

        # Get cache if it wasn't provided
        if cache is None:
            cache = self.feed_forward(input_data,
                                      prediction=False)

        if len(cache) == 2:
            activations, dropout_mask = cache
        else:
            activations = cache[0]

        # Multiply the binary mask with the incoming gradients
        if self.dropout > 0 and dropout_mask is not None:
            apply_dropout_mask(df_output, dropout_mask)

        # Get gradient wrt activation function
        df_activations = self.df(activations)
        delta = mult_matrix(df_activations, df_output)

        # Gradient wrt weights
        df_W = linalg.dot(input_data, delta, transa='T')
        # Gradient wrt bias
        df_b = matrix_sum_out_axis(delta, 0)
        # Gradient wrt inputs
        df_input = linalg.dot(delta, self.W, transb='T')

        # L1 weight decay
        if self.l1_penalty_weight:
            df_W += self.l1_penalty_weight * sign(self.W)

        # L2 weight decay
        if self.l2_penalty_weight:
            df_W += self.l2_penalty_weight * self.W

        return (df_W, df_b), df_input
