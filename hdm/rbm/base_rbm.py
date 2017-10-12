import numpy as np
import tensorflow as tf
from tensorflow.core.framework import summary_pb2

from hdm.base import TensorFlowModel, run_in_tf_session
from hdm.utils import (make_list_from, batch_iter, epoch_iter,
                       write_during_training)
from hdm.utils.testing import assert_len, assert_shape


class BaseRBM(TensorFlowModel):
    """
    A generic implementation of RBM with k-step
    Contrastive Divergence (CD-k) learning algorithm.

    Parameters
    ----------
    W_init : float or (n_visible, n_hidden) iterable
        Weight matrix initialization. If float, initialize from zero-centered
        Gaussian with this standard deviation. If iterable, initialize from it.
    vb_init, hb_init : float or iterable
        Visible and hidden unit bias(es).
    n_gibbs_steps : positive int
        Number of Gibbs updates per iteration.
    learning_rate, momentum : positive float or iterable
        Gradient descent parameters. Values are updated after each epoch.
    max_epoch : positive int
        Train till this epoch.
    batch_size : positive int
        Input batch size for training.
    l2 : non-negative float
        l2 weight decay coefficient.
    sample_v_states, sample_h_states : bool
        Whether to sample visible/hidden states, or to use probabilities
        w/o sampling. Note that data driven states for hidden units will
        be sampled regardless of the provided parameters.
        `transform` will also use probabilities/means of hidden units.
    dropout : None or float in [0; 1]
        If float, interpreted as probability of visible units being on.
    sparsity_target : float in (0; 1)
        Desired probability of hidden activation.
    sparsity_cost : positive float
        Controls the amount of sparsity penalty.
    sparsity_damping : float in (0; 1)
        Decay rate for hidden activations probs.
    dbm_first, dbm_last : bool
        Indicators of whether RBM is first or last in a stack of RBMs used
        for DBM pre-training to address "double counting evidence" problem [4].
    metrics_config : dict
        Parameters that controls which metrics and how often they are computed.
        Possible (optional) commands:
        * l2_loss : bool, default False
            Whether to compute weight decay penalty.
        * msre : bool, default False
            Whether to compute MSRE = mean squared reconstruction error.
        * pll : bool, default False
            Whether to compute pseudo-loglikelihood estimation. Only makes sense
            to compute for binary visible units (BernoulliRBM, MultinomialRBM).
        * feg : bool, default False
            Whether to compute free energy gap.
        * l2_loss_fmt : str, default '.2e'
        * msre_fmt : str, default '.4f'
        * pll_fmt : str, default '.3f'
        * feg_fmt : str, default '.2f'
        * train_metrics_every_iter : int, default 10
        * val_metrics_every_epoch : int, default 1
        * feg_every_epoch : int, default 2
        * n_batches_for_feg : int, default 10

    References
    ----------
    [1] Goodfellow I. et. al. "Deep Learning".
    [2] Hinton, G. "A Practical Guide to Training Restricted Boltzmann
        Machines" UTML TR 2010-003
    [3] Restricted Boltzmann Machines (RBMs), Deep Learning Tutorial
        url: http://deeplearning.net/tutorial/rbm.html
    [4] Salakhutdinov, R. and Hinton, G. (2009). Deep Boltzmann machines.
        In AISTATS 2009
    """
    def __init__(self,
                 n_visible=784, v_layer_cls=None, v_layer_params=None,
                 n_hidden=256, h_layer_cls=None, h_layer_params=None,
                 W_init=0.01, vb_init=0., hb_init=0., n_gibbs_steps=1,
                 learning_rate=0.01, momentum=0.9, max_epoch=10, batch_size=10, l2=1e-4,
                 sample_v_states=False, sample_h_states=True, dropout=None,
                 sparsity_target=0.1, sparsity_cost=0., sparsity_damping=0.9,
                 dbm_first=False, dbm_last=False,
                 metrics_config=None, verbose=True, save_after_each_epoch=True,
                 display_filters=30, filter_shape=(28, 28),
                 display_hidden_activations=25,
                 model_path='rbm_model/', *args, **kwargs):
        super(BaseRBM, self).__init__(model_path=model_path, *args, **kwargs)
        self.n_visible = n_visible
        self.n_hidden = n_hidden

        v_layer_params = v_layer_params or {}
        v_layer_params.setdefault('n_units', self.n_visible)
        v_layer_params.setdefault('tf_dtype', self._tf_dtype)
        h_layer_params = h_layer_params or {}
        h_layer_params.setdefault('n_units', self.n_hidden)
        h_layer_params.setdefault('tf_dtype', self._tf_dtype)
        self._v_layer = v_layer_cls(**v_layer_params)
        self._h_layer = h_layer_cls(**h_layer_params)

        self.W_init = W_init
        if hasattr(self.W_init, '__iter__'):
            self.W_init = np.asarray(self.W_init)
            assert_shape(self, 'W_init', (self.n_visible, self.n_hidden))

        # Visible biases can be initialized with list of values,
        # because it is often helpful to initialize i-th visible bias
        # with value log(p_i / (1 - p_i)), p_i = fraction of training
        # vectors where i-th unit is on, as proposed in [2]
        self.vb_init = vb_init
        if hasattr(self.vb_init, '__iter__'):
            self.vb_init = np.asarray(self.vb_init)
            assert_len(self, 'vb_init', self.n_visible)

        self.hb_init = hb_init
        if hasattr(self.hb_init, '__iter__'):
            self.hb_init = np.asarray(self.hb_init)
            assert_len(self, 'hb_init', self.n_hidden)

        # these can be set in `init_from` method
        self._dW_init = None
        self._dvb_init = None
        self._dhb_init = None

        self.n_gibbs_steps = n_gibbs_steps
        self.learning_rate = make_list_from(learning_rate)
        self.momentum = make_list_from(momentum)
        self.max_epoch = max_epoch
        self.batch_size = batch_size
        self.l2 = l2

        # According to [2], the training goes less noisy and slightly faster, if
        # sampling used for states of hidden units driven by the data, and probabilities
        # for ones driven by reconstructions, and if probabilities (means) used for visible units,
        # both driven by data and by reconstructions. It is therefore recommended to set
        # these parameter to False (default).
        self.sample_h_states = sample_h_states
        self.sample_v_states = sample_v_states
        self.dropout = dropout

        self.sparsity_target = sparsity_target
        self.sparsity_cost = sparsity_cost
        self.sparsity_damping = sparsity_damping

        self.dbm_first = dbm_first
        self.dbm_last = dbm_last

        self.metrics_config = metrics_config or {}
        self.metrics_config.setdefault('l2_loss', False)
        self.metrics_config.setdefault('msre', False)
        self.metrics_config.setdefault('pll', False)
        self.metrics_config.setdefault('feg', False)
        self.metrics_config.setdefault('l2_loss_fmt', '.2e')
        self.metrics_config.setdefault('msre_fmt', '.4f')
        self.metrics_config.setdefault('pll_fmt', '.3f')
        self.metrics_config.setdefault('feg_fmt', '.2f')
        self.metrics_config.setdefault('train_metrics_every_iter', 10)
        self.metrics_config.setdefault('val_metrics_every_epoch', 1)
        self.metrics_config.setdefault('feg_every_epoch', 2)
        self.metrics_config.setdefault('n_batches_for_feg', 10)
        self._metrics_names_map={
            'feg': 'free_energy_gap',
            'l2_loss': 'l2_loss',
            'msre': 'mean_squared_reconstruction_error',
            'pll': 'pseudo_loglikelihood'
        }
        self._train_metrics_names = ('l2_loss', 'msre', 'pll')
        self._train_metrics_map = {}
        self._val_metrics_names = ('msre', 'pll')
        self._val_metrics_map = {}

        self.verbose = verbose
        self.save_after_each_epoch = save_after_each_epoch

        self.display_filters = display_filters
        self.filter_shape = filter_shape
        self.display_hidden_activations = display_hidden_activations

        # current epoch and iteration
        self.epoch = 0
        self.iter = 0

        # tf constants
        self._n_visible = None
        self._n_hidden = None
        self._l2 = None
        self._dropout = None
        self._dbm_first = None
        self._dbm_last = None
        self._propup_multiplier = None
        self._propdown_multiplier = None

        # tf input data
        self._learning_rate = None
        self._momentum = None
        self._X_batch = None

        # tf vars
        self._W = None
        self._hb = None
        self._vb = None

        self._dW = None
        self._dhb = None
        self._dvb = None

        # tf operations
        self._train_op = None
        self._transform_op = None
        self._msre = None
        self._pll = None
        self._free_energy_op = None

    def _make_constants(self):
        with tf.name_scope('constants'):
            self._n_visible = tf.constant(self.n_visible, dtype=tf.int32, name='n_visible')
            self._n_hidden = tf.constant(self.n_hidden, dtype=tf.int32, name='n_hidden')
            self._l2 = tf.constant(self.l2, dtype=self._tf_dtype, name='l2_coef')
            if self.dropout is not None:
                self._dropout = tf.constant(self.dropout, dtype=self._tf_dtype, name='dropout_prob')
            self._sparsity_target = tf.constant(self.sparsity_target, dtype=self._tf_dtype, name='sparsity_target')
            self._sparsity_cost = tf.constant(self.sparsity_cost, dtype=self._tf_dtype, name='sparsity_cost')
            self._sparsity_damping = tf.constant(self.sparsity_damping, dtype=self._tf_dtype, name='sparsity_damping')

            self._dbm_first = tf.constant(self.dbm_first, dtype=tf.bool, name='is_dbm_first')
            self._dbm_last = tf.constant(self.dbm_last, dtype=tf.bool, name='is_dbm_last')
            t = tf.constant(1., dtype=self._tf_dtype, name="1")
            t1 = tf.cast(self._dbm_first, dtype=self._tf_dtype)
            self._propup_multiplier = tf.identity(tf.add(t1, t), name='propup_multiplier')
            t2 = tf.cast(self._dbm_last, dtype=self._tf_dtype)
            self._propdown_multiplier = tf.identity(tf.add(t2, t), name='propdown_multiplier')

    def _make_placeholders(self):
        with tf.name_scope('input_data'):
            self._learning_rate = tf.placeholder(self._tf_dtype, [], name='learning_rate')
            self._momentum = tf.placeholder(self._tf_dtype, [], name='momentum')
            self._X_batch = tf.placeholder(self._tf_dtype, [None, self.n_visible], name='X_batch')

    def _make_vars(self):
        with tf.name_scope('weights'):
            if hasattr(self.W_init, '__iter__'):
                W_init = tf.constant(self.W_init, dtype=self._tf_dtype)
            else:
                W_init = tf.random_normal([self._n_visible, self._n_hidden],
                                           mean=0.0, stddev=self.W_init,
                                           seed=self.random_seed, dtype=self._tf_dtype)
            W_init = tf.identity(W_init, name='W_init')

            vb_init = self.vb_init if hasattr(self.vb_init, '__iter__') else\
                      np.repeat(self.vb_init, self.n_visible)

            hb_init = self.hb_init if hasattr(self.hb_init, '__iter__') else\
                      np.repeat(self.hb_init, self.n_hidden)

            vb_init = tf.constant(vb_init, dtype=self._tf_dtype, name='vb_init')
            hb_init = tf.constant(hb_init, dtype=self._tf_dtype, name='hb_init')

            self._W = tf.Variable(W_init, dtype=self._tf_dtype, name='W')
            self._vb = tf.Variable(vb_init, dtype=self._tf_dtype, name='vb')
            self._hb = tf.Variable(hb_init, dtype=self._tf_dtype, name='hb')

            tf.summary.histogram('W', self._W)
            tf.summary.histogram('vb', self._vb)
            tf.summary.histogram('hb', self._hb)

            # visualize filters
            if self.display_filters:
                with tf.name_scope('filters_visualization'):
                    W_display = tf.reshape(self._W, [self.filter_shape[0],
                                                     self.filter_shape[1], self.n_hidden, 1])
                    W_display = tf.transpose(W_display, [2, 0, 1, 3])
                    tf.summary.image('W_filters', W_display, max_outputs=self.display_filters)

        with tf.name_scope('weights_increments'):
            dW_init = tf.constant(self._dW_init, dtype=self._tf_dtype) if self._dW_init is not None else\
                      tf.zeros([self._n_visible, self._n_hidden], dtype=self._tf_dtype)
            dvb_init = tf.constant(self._dvb_init, dtype=self._tf_dtype) if self._dvb_init is not None else \
                       tf.zeros([self._n_visible], dtype=self._tf_dtype)
            dhb_init = tf.constant(self._dhb_init, dtype=self._tf_dtype) if self._dhb_init is not None else \
                       tf.zeros([self._n_hidden], dtype=self._tf_dtype)

            self._dW = tf.Variable(dW_init, name='dW')
            self._dvb = tf.Variable(dvb_init, name='dvb')
            self._dhb = tf.Variable(dhb_init, name='dhb')

            tf.summary.histogram('dW', self._dW)
            tf.summary.histogram('dvb', self._dvb)
            tf.summary.histogram('dhb', self._dhb)

        with tf.name_scope('hidden_activations_probs'):
            self._q_means = tf.Variable(tf.zeros([self._n_hidden], dtype=self._tf_dtype), name='q_means')

    def _propup(self, v):
        with tf.name_scope('prop_up'):
            t = tf.matmul(v, self._W)
        return t

    def _propdown(self, h):
        with tf.name_scope('prop_down'):
            t = tf.matmul(a=h, b=self._W, transpose_b=True)
        return t

    def _means_h_given_v(self, v):
        """Compute means E(h|v)."""
        with tf.name_scope('means_h_given_v'):
            propup = self._propup_multiplier * self._propup(v)
            hb     = self._propup_multiplier * self._hb
            h_means = self._h_layer.activation(x=propup, b=hb)
        return h_means

    def _sample_h_given_v(self, h_means):
        """Sample from P(h|v)."""
        with tf.name_scope('sample_h_given_v'):
            h_samples = self._h_layer.sample(means=h_means)
        return h_samples

    def _means_v_given_h(self, h):
        """Compute means E(v|h)."""
        with tf.name_scope('means_v_given_h'):
            propdown = self._propdown_multiplier * self._propdown(h)
            vb       = self._propdown_multiplier * self._vb
            v_means = self._v_layer.activation(x=propdown, b=vb)
        return v_means

    def _sample_v_given_h(self, v_means):
        """Sample from P(v|h)."""
        with tf.name_scope('sample_v_given_h'):
            v_samples = self._v_layer.sample(means=v_means)
        return v_samples

    def _free_energy(self, v):
        """Compute (average) free energy of a visible vectors `v`."""
        raise NotImplementedError('`free_energy` is not implemented')

    def _make_train_op(self):
        # apply dropout if necessary
        if self.dropout is not None:
            self._X_batch = tf.nn.dropout(self._X_batch, keep_prob=self._dropout)

        # Run Gibbs chain for specified number of steps.
        with tf.name_scope('gibbs_chain'):
            h0_means = self._means_h_given_v(self._X_batch)
            h0_samples = self._sample_h_given_v(h0_means)
            h_means, h_samples = None, None
            v_means, v_samples = None, None
            h_states = h0_samples if self.sample_h_states else h0_means
            v_states = None

            for _ in xrange(self.n_gibbs_steps):
                with tf.name_scope('sweep'):
                    v_states = v_means = self._means_v_given_h(h_states)
                    if self.sample_v_states:
                        v_states = self._sample_v_given_h(v_means)
                    h_states = h_means = self._means_h_given_v(v_states)
                    if self.sample_h_states:
                        h_states = self._sample_h_given_v(h_means)

        # visualize hidden activation means
        if self.display_hidden_activations:
            with tf.name_scope('hidden_activations_visualization'):
                h_means_display = h_means[:, :self.display_hidden_activations]
                h_means_display = tf.cast(h_means_display, tf.float32)
                h_means_display = tf.expand_dims(h_means_display, 0)
                h_means_display = tf.expand_dims(h_means_display, -1)
                tf.summary.image('hidden_activation_means', h_means_display)

        # encoded data, used by the transform method
        with tf.name_scope('transform'):
            transform_op = tf.identity(h_means)
            tf.add_to_collection('transform_op', transform_op)

        # compute gradients estimates (= positive - negative associations)
        with tf.name_scope('grads_estimates'):
            # number of training examples might not be divisible by batch size
            N = tf.cast(tf.shape(self._X_batch)[0], dtype=self._tf_dtype)
            with tf.name_scope('dW'):
                dW_positive = tf.matmul(self._X_batch, h0_means, transpose_a=True)
                dW_negative = tf.matmul(v_states, h_means, transpose_a=True)
                dW = (dW_positive - dW_negative) / N - self._l2 * self._W
            with tf.name_scope('dvb'):
                dvb = tf.reduce_mean(self._X_batch - v_states, axis=0) # == sum / N
            with tf.name_scope('dhb'):
                dhb = tf.reduce_mean(h0_means - h_means, axis=0) # == sum / N

        # apply sparsity targets if needed
        with tf.name_scope('sparsity_targets'):
            q_means = tf.reduce_sum(h_means, axis=0)
            q_update = self._q_means.assign(self._sparsity_damping * self._q_means +\
                                            (1 - self._sparsity_damping) * q_means)
            sparsity_penalty = self._sparsity_cost * (q_update - self._sparsity_target)
            dhb -= sparsity_penalty
            dW  -= sparsity_penalty

        # update parameters
        with tf.name_scope('momentum_updates'):
            with tf.name_scope('dW'):
                dW_update = self._dW.assign(self._learning_rate * (self._momentum * self._dW + dW))
                W_update = self._W.assign_add(dW_update)
            with tf.name_scope('dvb'):
                dvb_update = self._dvb.assign(self._learning_rate * (self._momentum * self._dvb + dvb))
                vb_update = self._vb.assign_add(dvb_update)
            with tf.name_scope('dhb'):
                dhb_update = self._dhb.assign(self._learning_rate * (self._momentum * self._dhb + dhb))
                hb_update = self._hb.assign_add(dhb_update)

        # assemble train_op
        with tf.name_scope('training_step'):
            train_op = tf.group(W_update, vb_update, hb_update)
            tf.add_to_collection('train_op', train_op)

        # compute metrics
        with tf.name_scope('l2_loss'):
            l2_loss = self._l2 * tf.nn.l2_loss(self._W)
            tf.add_to_collection('l2_loss', l2_loss)

        with tf.name_scope('mean_squared_recon_error'):
            msre = tf.reduce_mean(tf.square(self._X_batch - v_means))
            tf.add_to_collection('msre', msre)

        # Since reconstruction error is fairly poor measure of performance,
        # as this is not what CD-k learning algorithm aims to minimize [2],
        # compute (per sample average) pseudo-loglikelihood (proxy to likelihood)
        # instead, which not only is much more cheaper to compute, but also is
        # an asymptotically consistent estimate of the true log-likelihood [1].
        # More specifically, PLL computed using approximation as in [3].
        with tf.name_scope('pseudo_loglik'):
            x = self._X_batch
            # randomly corrupt one feature in each sample
            x_ = tf.identity(x)
            batch_size = tf.shape(x)[0]
            pll_rand = tf.random_uniform([batch_size], minval=0, maxval=self._n_visible,
                                         dtype=tf.int32)
            ind = tf.transpose([tf.range(batch_size), pll_rand])
            m = tf.SparseTensor(indices=tf.to_int64(ind),
                                values=tf.ones_like(pll_rand, dtype=self._tf_dtype),
                                dense_shape=tf.to_int64(tf.shape(x_)))
            x_ = tf.multiply(x_, -tf.sparse_tensor_to_dense(m, default_value=-1))
            x_ = tf.sparse_add(x_, m)
            x_ = tf.identity(x_, name='x_corrupted')

            pll = tf.cast(self._n_visible, dtype=self._tf_dtype) *\
                  tf.log_sigmoid(self._free_energy(x_)-self._free_energy(x))
            tf.add_to_collection('pll', pll)

        # add also free energy of input batch to collection (for feg)
        free_energy_op = self._free_energy(self._X_batch)
        tf.add_to_collection('free_energy_op', free_energy_op)

        # collect summaries
        if self.metrics_config['l2_loss']:
            tf.summary.scalar(self._metrics_names_map['l2_loss'], l2_loss)
        if self.metrics_config['msre']:
            tf.summary.scalar(self._metrics_names_map['msre'], msre)
        if self.metrics_config['pll']:
            tf.summary.scalar(self._metrics_names_map['pll'], pll)

    def _make_tf_model(self):
        self._make_constants()
        self._make_placeholders()
        self._make_vars()
        self._make_train_op()

    def _make_tf_feed_dict(self, X_batch):
        d = {}
        d['learning_rate'] = self.learning_rate[min(self.epoch, len(self.learning_rate) - 1)]
        d['momentum'] = self.momentum[min(self.epoch, len(self.momentum) - 1)]
        d['X_batch'] = X_batch
        # prepend name of the scope, and append ':0'
        feed_dict = {}
        for k, v in d.items():
            feed_dict['input_data/{0}:0'.format(k)] = v
        return feed_dict

    def _train_epoch(self, X):
        results = [[] for _ in xrange(len(self._train_metrics_map))]
        for X_batch in batch_iter(X, self.batch_size,
                                  verbose=self.verbose):
            self.iter += 1
            if self.iter % self.metrics_config['train_metrics_every_iter'] == 0:
                run_ops = [v for _, v in sorted(self._train_metrics_map.items())]
                run_ops += [self._tf_merged_summaries, self._train_op]
                outputs = \
                self._tf_session.run(run_ops,
                                     feed_dict=self._make_tf_feed_dict(X_batch))
                values = outputs[:len(self._train_metrics_map)]
                for i, v in enumerate(values):
                    results[i].append(v)
                train_s = outputs[len(self._train_metrics_map)]
                self._tf_train_writer.add_summary(train_s, self.iter)
            else:
                self._tf_session.run(self._train_op,
                                     feed_dict=self._make_tf_feed_dict(X_batch))

        # aggregate and return metrics values
        results = map(lambda r: np.mean(r) if r else None, results)
        return dict(zip(sorted(self._train_metrics_map), results))

    def _run_val_metrics(self, X_val):
        results = [[] for _ in xrange(len(self._val_metrics_map))]
        for X_vb in batch_iter(X_val, batch_size=self.batch_size):
            run_ops = [v for _, v in sorted(self._val_metrics_map.items())]
            values = \
            self._tf_session.run(run_ops,
                                 feed_dict=self._make_tf_feed_dict(X_vb))
            for i, v in enumerate(values):
                results[i].append(v)
        for i, r in enumerate(results):
            results[i] = np.mean(r) if r else None
        summary_value = []
        for i, m in enumerate(sorted(self._val_metrics_map)):
            summary_value.append(summary_pb2.Summary.Value(tag=self._metrics_names_map[m],
                                                           simple_value=results[i]))
        val_s = summary_pb2.Summary(value=summary_value)
        self._tf_val_writer.add_summary(val_s, self.iter)
        return dict(zip(sorted(self._val_metrics_map), results))

    def _run_feg(self, X, X_val):
        """Calculate difference between average free energies of subsets
        of validation and training sets to monitor overfitting,
        as proposed in [2]. If the model is not overfitting at all, this
        quantity should be close to zero. Once this value starts
        growing, the model is overfitting and the value ("free energy gap")
        represents the amount of overfitting.
        """
        self._free_energy_op = tf.get_collection('free_energy_op')[0]
        train_fes, val_fes = [], []
        for _, X_b in zip(xrange(self.metrics_config['n_batches_for_feg']),
                          batch_iter(X, batch_size=self.batch_size)):
            train_fe = self._tf_session.run(self._free_energy_op,
                                            feed_dict=self._make_tf_feed_dict(X_b))
            train_fes.append(train_fe)
        for _, X_vb in zip(xrange(self.metrics_config['n_batches_for_feg']),
                           batch_iter(X_val, batch_size=self.batch_size)):
            val_fe = self._tf_session.run(self._free_energy_op,
                                          feed_dict=self._make_tf_feed_dict(X_vb))
            val_fes.append(val_fe)
        feg = np.mean(val_fes) - np.mean(train_fes)
        summary_value = [summary_pb2.Summary.Value(tag=self._metrics_names_map['feg'],
                                                   simple_value=feg)]
        feg_s = summary_pb2.Summary(value=summary_value)
        self._tf_val_writer.add_summary(feg_s, self.iter)
        return feg

    def _fit(self, X, X_val=None):
        # load ops requested
        self._train_op = tf.get_collection('train_op')[0]
        self._train_metrics_map = {}
        self._val_metrics_map = {}
        for m in self._train_metrics_names:
            if self.metrics_config[m]:
                self._train_metrics_map[m] = tf.get_collection(m)[0]
        for m in self._val_metrics_names:
            if self.metrics_config[m]:
                self._val_metrics_map[m] = tf.get_collection(m)[0]

        # main loop
        for self.epoch in epoch_iter(start_epoch=self.epoch, max_epoch=self.max_epoch,
                                     verbose=self.verbose):
            val_results = {}
            feg = None
            train_results = self._train_epoch(X)

            # run validation metrics if needed
            if X_val is not None and self.epoch % self.metrics_config['val_metrics_every_epoch'] == 0:
                val_results = self._run_val_metrics(X_val)
            if X_val is not None and self.metrics_config['feg'] and \
                    self.epoch % self.metrics_config['feg_every_epoch'] == 0:
                feg = self._run_feg(X, X_val)

            # print progress
            if self.verbose:
                s = "epoch: {0:{1}}/{2}".format(self.epoch, len(str(self.max_epoch)), self.max_epoch)
                for m, v in sorted(train_results.items()):
                    if v is not None:
                        s += "; {0}: {1:{2}}".format(m, v, self.metrics_config['{0}_fmt'.format(m)])
                for m, v in sorted(val_results.items()):
                    if v is not None:
                        s += "; val.{0}: {1:{2}}".format(m, v, self.metrics_config['{0}_fmt'.format(m)])
                if feg is not None: s += " ; feg: {0:{1}}".format(feg, self.metrics_config['feg_fmt'])
                write_during_training(s)

            # save if needed
            if self.save_after_each_epoch:
                self._save_model(global_step=self.epoch)

    def init_from(self, rbm):
        if type(self) != type(rbm):
            raise ValueError('an attempt to initialize `{0}` from `{1}`'.\
                             format(self.__class__.__name__, rbm.__class__.__name__))
        weights = rbm.get_tf_params(scope='weights')
        self.W_init = weights['W']
        self.vb_init = weights['vb']
        self.hb_init = weights['hb']

        weights_increments = rbm.get_tf_params(scope='weights_increments')
        self._dW_init = weights_increments['dW']
        self._dvb_init = weights_increments['dvb']
        self._dhb_init = weights_increments['dhb']

        self.iter = rbm.iter
        self.epoch = rbm.epoch

    @run_in_tf_session(update_seed=True)
    def transform(self, X):
        """Compute hidden units' activation probabilities."""
        self._transform_op = tf.get_collection('transform_op')[0]
        Z = np.zeros((len(X), self.n_hidden))
        start = 0
        for X_b in batch_iter(X, batch_size=self.batch_size,
                              verbose=self.verbose):
            Z_b = self._transform_op.eval(feed_dict=self._make_tf_feed_dict(X_b))
            Z[start:(start + self.batch_size)] = Z_b
            start += self.batch_size
        return Z

    def _serialize(self, params):
        for k, v in params.items():
            if isinstance(v, np.ndarray):
                # noinspection PyUnresolvedReferences
                params[k] = v.tolist()
        return params
