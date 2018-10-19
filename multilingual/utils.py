import math
from typing import List
from config import LANG_INDICES

import numpy as np
import io
import torch.tensor as Tensor

def input_transpose(sents, pad_token):
    """
    This function transforms a list of sentences of shape (batch_size, token_num) into 
    a list of shape (token_num, batch_size). You may find this function useful if you
    use pytorch
    """
    max_len = max(len(s) for s in sents)
    batch_size = len(sents)

    sents_t = []
    for i in range(max_len):
        sents_t.append([sents[k][i] if len(sents[k]) > i else pad_token for k in range(batch_size)])

    return sents_t

def read_corpus(file_path, source):
    data = []
    for line in open(file_path, encoding="utf-8"):
        sent = line.strip().split(' ')
        # only append <s> and </s> to the target sentence
        if source == 'tgt':
            sent = ['<s>'] + sent + ['</s>']
        data.append(sent)

    return data


def read_corpus_pairs(src_lang_idx, tgt_lang_idx, source):
    src_sents = []
    tgt_sents = []

    src_lang = LANG_INDICES[src_lang_idx]
    tgt_lang = LANG_INDICES[tgt_lang_idx]

    # read corpus for src corpus
    file_path = '%s.%s-%s.%s.txt' % (source, tgt_lang, src_lang, src_lang)
    for line in open(file_path, encoding="utf-8"):
        sent = line.strip().split(' ')
        src_sents.append(sent)

    # read corpus for tgt corpus
    file_path = '%s.%s-%s.%s.txt' % (source, tgt_lang, src_lang, tgt_lang)
    for line in open(file_path, encoding="utf-8"):
        sent = line.strip().split(' ')
        if source == 'tgt':
            sent = ['<s>'] + sent + ['</s>']
        tgt_sents.append(sent)

    # pair those corresponding sents together
    src_tgt_sent_pairs = list(zip(src_sents, tgt_sents))

    return src_tgt_sent_pairs


def assert_tensor_size(tensor: Tensor, expected_size: List[int]):
    try:
        assert list(tensor.shape) == expected_size
    except AssertionError:
        print("tensor shape %s doesn't match expected size %s" % (tensor.shape, expected_size))
        raise


def batch_iter(data, batch_size, shuffle=True):
    """
    Given a list of examples, shuffle and slice them into mini-batches
    """
    batch_num = math.ceil(len(data) / batch_size)
    index_array = list(range(len(data)))

    # sort the pairs w.r.t. the length of the src sent
    data = sorted(data, key=lambda e: len(e[0]), reverse=True)

    batch_idx = list(range(batch_num))
    if shuffle:
        np.random.shuffle(batch_idx)
    for i in batch_idx:
        indices = index_array[i * batch_size: (i + 1) * batch_size]
        examples = [data[idx] for idx in indices]

        src_sents = [e[0] for e in examples]
        tgt_sents = [e[1] for e in examples]

        yield src_sents, tgt_sents

def load_matrix(fname, vocabs, emb_dim):
    words = []
    word2idx = {}
    word2vec = {}

    fin = io.open(fname, 'r', encoding='utf-8', newline='\n', errors='ignore')
    n, d = map(int, fin.readline().split())
    data = {}
    for line in fin:
        tokens = line.rstrip().split(' ')
        word = tokens[0]
        word2idx[word] = len(words)
        words.append(word)
        word2vec[word] = np.array(tokens[1:]).astype(np.float)

    matrix_len = len(vocabs)
    weights_matrix = np.zeros((matrix_len, emb_dim))
    words_found = 0

    for i, word in enumerate(vocabs):
        try:
            weights_matrix[i] = word2vec[word]
            words_found += 1
        except KeyError:
            weights_matrix[i] = np.random.random(size=(emb_dim,))
    return weights_matrix
