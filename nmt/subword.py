#!/usr/bin/env python
"""
Generate the subword models and vocab for languages
The model and vocab can be further used to encode and decode using provided functions

Usage:
    vocab.py --lang=<lang-abbr> --vocab-size=<file>

Options:
    -h --help                  Show this screen.
    --lang=<lang-abbr>         Two letter representation of language
    --vocab-size=<file>        The vocabulary size for subword model
"""
from typing import List, Tuple, Set

import sentencepiece as spm
import numpy as np
from docopt import docopt


def train(lang, vocab_size):
    spm.SentencePieceTrainer. \
        Train('--pad_id=3 --character_coverage=0.9995 --input=../multilingual/data/%s_mono.txt --model_prefix=subword_files/%s --vocab_size=%d' % (lang, lang, vocab_size))


def get_corpus_pairs(src_lang_idx: int, tgt_lang_idx: int, data_type: str) \
        -> List[Tuple[List[int], List[int]]]:
    # get src and tgt corpus ids separately
    src_sents, long_sent = get_corpus_ids(src_lang_idx, tgt_lang_idx, data_type, False)
    tgt_sents, _ = get_corpus_ids(src_lang_idx, tgt_lang_idx, data_type, True, long_sent=long_sent)

    # pair those corresponding sents together
    src_tgt_sent_pairs = list(zip(src_sents, tgt_sents))

    return src_tgt_sent_pairs


def get_corpus_ids(file_path, lang_name, is_tgt: bool, skip_long=True, long_sent=set())\
        -> Tuple[List[List[int]], Set[int]]:
    sents = []

    # load the subword models for encoding these sents to indices
    sp = spm.SentencePieceProcessor()
    sp.Load('subword_files/%s.model' % lang_name)

    # read corpus for corpus
    line_count = 0
    long_sent_in_src = set()
    line_lens = []
    for line in open(file_path, encoding="utf-8"):
        sent = line.strip()
        line_count += 1
        sent_encode = sp.EncodeAsIds(sent)
        if is_tgt:
            if line_count in long_sent:
                continue
        else:
            line_lens.append(len(sent_encode))
            if skip_long and len(sent_encode) > 100:
                long_sent_in_src.add(line_count)
                continue
        if is_tgt:
            # add <s> and </s> to the tgt sents
            sent_encode = [sp.bos_id()] + sent_encode + [sp.eos_id()]
        sents.append(sent_encode)
    if len(line_lens) > 0:
        print(np.histogram(line_lens, bins=np.arange(max(line_lens), step=10), density=True))
    return sents, long_sent_in_src


def decode_corpus_ids(lang_name: str, sents: List[List[int]]) -> List[List[str]]:
    sp = spm.SentencePieceProcessor()
    sp.Load('subword_files/%s.model' % lang_name)

    decoded_sents = []
    for line in sents:
        sent = sp.DecodeIds(line)
        decoded_sents.append(sent)

    return decoded_sents


if __name__ == '__main__':
    args = docopt(__doc__)

    vocab_size = int(args['--vocab-size'])
    lang = args['--lang']

    print('building subword model for %s language : ' % lang)

    train(lang, vocab_size)
    print('Done for %s : ' % lang)
