'''This module contains the elements needed to set up the listen attend and
spell network.'''

from copy import copy
import sys
import numpy as np
import tensorflow as tf
from tensorflow.python.ops import rnn_cell
from tensorflow.python.ops.rnn_cell import RNNCell

# we are currenly in neuralnetworks, add it to the path.
sys.path.append("neuralnetworks")
from nnet_layer import BlstmLayer
from nnet_layer import PyramidalBlstmLayer
from nnet_layer import FFLayer
from nnet_activations import TfWrapper

from IPython.core.debugger import Tracer; debug_here = Tracer();

#disable the too few public methods complaint
# pylint: disable=R0903

class Listener(object):
    """
    A set of pyramidal blstms, which compute high level audio features.
    """
    def __init__(self, blstm_settings, plstm_settings, plstm_layer_no,
                 output_dim):
        """ Initialize the listener.
        """
        self.output_dim = output_dim
        #the Listerner foundation is a classical bidirectional Long Short
        #term mermory layer.
        blstm_settings.name = 'blstm_layer0'
        self.blstm_layer = BlstmLayer(blstm_settings)
        #on top of are three pyramidal BLSTM layers.
        self.plstms = []
        for layer_count in range(plstm_layer_no):
            plstm_settings.name = 'plstm_layer_' + str(layer_count)
            if (layer_count+1) == len(self.plstms):
                plstm_settings.output_dim = output_dim
            self.plstms.append(PyramidalBlstmLayer(plstm_settings))

    def __call__(self, input_features, sequence_lengths):
        """ Compute the output of the listener function. """
        #compute the base layer blstm output.
        hidden_values = self.blstm_layer(input_features, sequence_lengths)
        #move on to the plstm ouputs.
        for plstm_layer in self.plstms:
            hidden_values = plstm_layer(hidden_values)
        return hidden_values


class AttendAndSpellCell(RNNCell):
    """
    Define an attend and Spell Cell. This cell takes the high level features
    as input. During training the groundtrouth values are fed into the network
    as well.

    Internal Variables:
              features: (H) the high level features the Listener computed.
         decoder_state: (s_i) ambiguous in the las paper split in two here.
       context_vectors: (c_i) in the paper, found using the
                        attention_context function.
          one_hot_char: (y) one hot encoded input and output char.
    """
    def __init__(self, batch_size, decoder_state_size=42,
                 feedforward_hidden_units=42, feedforward_hidden_layers=4):
        self.feedforward_hidden_units = feedforward_hidden_units
        self.feedforward_hidden_layers = feedforward_hidden_layers
        #the decoder state size must be equal to the RNN size.
        self.dec_state_size = decoder_state_size
        self.high_lvl_features = None

        #----------------------Create Variables-------------------------------#
        # setting up the decider_state, character distribution
        # and context vector variables.
        zero_initializer = tf.constant_initializer(value=0)
        self.decoder_state = tf.get_variable(
            name='current_decoder_state',
            shape=[batch_size, self.dec_state_size],
            initializer=zero_initializer,
            trainable=False)
        # the charater distirbution must initially be the sos token.
        # assuming encoding done as specified in the batch dispenser.
        # 0: ' ', 1: '<', 2:'>', ...
        # initialize to start of sentence token '<' as one hot encoding:
        # 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
        sos = np.zeros(self.las_model.target_label_no)
        sos[1] = 1
        sos_initializer = tf.constant_initializer(sos)
        self.one_hot_char = tf.get_variable(
            name='one_hot_char',
            shape=[batch_size,
                   self.las_model.target_label_no],
            initializer=sos_initializer,
            trainable=False)
        # the dimension of the context vector is determined by the listener
        # output dimension.
        self.context_vector = tf.get_variable(
            name='context_vector',
            shape=[batch_size,
                   self.las_model.listen_output_dim],
            initializer=zero_initializer,
            trainable=False)

        self.scalar_energies = tf.get_variable(
            name='scalar_energies',
            shape=[self.las_model.listen_output_dim, 1],
            initializer=zero_initializer,
            trainable=False)

        self.char_dist_tensor = tf.get_variable(
            name='char_dist_tensor',
            shape=[1, batch_size,
                   self.las_model.target_label_no],
            initializer=sos_initializer,
            trainable=False)
        #--------------------Create network functions-------------------------#
        # TODO: Move outside the cell
        # Feedforward layer custom parameters. Vincent knows more about these.
        activation = None
        activation = TfWrapper(activation, tf.nn.relu)

        state_net_dimension = FFNetDimension(self.dec_state_size,
                                             self.feedforward_hidden_units,
                                             self.feedforward_hidden_units,
                                             self.feedforward_hidden_layers
                                            )

        self.state_net = FeedForwardNetwork(state_net_dimension,
                                            activation, name='state_net')
        # copy the state net any layer settings
        # => all properties, which are not explicitly changed
        # stay the same.
        featr_net_dimension = copy(state_net_dimension)
        featr_net_dimension.input_dim = self.las_model.listen_output_dim
        self.featr_net = FeedForwardNetwork(featr_net_dimension,
                                            activation, name='featr_net')

        self.pre_context_rnn = RNN(self.dec_state_size,
                                   name='pre_context_rnn')
        self.post_context_rnn = RNN(self.dec_state_size,
                                    name='post_context_rnn')

        char_net_dimension = FFNetDimension(
            input_dim=self.dec_state_size
            +         self.las_model.listen_output_dim,
            output_dim=self.las_model.target_label_no,
            num_hidden_units=self.feedforward_hidden_units,
            num_hidden_layers=self.feedforward_hidden_layers)

        self.char_net = FeedForwardNetwork(char_net_dimension,
                                           activation,
                                           name='char_net')



    def set_features(self, high_lvl_features):
        ''' Set the features when available, storing the features in the
            object makes the cell call simpler.'''
        self.high_lvl_features = high_lvl_features

    def __call__(self, cell_input, state, scope=None):
        """
        Do the computations for a single unrolling of the attend and
        spell network.
        During training make sure training_char_input contains
        valid groundtrouth values.
        """
        groundtruth_char = cell_input
        pre_context_states, post_context_states, one_hot_char, \
            context_vector = state

        if self.high_lvl_features is None:
            raise AttributeError("Features must be set.")

        #TODO in training mode pick the last output sometimes.
        if groundtruth_char is not None:
            one_hot_char = groundtruth_char


        #s_i = RNN(s_(i-1), y_(i-1), c_(i-1))
        #Dimensions:   decoder_state_size , 42
        #            + alphabet_size,       31
        #            + listener_output_dim, 64
        #                                   137
        rnn_input = tf.concat(1, [pre_context_states,
                                  one_hot_char,
                                  context_vector])

        pre_context_out, pre_context_states = \
                self.pre_context_rnn(rnn_input, pre_context_states)

        #for loop runing trough the high level features.
        #assert len(high level features) == U.
        scalar_energy_lst = []
        for feat_vec in high_lvl_features:
            ### compute the attention context. ###
            # e_(i,u) = psi(s_i)^T * phi(h_u)
            phi = self.state_net(pre_context_out)
            psi = self.featr_net(feat_vec)
            scalar_energy = tf.reduce_sum(psi*phi, reduction_indices=1,
                                          name='dot_sum')

            scalar_energy_lst.append(scalar_energy)
        # alpha = softmax(e_(i,u))
        scalar_energy_tensor = tf.convert_to_tensor(
            scalar_energy_lst)
        #Alpha has the same shape as the scalar_energy_tensor
        alpha = tf.nn.softmax(scalar_energy_tensor)

        ### find the context vector. ###
        # c_i = sum(alpha*h_i)
        context_vector = 0*context_vector
        for t in range(0, len(high_lvl_features)):
            #reshaping from (batch_size,) to (batch_size,1) is
            #needed for broadcasting.
            current_alpha = tf.reshape(alpha[t, :],
                                       [self.las_model.batch_size,
                                        1])
            context_vector = (context_vector
                              + current_alpha*self.high_lvl_features[t])

        #construct the char_net input
        #TODO: add the post_context RNN.

        char_net_input = tf.concat(1, [pre_context_out, context_vector])
        logits = self.char_net(char_net_input)

        #TODO: figure out over which dimension to run the argmax.
        #max_pos = tf.argmax(logits, 0, name='choose_max')

        #one = tf.get_variable('one', shape=(),
        #                      initializer=tf.constant_initializer(1))
        #one_hot_char = 0*logits
        #one_hot_char = tf.scatter_update(1, one)
        #TODO: remove and use above.
        one_hot_char = tf.nn.softmax(logits)

        attend_and_spell_states = (pre_context_states, post_context_states,
            one_hot_char, context_vector)

        return logits, attend_and_spell_states



