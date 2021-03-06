import tensorflow as tf
import modekeys
import helper
from tensorflow.contrib.seq2seq.python.ops.attention_wrapper import _BaseAttentionMechanism, AttentionMechanism, AttentionWrapper
from tensorflow.python.layers import core as layers_core
from tensorflow.contrib.seq2seq.python.ops.basic_decoder import BasicDecoder
from tensorflow.contrib.seq2seq.python.ops.decoder import dynamic_decode


random_seed = 9

def create_input_layer(filename,hp,mode):
    with tf.name_scope('input_layer') as ns:
        example = read_and_decode([filename], hp.num_epochs, hp.max_sentence_length,hp.max_context_length)
        min_after_dequeue = 10000
        capacity = min_after_dequeue + 3 * hp.batch_size
        if mode ==modekeys.TRAIN and hp.shuffle_batch:
            batch_example = tf.train.shuffle_batch(example,batch_size=hp.batch_size,
                                                   capacity=capacity,min_after_dequeue=min_after_dequeue)
        else:
            batch_example = tf.train.batch(example,batch_size=hp.batch_size)


        batch_example['context_length'] = tf.squeeze(batch_example['context_length'], 1)
        batch_example['response_mask'] = tf.to_float(batch_example['response_mask'])

        if mode == modekeys.TRAIN or mode == modekeys.EVAL:
            return batch_example
        elif mode == modekeys.PREDICT:
            return batch_example

def read_and_decode(filenames,num_epochs,max_sentence_length,max_context_length):
    fname_queue = tf.train.string_input_producer(filenames,num_epochs=num_epochs)
    reader = tf.TFRecordReader("my_reader")
    _, serilized_example = reader.read(queue=fname_queue)
    feature_spec = create_feature_spec(max_sentence_length,max_context_length)
    example = tf.parse_single_example(serilized_example, feature_spec)
    example['contexts'] = tf.reshape(example['contexts_flatten'],shape=[max_context_length,max_sentence_length])
    example.pop('contexts_flatten')
    return example

def create_feature_spec(max_sentence_length,max_context_length):
    spec = {}
    spec['contexts_flatten'] = tf.FixedLenFeature(shape=[max_context_length * max_sentence_length],dtype=tf.int64)
    spec['context_utterance_length'] = tf.FixedLenFeature(shape=[max_context_length], dtype=tf.int64)
    spec['context_length'] = tf.FixedLenFeature(shape=[1], dtype=tf.int64)
    spec['response_in'] = tf.FixedLenFeature(shape=[max_sentence_length], dtype=tf.int64)
    spec['response_out'] = tf.FixedLenFeature(shape=[max_sentence_length], dtype=tf.int64)
    spec['response_mask'] = tf.FixedLenFeature(shape=[max_sentence_length], dtype=tf.int64)
    return spec

