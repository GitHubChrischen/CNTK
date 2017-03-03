import cntk as C
import numpy as np
from cntk import layers, blocks, models
from helpers import PastValueWindow, seqlogZ
import pickle

with open('vocabs.pkl', 'rb') as vf:
    known, vocab, chars = pickle.load(vf)

word_size = 20
w_dim = len(vocab)
c_dim = len(chars) * word_size
a_dim = 1
dim = 100
convs = 100
rf = 5
dropout = 0.2
char_emb_dim = 8
word_count_threshold = 10
max_question_len = 15 # 65 # actual value is 65 let's wait for a good window function
highway_layers = 2
char_count_threshold = 50
max_context_len = 870

C.set_default_device(C.cpu())

def charcnn(x):
    return C.models.Sequential([
        C.layers.Embedding(char_emb_dim),
        C.layers.Dropout(0.2),
        C.layers.Convolution1D((5,), convs, activation=C.relu, init=C.glorot_uniform(), pad=[True], strides=1, bias=True, init_bias=True),
        C.GlobalMaxPooling()])(x)

# todo switch to this once splice can backprop sparse gradients
def embed_via_splice(known, vocab):
    # load glove
    npglove = np.zeros((known,dim), dtype=np.float32)
    with open('glove.6B.100d.txt', encoding='utf-8') as f:
        for line in f:
            parts = line.split()
            word = parts[0].lower()
            if word in vocab:
                npglove[vocab[word],:] = np.asarray([float(p) for p in parts[1:]])
    glove = C.constant(npglove)
    nonglove = C.parameter(shape=(len(vocab) - known, dim), init=C.glorot_uniform())
    return C.splice(glove, nonglove, axis=0)

# todo: this is not exactly the implementation in TP3 because the glove vectors are just used for initialization here
def embed(known, vocab):
    # load glove
    n = len(vocab)
    npglove = (np.random.rand(n, dim) * np.sqrt(6.0/(n + dim))).astype(np.float32)
    with open('glove.6B.100d.txt', encoding='utf-8') as f:
        for line in f:
            parts = line.split()
            word = parts[0].lower()
            if word in vocab:
                npglove[vocab[word],:] = np.asarray([float(p) for p in parts[1:]])
    glove = C.parameter(shape=(n, dim), init=npglove)

    def apply(x):
        return C.times(x, glove)
    return apply

c = C.Axis.new_unique_dynamic_axis('c')
q = C.Axis.new_unique_dynamic_axis('q')
b = C.Axis.default_batch_axis()
cw = C.input_variable(w_dim, dynamic_axes=[b,c], is_sparse=True, name='cw')
qw = C.input_variable(w_dim, dynamic_axes=[b,q], is_sparse=True, name='qw')
cc = C.input_variable(c_dim, dynamic_axes=[b,c], name='cc')
qc = C.input_variable(c_dim, dynamic_axes=[b,q], name='qc')
ab = C.input_variable(a_dim, dynamic_axes=[b,c], name='ab')
ae = C.input_variable(a_dim, dynamic_axes=[b,c], name='ae')

mb_source = C.MinibatchSource(C.CTFDeserializer('val.ctf', C.StreamDefs(
    context_words = C.StreamDef('cw',  shape=w_dim, is_sparse=True),
    query_words   = C.StreamDef('qw',  shape=w_dim, is_sparse=True),
    answer_begin  = C.StreamDef('ab',  shape=1,     is_sparse=False),
    answer_end    = C.StreamDef('ae',  shape=1,     is_sparse=False),
    context_chars = C.StreamDef('cc',  shape=c_dim, is_sparse=True),
    query_chars   = C.StreamDef('qc',  shape=c_dim, is_sparse=True)
)), randomize=False)

input_map = {
    cw: mb_source.streams.context_words,
    qw: mb_source.streams.query_words,
    cc: mb_source.streams.context_chars,
    qc: mb_source.streams.query_chars,
    ab: mb_source.streams.answer_begin,
    ae: mb_source.streams.answer_end
}

mb_data = mb_source.next_minibatch(256, input_map=input_map)
print(mb_data)

embedding = embed(known, vocab)

input_chars = C.placeholder_variable(shape=(c_dim,))
input_words = C.placeholder_variable(shape=(w_dim,))

