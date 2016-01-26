import itertools
import tensorflow as tf

from data import DataSet


class EmbeddingModule(object):
    l_dict = {}

    def __init__(self, config, em=None, te=False, name=""):
        self.config = config
        self.te = te
        default_initializer = tf.random_normal_initializer(config.init_mean, config.init_std)
        M, J, d, V = config.memory_size, config.sentence_size, config.hidden_size, config.vocab_size
        if em is None:
            self.inherited = False
            self.name = name
            self.A = tf.get_variable(name, shape=[V, d], initializer=default_initializer)
            if te:
                self.TA = tf.get_variable("T_%s" % self.name, shape=[M, d], initializer=default_initializer)
            else:
                self.TA = None
        else:
            self.inherited = True
            self.name = em.name
            self.A, self.TA = em.A, em.TA

        # variables initialized in this module
        self.init_variables = []
        if not self.inherited:
            self.init_variables.append(self.A)
            if self.te:
                self.init_variables.append(self.TA)

    def __call__(self, x_batch):
        Ax_batch = tf.nn.embedding_lookup(self.A, x_batch)  # [B, M, J, d]
        if self.config.pe:
            Ax_batch *= EmbeddingModule._get_pe(self.config.sentence_size, self.config.hidden_size)
        m_batch = tf.reduce_sum(Ax_batch, [2])  # [B, M, d]
        if self.te:
            m_batch += self.TA  # broadcasting
        # m_batch = tf.Print(m_batch, [m_batch], self.name)
        self.m_batch = m_batch
        return m_batch

    def variables(self):
        out = []
        return out

    @staticmethod
    def _get_pe(sentence_size, hidden_size):
        key = (sentence_size, hidden_size)
        if key not in EmbeddingModule.l_dict:
            f = lambda J, j, d, k: (1-float(j)/J) - (float(k)/d)*(1-2.0*j/J)
            g = lambda j: [f(sentence_size, j, hidden_size, k) for k in range(hidden_size)]
            l = [g(j) for j in range(sentence_size)]
            l_tensor = tf.constant(l, shape=[sentence_size, hidden_size])
            EmbeddingModule.l_dict[key] = l_tensor
        return EmbeddingModule.l_dict[key]


class MemoryLayer(object):
    def __init__(self, config, layer_name=None):
        self.config = config

    def __call__(self, m_batch, c_batch, u_batch):
        self.c_batch = c_batch
        self.u_batch = u_batch
        u_2d_batch = tf.expand_dims(u_batch, -1)

        p_batch = tf.nn.softmax(tf.squeeze(tf.batch_matmul(m_batch, u_2d_batch)))
        p_2d_batch = tf.expand_dims(p_batch, -1) # [B, M, 1]
        p_tiled_batch = tf.tile(p_2d_batch, [1, 1, self.config.hidden_size])  # [B, M, d]

        o_batch = tf.reduce_sum(c_batch * p_tiled_batch, [1])  # [B d]

        self.p_batch = p_batch
        self.o_batch = o_batch
        return o_batch