def impl(features,mode,hp):
    contexts = features['contexts']  # batch_size,max_con_length(with query),max_sen_length
    context_utterance_length = features['context_utterance_length']  # batch_size,max_con_length
    context_length = features['context_length']  # batch_size
    response_in = features['response_in']  # batch_size,max_res_length(with eos token)
    response_out = features['response_out']  # batch_size, max_res_length (with eos token append before)
    response_mask = features['response_mask']  # batch_size, max_res_length (with eos token append before)

    with tf.variable_scope('embedding_layer') as vs:
        embedding_w = get_embedding_matrix(hp.word_dim,mode,hp.vocab_size)
        contexts = tf.nn.embedding_lookup(embedding_w,contexts,'context_embedding')
        if mode == modekeys.TRAIN or mode == modekeys.EVAL:
            response_in = tf.nn.embedding_lookup(embedding_w, response_in, 'response_in_embedding')

    with tf.variable_scope('utterance_encoding_layer',reuse=tf.AUTO_REUSE) as vs:
        kernel_initializer = tf.random_normal_initializer(mean=0.0, stddev=0.1, seed=random_seed)
        bias_initializer = tf.zeros_initializer()
        fw_cell = tf.nn.rnn_cell.GRUCell(num_units=hp.word_rnn_num_units, kernel_initializer=kernel_initializer,
                                      bias_initializer=bias_initializer)
        bw_cell = tf.nn.rnn_cell.GRUCell(num_units=hp.word_rnn_num_units, kernel_initializer=kernel_initializer,
                                      bias_initializer=bias_initializer)

        context_t = tf.transpose(contexts, perm=[1, 0, 2, 3])  # max_con_length(with query),batch_size,max_sen_length
        context_utterance_length_t = tf.transpose(context_utterance_length, perm=[1, 0])  # max_con_length, batch_size
        a = tf.split(context_t, hp.max_context_length, axis=0)  # 1,batch_size,max_sen_length
        b = tf.split(context_utterance_length_t, hp.max_context_length, axis=0)  # 1,batch_size

        utterance_encodings = []
        for utterance,length in zip(a,b):
            utterance = tf.squeeze(utterance,axis=0)
            length = tf.squeeze(length,axis=0)
            utterance_hidden_states,_ =tf.nn.bidirectional_dynamic_rnn(fw_cell,bw_cell,utterance,sequence_length=length,initial_state_fw=fw_cell.zero_state(hp.batch_size,tf.float32),initial_state_bw=fw_cell.zero_state(hp.batch_size,tf.float32))
            utterance_encoding = tf.concat(utterance_hidden_states,axis=2)
            utterance_encodings.append(tf.expand_dims(utterance_encoding,axis=0))

        utterance_encodings = tf.concat(utterance_encodings, axis=0)  # max_con_length,batch_size,max_sen,2*word_rnn_num_units

    with tf.variable_scope('hierarchical_attention_layer',reuse=tf.AUTO_REUSE) as vs:
        attention_mechanism = ContextAttentionMechanism(context_num_units=100,context=utterance_encodings,context_utterance_length=context_utterance_length_t,max_context_length=hp.max_context_length,context_rnn_num_units=hp.context_rnn_num_units,context_actual_length=context_length)

    with tf.variable_scope('decoder_layer',reuse=tf.AUTO_REUSE) as vs:
        kernel_initializer = tf.random_normal_initializer(mean=0.0, stddev=0.1, seed=random_seed)
        bias_initializer = tf.zeros_initializer()
        decoder_cell = tf.nn.rnn_cell.GRUCell(num_units=hp.decoder_rnn_num_units, kernel_initializer=kernel_initializer,
                                              bias_initializer=bias_initializer)

        sequence_length = tf.constant(value=hp.max_sentence_length, dtype=tf.int32, shape=[hp.batch_size])
        if mode == modekeys.TRAIN:
            helper = tf.contrib.seq2seq.TrainingHelper(inputs=response_in, sequence_length=sequence_length)
        elif mode == modekeys.EVAL:
            helper = tf.contrib.seq2seq.TrainingHelper(inputs=response_in, sequence_length=sequence_length)
        else:
            start_tokens = tf.constant(value=1, dtype=tf.int32, shape=[hp.batch_size], name='start_tokens')
            end_token = 1
            helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(embedding=embedding_w, start_tokens=start_tokens,
                                                              end_token=end_token)

        attn_cell = AttentionWrapper(decoder_cell,attention_mechanism=attention_mechanism,attention_layer_size=None,output_attention=False) # output_attention should be False
        output_layer = layers_core.Dense(units=hp.vocab_size,activation=None,use_bias=False)  # should use no activation and no bias
        decoder = BasicDecoder(cell=attn_cell,helper=helper,initial_state=attn_cell.zero_state(hp.batch_size,tf.float32),output_layer=output_layer)

        if mode == modekeys.TRAIN:
            final_outputs, final_state, final_sequence_lengths = dynamic_decode(decoder=decoder,impute_finished=True,parallel_iterations=32,swap_memory=True)
            logits = final_outputs.rnn_output
            cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=response_out,logits=logits)
            cross_entropy = tf.reduce_sum(cross_entropy*response_mask,axis=1)
            loss = tf.reduce_mean(cross_entropy)
            return loss
        elif mode == modekeys.EVAL:
            final_outputs, final_state, final_sequence_lengths = dynamic_decode(decoder=decoder, impute_finished=True,
                                                                                parallel_iterations=32,
                                                                                swap_memory=True)
            logits = final_outputs.rnn_output
            cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=response_out, logits=logits)
            cross_entropy = tf.reduce_mean(cross_entropy*response_mask)
            ppl = tf.exp(cross_entropy)
            return ppl
        else:
            max_iter = tf.constant(2*hp.max_sentence_length,dtype=tf.int32,shape=[])
            final_outputs, final_state, final_sequence_lengths = dynamic_decode(decoder=decoder, impute_finished=True,
                                                                                parallel_iterations=32,
                                                                                swap_memory=True,maximum_iterations=max_iter)
            return final_outputs.sample_id, final_sequence_lengths #batch, T T = max(final_sequence_lengths)