# we need to reshape because GlobalMaxPooling is retaining a trailing singleton dimension
# todo GlobalPooling should have a keepdims default to False
embedded = C.splice(embedding(input_words), C.reshape(charcnn(C.reshape(input_chars, (C.InferredDimension, word_size))), convs))

def BidirectionalRecurrence(fwd, bwd):
    f = C.layers.Recurrence(fwd)
    b = C.layers.Recurrence(bwd, go_backwards=True)
    x = C.placeholder_variable()
    return C.splice (f(x), b(x))

#todo add highway network
input_layers = C.layers.Sequential([
    # HighwayNetwork(2),
    C.Dropout(0.2),
    BidirectionalRecurrence(C.blocks.LSTM(dim), C.blocks.LSTM(dim))
])

q_emb = embedded.clone(C.CloneMethod.share, dict(zip(embedded.placeholders, [qw,qc])))
c_emb = embedded.clone(C.CloneMethod.share, dict(zip(embedded.placeholders, [cw,cc])))
q_processed = input_layers(q_emb)
c_processed = input_layers(c_emb)

pvw, mask = PastValueWindow(max_question_len, C.Axis.new_leading_axis())(q_processed)
print('pvw', pvw)

# This part deserves some explanation
# It is the attention layer
# In the paper they use a 6 * dim dimensional vector
# here we split it in three parts because the different parts
# participate in very different operations
# so W * [h; u; h.* u] becomes w1 * h + w2 * u + w3 * (h.*u)
ws1 = C.parameter(shape=(2 * dim, 1), init=C.glorot_uniform())
ws2 = C.parameter(shape=(2 * dim, 1), init=C.glorot_uniform())
ws3 = C.parameter(shape=(1, 2 * dim), init=C.glorot_uniform())

wh = C.times (c_processed, ws1)
wu = C.reshape(C.times (pvw, ws2), (max_question_len,))
whu = C.times_transpose(c_processed, C.sequence.broadcast_as(C.element_times (pvw, ws3), c_processed))
S = wh + whu + C.sequence.broadcast_as(wu, c_processed)
q_logZ = C.reshape(C.reduce_log_sum_exp(S),(1,))
q_attn = C.reshape(C.exp(S - q_logZ),(-1,1))
utilde = C.reshape(C.reduce_sum(C.sequence.broadcast_as(pvw, q_attn) * q_attn, axis=0),(-1))
print(utilde)

max_col = C.reduce_max(S)
c_logZ = seqlogZ(max_col)
c_attn = C.exp(max_col - C.sequence.broadcast_as(c_logZ, max_col))
htilde = C.sequence.reduce_sum(c_processed * c_attn)
print(htilde)
print(c_processed)

Htilde = C.sequence.broadcast_as(htilde, c_processed)
modeling_layer_input = C.splice(c_processed, utilde, c_processed * utilde,  c_processed * Htilde)
print(modeling_layer_input)

#todo replace with optimized_rnnstack for training purposes once it supports dropout
begin_model = C.layers.Sequential([
    C.Dropout(0.2),
    BidirectionalRecurrence(C.blocks.LSTM(dim), C.blocks.LSTM(dim)),
    C.Dropout(0.2),
    BidirectionalRecurrence(C.blocks.LSTM(dim), C.blocks.LSTM(dim))
])

end_model = begin_model.clone(C.CloneMethod.clone)

second_layer = begin_model(modeling_layer_input)
fourth_layer = end_model(second_layer)

def seqloss(logits, y):
    return C.sequence.last(C.sequence.gather(logits, y)) - seqlogZ(logits)

begin_input = C.splice(modeling_layer_input, second_layer)
begin_weights = C.parameter(shape=(C.InferredDimension,1), init=C.glorot_uniform())
begin_logits = C.times(begin_input, begin_weights)
begin_loss = seqloss(begin_logits, ab)

end_input = C.splice(modeling_layer_input, fourth_layer)
end_weights = C.parameter(shape=(C.InferredDimension,1), init=C.glorot_uniform())
end_logits = C.times(end_input, end_weights)
end_loss = seqloss(end_logits, ae)

loss = begin_loss + end_loss

print(loss)
print([t.shape for t in loss.grad(mb_data, wrt=loss.parameters)])