class RNN(object):
    """
    Set up the RNN network which computes the decoder state.
    """
    def __init__(self, lstm_dim, name):
        self.name = name
        self.layer_number = 2
        #create the two required LSTM blocks.
        self.blocks = []
        for _ in range(0, self.layer_number):
            self.blocks.append(rnn_cell.LSTMCell(lstm_dim,
                                                 use_peepholes=True,
                                                 state_is_tuple=False))
        self.wrapped_cells = rnn_cell.MultiRNNCell(self.blocks,
                                                   state_is_tuple=False)
        self.reuse = None

    def get_zero_states(self, batch_size, dtype):
        """ Get a list filled with zero states which can be used
            to start up the unrolled LSTM computations."""
        return self.wrapped_cells.zero_state(batch_size, dtype)

    def __call__(self, single_input, state):
        """
        Computes the RNN outputs for a single input. This CALL MUST BE
        UNROLLED MANUALLY.
        """
        #assertion only works if state_is_touple is set to true.
        #assert len(state) == len(self.blocks)

        with tf.variable_scope(self.name + '_call', reuse=self.reuse):
            output = self.wrapped_cells(single_input, state)

        if self.reuse is None:
            self.reuse = True

        return output

class FFNetDimension(object):
    """ Class containing the information to create Feedforward nets. """
    def __init__(self, input_dim, output_dim, num_hidden_units,
                 num_hidden_layers):
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.num_hidden_units = num_hidden_units
        self.num_hidden_layers = num_hidden_layers

class FeedForwardNetwork(object):
    """ A class defining the feedforward MLP networks used to compute the
        scalar energy values required for the attention mechanism.
    """
    def __init__(self, dimension, activation, name):
        #store the settings
        self.dimension = dimension
        self.activation = activation
        self.name = name
        self.reuse = None

        #create the layers
        self.layers = [None]*(dimension.num_hidden_layers + 1)
        #input layer
        self.layers[0] = FFLayer(dimension.num_hidden_units, activation)
        #hidden layers
        for k in range(1, len(self.layers)-1):
            self.layers[k] = FFLayer(dimension.num_hidden_units, activation)
        #output layer
        self.layers[-1] = FFLayer(dimension.output_dim, activation)

    def __call__(self, states_or_features):
        hidden = states_or_features
        for i, layer in enumerate(self.layers):
            hidden = layer(hidden, scope=(self.name + '/' + str(i)),
                           reuse=(self.reuse))
        #set reuse to true after the variables have been created in the first
        #call.
        if self.reuse is None:
            self.reuse = True
        return hidden