def get_embedding_matrix(word_dim,mode,vocab_size):
    if mode == modekeys.TRAIN:
        vocab, vocab_dict = helper.load_vocab('twitter_data/rg_vocab.txt')
        glove_vectors,glove_dict  = helper.load_glove_vectors('twitter_data/my_vector.txt', vocab)
        initial_value = helper.build_initial_embedding_matrix(vocab_dict, glove_dict, glove_vectors, word_dim)
        embedding_w = tf.get_variable(name='embedding_W', initializer=initial_value, trainable=True)
    else:
        embedding_w = tf.get_variable(name='embedding_W',shape=[vocab_size,word_dim],dtype=tf.float32,trainable=True)
    return embedding_w

class ContextAttentionMechanism(AttentionMechanism):

    def __init__(self, context_num_units, context, context_utterance_length,max_context_length,context_rnn_num_units,context_actual_length):
        # context: max_con, batch_size, max_sen, 2*word_rnn_dim
        # memory_sequence_length: max_con, batch_size
        # context_actual_length: batch
        self._context_num_units = context_num_units
        self._query_layer = layers_core.Dense(units=context_num_units,activation=None,use_bias=False)
        self._memory_layer = layers_core.Dense(units=context_num_units,activation=None,use_bias=False)

        self._context = tf.split(context,num_or_size_splits=max_context_length,axis=0) #1,batch, max_sen, 2*word_rnn_dim
        self._context_sequence_length = tf.split(context_utterance_length,num_or_size_splits=max_context_length,axis=0)
        self._max_context_length = max_context_length
        self._context_actual_length = context_actual_length

        self._utterance_attentions = []
        for u,l in zip(self._context, self._context_sequence_length):
            u = tf.squeeze(u,axis=0) #batch, max_sen, 2*word_rnn_dim
            l = tf.squeeze(l,axis=0) #batch_size
            self._utterance_attentions.append(UtteranceAttentionMechanism(num_units=100,memory=u,memory_sequence_length=l))

        kernel_initializer = tf.random_normal_initializer(mean=0.0, stddev=0.1, seed=random_seed)
        bias_initializer = tf.zeros_initializer()
        self._context_encoding_cell = tf.nn.rnn_cell.GRUCell(context_rnn_num_units,kernel_initializer=kernel_initializer,bias_initializer=bias_initializer)

        self.batch_size = context.shape[1].value
        self.values = tf.ones(shape=[self.batch_size, self._max_context_length,context_rnn_num_units]) #dummy atten value
        self._alignments_size = max_context_length


    @property
    def alignments_size(self):
        return self._alignments_size

    @property
    def state_size(self):
        return self._alignments_size

    def __call__(self, query, previous_alignments):

        with tf.variable_scope('context_encoding_layer',reuse=tf.AUTO_REUSE) as vs:
            prev_state = tf.random_normal(shape=[self.batch_size,self._context_encoding_cell.state_size], mean=0.0,stddev=0.1,seed=random_seed)
            context_utterance_encodings = []
            for i in range(self._max_context_length)[::-1]:
                utterance_alignments = self._utterance_attentions[i](decoder_hidden_state=query,context_encoder_hidden_state=prev_state)
                expanded_alignments = tf.expand_dims(utterance_alignments, 1) #batch,1,max_sen
                weighted_sum = tf.matmul(expanded_alignments, self._utterance_attentions[i].values) #batch,1,2*word_rnn_num_units
                weighted_sum = tf.squeeze(weighted_sum, [1]) #batch, 2*word_rnn_num_units
                _,new_state = self._context_encoding_cell(inputs=weighted_sum,state=prev_state)
                prev_state = new_state
                context_utterance_encodings.append(tf.expand_dims(new_state,axis=0)) #1,batch, context_rnn_num_units

        with tf.variable_scope('context_attention_layer') as vs:
            context_utterance_encodings = tf.transpose(tf.concat(context_utterance_encodings, axis=0),perm=[1, 0, 2])  # batch, max_con,context_rnn_num_units
            context_mask = tf.sequence_mask(lengths=self._context_actual_length,maxlen=self._max_context_length,dtype=tf.float32) #batch,max_con
            context_mask = tf.expand_dims(context_mask,axis=2) #batch,max_con, 1
            self.values = context_utterance_encodings * context_mask # batch, max_con,context_rnn_num_units
            keys = self._memory_layer(self.values)  # batch_size, max_con, context_num_units

            processed_query = self._query_layer(query) #batch, context_num_units
            processed_query = tf.expand_dims(processed_query,axis=1) #batch, 1, context_num_units

            v = tf.get_variable("attention_v", [self._context_num_units], dtype=tf.float32)
            score = tf.reduce_sum(v *tf.nn.tanh(keys + processed_query),axis=2) #batch_size, max_con
            alignments = tf.nn.softmax(score)
            return alignments

    def initial_alignments(self,batch_size,dtype):
        max_time = self._alignments_size
        return tf.zeros(shape=[batch_size,max_time],dtype=dtype)