class MemN2NModel(object):
    def __init__(self, config, session):
        self.config = config

        default_initializer = tf.random_normal_initializer(config.init_mean, config.init_std)

        # place holders
        self.x_batch = tf.placeholder('int32', name='x', shape=[None, config.memory_size, config.sentence_size])
        self.q_batch = tf.placeholder('int32', name='q', shape=[None, config.sentence_size])
        self.y_batch = tf.placeholder('int32', name='y', shape=[None])
        self.learning_rate = tf.placeholder('float', name='lr')

        # input embedding
        self.A_ems, self.C_ems = [], []
        for i in range(config.num_layer):
            if i == 0:
                A_em = EmbeddingModule(config, te=config.te, name='A%d' % i)
                C_em = EmbeddingModule(config, te=config.te, name='C%d' % i)
            else:
                if config.tying == 'adj':
                    A_em = EmbeddingModule(config, C_em)
                    C_em = EmbeddingModule(config, te=config.te, name='C%d' % i)
                elif config.tying == 'rnn':
                    A_em = EmbeddingModule(config, A_em)
                    C_em = EmbeddingModule(config, C_em)
                else:
                    raise Exception("undefined tying method")
            self.A_ems.append(A_em)
            self.C_ems.append(C_em)

        # question embedding
        if config.tying == 'adj':
            self.B_em = EmbeddingModule(config, self.A_ems[0])
        else:
            self.B_em = EmbeddingModule(config, name='B')

        # label -> one-hot vector
        self.v_batch = tf.nn.embedding_lookup(tf.diag(tf.ones([self.config.vocab_size])), self.y_batch)

        # memory layers
        self.memory_layers = [MemoryLayer(config) for _ in range(config.num_layer)]
        
        # linear mapping
        if config.tying == 'rnn':
            self.H = tf.get_variable('H', shape=[config.hidden_size, config.hidden_size], initializer=default_initializer)

        # output mapping
        if config.tying == 'adj':
            self.W = tf.transpose(self.C_ems[-1].A)
        elif config.tying == 'rnn':
            self.W = tf.get_variable('W', shape=[config.hidden_size, config.vocab_size], initializer=default_initializer)

        # connect tensors
        # TODO : this can be simplified if we figure out how to count dimension backward
        u_batch = tf.squeeze(self.B_em(tf.expand_dims(self.q_batch, 1)))
        for i, (A_em, C_em, memory_layer) in enumerate(zip(self.A_ems, self.C_ems, self.memory_layers)):
            o_batch = memory_layer(A_em(self.x_batch), C_em(self.x_batch), u_batch)
            if config.tying == 'rnn':
                u_batch = tf.matmul(u_batch, self.H)
            u_batch = u_batch + o_batch

        # output tensor
        self.unscaled_a_batch = tf.matmul(u_batch, self.W)

        # accuracy tensor
        correct_prediction = tf.equal(tf.argmax(self.unscaled_a_batch, 1), tf.argmax(self.v_batch, 1))
        self.accuracy = tf.reduce_mean(tf.cast(correct_prediction, 'float'))

        # loss tensor
        self.loss = tf.nn.softmax_cross_entropy_with_logits(self.unscaled_a_batch, self.v_batch)

        # optimizer
        opt = tf.train.GradientDescentOptimizer(self.learning_rate)

        # gradient clipping
        variables = []
        for em in self.A_ems:
            variables.extend(em.init_variables)
        for em in self.C_ems:
            variables.extend(em.init_variables)
        if config.tying != 'adj':
            variables.extend(self.B_em.variables())
        if config.tying == 'rnn':
            variables.append(self.H)
            variables.append(self.W)

        # self.loss = tf.Print(self.loss, [tf.argmax(self.v_batch, 1), tf.argmax(self.unscaled_a_batch, 1)])
        grads_and_vars = opt.compute_gradients(self.loss)
        clipped_grads_and_vars = [(tf.clip_by_norm(grad, config.max_grad_norm), var) for grad, var in grads_and_vars]
        self.opt_op = opt.apply_gradients(clipped_grads_and_vars)
        # self.opt_op = opt.minimize(self.loss)

        # self.optimizer = optimizer_handler.minimize(self.loss)

        tf.initialize_all_variables().run()

    def train(self, x_batch, q_batch, y_batch, learning_rate):
        self.opt_op.run(feed_dict={self.x_batch: x_batch, self.q_batch: q_batch, self.y_batch: y_batch,
                                   self.learning_rate: learning_rate})

    def train_data_set(self, train_data_set, val_data_set=None, val_period=1):
        """
        :param train_data_set:
        :param val_data_set: If val_data_set is specified, then intermediate results are printed for every val_period epochs
        :param val_period:
        :return:
        """
        assert isinstance(train_data_set, DataSet)
        num_epoch = self.config.num_epoch
        batch_size = self.config.batch_size
        lr, anneal_ratio, anneal_period = self.config.init_lr, self.config.anneal_ratio, self.config.anneal_period
        for epoch_idx in range(num_epoch):
            if epoch_idx > 0 and epoch_idx % anneal_period == 0:
                lr *= anneal_ratio
            while train_data_set.has_next(batch_size):
                x_batch, q_batch, y_batch = train_data_set.next_batch(batch_size)
                self.train(x_batch, q_batch, y_batch, lr)
            train_data_set.rewind()
            if val_data_set is not None and epoch_idx % val_period == 0:
                print self.test_data_set(val_data_set), lr

    def test(self, x_batch, q_batch, y_batch):
        # print sum(sum(self.v_batch.eval(feed_dict={self.x_batch: x_batch, self.q_batch: q_batch, self.y_batch: y_batch})))
        return self.accuracy.eval(feed_dict={self.x_batch: x_batch, self.q_batch: q_batch, self.y_batch: y_batch})

    def test_data_set(self, data_set):
        assert isinstance(data_set, DataSet)
        return self.test(data_set.xs, data_set.qs, data_set.ys)