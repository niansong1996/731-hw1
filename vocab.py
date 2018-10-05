#!/usr/bin/env python
"""
Generate the vocabulary file for neural network training
A vocabulary file is a mapping of tokens to their indices

Usage:
    vocab.py --train-src=<file> --train-tgt=<file> [options] VOCAB_FILE

Options:
    -h --help                  Show this screen.
    --train-src=<file>         File of training source sentences
    --train-tgt=<file>         File of training target sentences
    --size=<int>               vocab size [default: 50000]
    --freq-cutoff=<int>        frequency cutoff [default: 2]
"""

from typing import List
from collections import Counter
from itertools import chain
from docopt import docopt
import pickle
import re

from utils import read_corpus, input_transpose


class VocabEntry(object):
    def __init__(self):
        self.word2id = dict()
        self.unk_id = 3
        self.word2id['<pad>'] = 0
        self.word2id['<s>'] = 1
        self.word2id['</s>'] = 2
        self.word2id['<unk>'] = 3

        self.id2word = {v: k for k, v in self.word2id.items()}

    def __getitem__(self, word):
        return self.word2id.get(word, self.unk_id)

    def __contains__(self, word):
        return word in self.word2id

    def __setitem__(self, key, value):
        raise ValueError('vocabulary is readonly')

    def __len__(self):
        return len(self.word2id)

    def __repr__(self):
        return 'Vocabulary[size=%d]' % len(self)

    def id2word(self, wid):
        return self.id2word[wid]

    def add(self, word):
        if word not in self:
            wid = self.word2id[word] = len(self)
            self.id2word[wid] = word
            return wid
        else:
            return self[word]

    def words2indices(self, sents):
        if type(sents[0]) == list:
            return [[self[w] for w in s] for s in sents]
        else:
            return [self[w] for w in sents]

    @staticmethod
    def from_corpus(corpus, size, freq_cutoff=2):
        vocab_entry = VocabEntry()

        word_freq = Counter(chain(*corpus))
        valid_words = [w for w, v in word_freq.items() if v >= freq_cutoff]
        print(f'number of word types: {len(word_freq)}, number of word types w/ frequency >= {freq_cutoff}: {len(valid_words)}')

        top_k_words = sorted(valid_words, key=lambda w: word_freq[w], reverse=True)[:size]
        for word in top_k_words:
            vocab_entry.add(word)

        return vocab_entry


class Vocab(object):
    def __init__(self, src_sents, tgt_sents, vocab_size, freq_cutoff):
        assert len(src_sents) == len(tgt_sents)

        print('initialize source vocabulary ..')
        self.src = VocabEntry.from_corpus(src_sents, vocab_size, freq_cutoff)

        print('initialize target vocabulary ..')
        self.tgt = VocabEntry.from_corpus(tgt_sents, vocab_size, freq_cutoff)

        print('initializing ger-eng dictionary ..')
        self.decoder_dict = dict()
        with open('raw_dict.txt', encoding='utf-8') as f:
            raw_content = f.readlines()
            for line in raw_content:
                try:
                    if line[0] in '\'()#\n&-.0123456789,':
                        continue
                    parts = line.strip().split('\t')
                    ger = parts[0].split(' ')[0]
                    eng = parts[1].split(' ')[0]
                    if ger not in self.decoder_dict:
                        self.decoder_dict[ger] = eng
                except:
                    continue

    def __repr__(self):
        return 'Vocab(source %d words, target %d words, %d decoder_dict items)' \
        % (len(self.src), len(self.tgt), len(self.decoder_dict))


if __name__ == '__main__':
    decoder_dict = dict()
    with open('raw_dict.txt', encoding='utf-8') as f:
            raw_content = f.readlines()
            for line in raw_content:
                try:
                    if line[0] in '\'()#\n&-.0123456789,':
                        continue
                    parts = line.strip().split('\t')
                    ger = parts[0].split(' ')[0]
                    eng = parts[1]
                    eng = re.sub('\{.+\}|\[.+\]', '', eng)
                    eng_words = eng.strip().split(' ')
                    if eng_words[0] == 'to':
                        eng_words = eng_words[1:]
                    eng_words = [x.strip() for x in eng_words]
                    eng = ' '.join(eng_words)
                    if ger not in decoder_dict:
                        decoder_dict[ger] = eng
                except:
                    continue

    args = docopt(__doc__)

    print('read in source sentences: %s' % args['--train-src'])
    print('read in target sentences: %s' % args['--train-tgt'])

    src_sents = read_corpus(args['--train-src'], source='src')
    tgt_sents = read_corpus(args['--train-tgt'], source='tgt')

    vocab = Vocab(src_sents, tgt_sents, int(args['--size']), int(args['--freq-cutoff']))
    print('generated vocabulary, source %d words, target %d words, decoder_dict %d items' \
        % (len(vocab.src), len(vocab.tgt), len(vocab.decoder_dict)))

    pickle.dump(vocab, open(args['VOCAB_FILE'], 'wb'))
    print('vocabulary saved to %s' % args['VOCAB_FILE'])