class UtteranceAttentionMechanism(_BaseAttentionMechanism):

    def __init__(self, num_units, memory, memory_sequence_length):
        # memory: batch_size, max_sen, 2*word_rnn_dim
        # memory_sequence_length: batch_size

        wrapped_probability_fn = lambda score, _: tf.nn.softmax(score)
        super().__init__(query_layer=None,
            memory=memory,
            probability_fn=wrapped_probability_fn,
            memory_sequence_length=memory_sequence_length,
            memory_layer=layers_core.Dense(
                num_units, name="memory_layer", use_bias=False, dtype=tf.float32))

        self._decoder_hidden_state_layer = layers_core.Dense(num_units, activation=None, name="decoder_hidden_state_layer", use_bias=False, dtype=tf.float32)
        self._context_hidden_state_layer = layers_core.Dense(units=num_units,activation=None,use_bias=False,name='context_hidden_state_layer',dtype=tf.float32)
        self._num_units = num_units

    def __call__(self, decoder_hidden_state, context_encoder_hidden_state):
        with tf.variable_scope(None, "utterance_attention_layer"):
            processed_decoder_hidden_state = self._decoder_hidden_state_layer(decoder_hidden_state) #batch,num_units
            processed_decoder_hidden_state = tf.expand_dims(processed_decoder_hidden_state, 1)  # batch,1,num_units
            processed_context_state = self._context_hidden_state_layer(context_encoder_hidden_state) #batch,num_units
            processed_context_state = tf.expand_dims(processed_context_state, 1) # batch,1,num_units

            v = tf.get_variable("attention_v", [self._num_units], dtype=tf.float32)
            score = tf.reduce_sum(v * tf.nn.tanh(self._keys + processed_decoder_hidden_state + processed_context_state), [
                2])  #batch,max_mem_len
            alignments = self._probability_fn(score, None)  # batch,max_mem_len with each entry in 0-1 scale
        return alignments




























