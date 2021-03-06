# coding=utf-8

"""
A very basic implementation of neural machine translation

Usage:
    nmt.py train --vocab-size=<int> [options]
    nmt.py decode [options] MODEL_PATH SRC_LANG TGT_LANG OUTPUT_FILE

Options:
    -h --help                               show this screen.
    --langs=<src-tgt,...>                   comma separated language pairs <src-tgt>
    --cuda                                  use GPU
    --vocab-size=<int>                      vocab size [default: 20000]
    --low-rank=<int>                        low rank size [default: 4]
    --seed=<int>                            seed [default: 0]
    --batch-size=<int>                      batch size [default: 32]
    --lang-embed-size=<int>                 language embedding size [default: 8]
    --embed-size=<int>                      word embedding size [default: 256]
    --num-layers=<int>                      number of layers [default: 2]
    --hidden-size=<int>                     hidden size [default: 256]
    --clip-grad=<float>                     gradient clipping [default: 5.0]
    --log-every=<int>                       log every [default: 10]
    --max-epoch=<int>                       max epoch [default: 30]
    --patience=<int>                        wait for how many iterations to decay learning rate [default: 5]
    --max-num-trial=<int>                   terminate training after how many trials [default: 5]
    --lr-decay=<float>                      learning rate decay [default: 0.5]
    --beam-size=<int>                       beam size [default: 5]
    --lr=<float>                            learning rate [default: 0.001]
    --uniform-init=<float>                  uniformly initialize all parameters [default: 0.1]
    --save-to=<file>                        model save path
    --save-opt=<file>                       optimizer state save path
    --valid-niter=<int>                     perform validation after how many iterations [default: 2000]
    --dropout=<float>                       dropout [default: 0]
    --max-decoding-time-step=<int>          maximum number of decoding time steps [default: 70]
"""

import math
import sys
import time
from typing import *

import numpy as np
import torch
from docopt import docopt
from nltk.translate.bleu_score import corpus_bleu
from tqdm import tqdm
import sentencepiece as spm

from MultiMT import Hypothesis, MultiNMT
from config import device, LANG_INDICES, LANG_NAMES
from subword import get_corpus_pairs, get_corpus_ids, decode_corpus_ids, decode_sent_ids
from utils import batch_iter, PairedData, LangPair, read_corpus


def compute_corpus_level_bleu_score(references: List[List[str]], hypotheses: List[Hypothesis]) -> float:
    """
    Given decoding results and reference sentences, compute corpus-level BLEU score

    Args:
        references: a list of gold-standard reference target sentences
        hypotheses: a list of hypotheses, one for each reference

    Returns:
        bleu_score: corpus-level BLEU score
    """
    if references[0][0] == '<s>':
        references = [ref[1:-1] for ref in references]

    bleu_score = corpus_bleu([[ref] for ref in references],
                             [hyp.value for hyp in hypotheses])

    return bleu_score


def get_data_pairs(langs: List[List[str]], data_type: str):
    data = []
    for src_name, tgt_name in langs:
        src = LANG_INDICES[src_name]
        tgt = LANG_INDICES[tgt_name]
        data_pair = get_corpus_pairs(src, tgt, data_type)
        data.append(PairedData(data_pair, LangPair(src, tgt)))
        print('Done loading %s data for %s-%s parallel translation' \
              % (data_type, src_name, tgt_name))
    return data


def train(args: Dict[str, str]):
    lang_pairs = args['--langs']
    langs = [p.split('-') for p in lang_pairs.split(',')]
    train_data = get_data_pairs(langs, 'train')
    dev_data = get_data_pairs(langs, 'dev')

    train_batch_size = int(args['--batch-size'])
    clip_grad = float(args['--clip-grad'])
    valid_niter = int(args['--valid-niter'])
    log_every = int(args['--log-every'])
    model_save_path = args['--save-to']
    optimizer_save_path = args['--save-opt']

    # initialize the model
    print('Model initializing...')
    model = MultiNMT(args).to(device)

    num_trial = 0
    train_iter = patience = cum_loss = report_loss = cumulative_tgt_words = report_tgt_words = 0
    cumulative_examples = report_examples = epoch = valid_num = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin Maximum Likelihood training')

    # set the optimizers
    lr = float(args['--lr'])
    model_params = model.parameters()
    for param in model_params:
        print(type(param.data), param.size())
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, amsgrad=True)

    # TODO: [remove this] temporaily save inited model for testing
    model.save(model_save_path)
    print('save currently the best model to [%s]' % model_save_path)
    sps = []
    sp = spm.SentencePieceProcessor()
    for i in range(len(LANG_NAMES)):
        sp.Load('subword_files/%s.model' % LANG_NAMES[i])
        sps.append(sp)

    while True:
        epoch += 1

        for src_lang, tgt_lang, src_sents, tgt_sents in batch_iter(train_data, batch_size=train_batch_size):
            train_iter += 1
            batch_size = len(src_sents)

            if train_iter % 5 == 0:
                print("#", end="", flush=True)

            # start training routine
            #torch.cuda.empty_cache()
            optimizer.zero_grad()
            loss_v, _ = model(src_lang, tgt_lang, src_sents, tgt_sents)
            loss = torch.sum(loss_v)
            loss.backward()
            torch.nn.utils.clip_grad_norm(model.parameters(), clip_grad)
            optimizer.step()

            report_loss += float(loss)
            cum_loss += float(loss)
            del loss
            with torch.no_grad():
                tgt_words_num_to_predict = sum(len(s[1:]) for s in tgt_sents)  # omitting leading `<s>`
                report_tgt_words += tgt_words_num_to_predict
                cumulative_tgt_words += tgt_words_num_to_predict
                report_examples += batch_size
                cumulative_examples += batch_size

                if train_iter % log_every == 0:
                    print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f '
                          'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' %
                          (epoch, train_iter, report_loss / report_examples, math.exp(report_loss / report_tgt_words),
                           cumulative_examples, report_tgt_words / (time.time() - train_time), time.time() - begin_time),
                          flush=True)

                    train_time = time.time()
                    report_loss = report_tgt_words = report_examples = 0.

                # the following code performs validation on dev set, and controls the learning schedule
                # if the dev score is better than the last check point, then the current model is saved.
                # otherwise, we allow for that performance degeneration for up to `--patience` times;
                # if the dev score does not increase after `--patience` iterations, we reload the previously
                # saved best model (and the state of the optimizer), halve the learning rate and continue
                # training. This repeats for up to `--max-num-trial` times.
                if train_iter % valid_niter == 0:
                    print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' %
                          (epoch, train_iter, cum_loss / cumulative_examples, np.exp(cum_loss / cumulative_tgt_words),
                           cumulative_examples))

                    cum_loss = cumulative_examples = cumulative_tgt_words = 0.
                    valid_num += 1

                    print('begin validation ... size %d' % len(dev_data))

                    # set model to evaluate mode
                    model.eval()
                    # compute dev. ppl and bleu
                    # dev batch size can be a bit larger
                    dev_ppl, output, tgt_sents = model.evaluate_ppl(dev_data, batch_size=128)
                    dev_data_src, _ = get_corpus_ids(src_lang, tgt_lang, data_type='dev', is_tgt=False, is_train=False)
                    top_hypotheses = [Hypothesis(sps[tgt_lang].DecodeIds(sent).split(' '), 1)
                                      for sent in output]
                    bleu_score = \
                        compute_corpus_level_bleu_score([sps[tgt_lang].DecodeIds(sent).split(' ')
                                                         for sent in tgt_sents], top_hypotheses)
                    print(f'################ Corpus BLEU: {bleu_score} ###########################')
                    # set model back to training mode
                    model.train()
                    valid_metric = -dev_ppl

                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl))

                    is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                    hist_valid_scores.append(valid_metric)

                    if is_better:
                        patience = 0
                        print('save currently the best model to [%s]' % model_save_path)
                        model.save(model_save_path)
                        torch.save(optimizer, optimizer_save_path)

                    elif patience < int(args['--patience']):
                        patience += 1
                        print('hit patience %d' % patience)

                        if patience == int(args['--patience']):
                            num_trial += 1
                            print('hit #%d trial' % num_trial)
                            if num_trial == int(args['--max-num-trial']):
                                print('early stop!')
                                exit(0)

                            # load model
                            model = model.load(model_save_path)
                            optimizer = torch.load(optimizer_save_path)

                            # decay learning rate, and restore from previously best checkpoint
                            lr = lr * float(args['--lr-decay'])
                            for param_group in optimizer.param_groups:
                                param_group['lr'] = lr
                            print('load previously best model and decay learning rate to %f' % lr)

                            # reset patience
                            patience = 0

                    if epoch == int(args['--max-epoch']):
                        print('reached maximum number of epochs!')
                        exit(0)


def beam_search(model: MultiNMT, test_data_src: List[List[int]], src_lang: int, tgt_lang: int, \
                beam_size: int, max_decoding_time_step: int) -> List[List[Hypothesis]]:
    hypotheses = []
    for src_sent in tqdm(test_data_src, desc='Decoding', file=sys.stdout):
        example_hyps = model.beam_search(src_sent, src_lang, tgt_lang, beam_size=beam_size,
                                         max_decoding_time_step=max_decoding_time_step)

        hypotheses.append(example_hyps)

    return hypotheses


def compute_corpus_level_bleu_score(references: List[List[str]], hypotheses: List[Hypothesis]) -> float:
    """
    Given decoding results and reference sentences, compute corpus-level BLEU score

    Args:
        references: a list of gold-standard reference target sentences
        hypotheses: a list of hypotheses, one for each reference

    Returns:
        bleu_score: corpus-level BLEU score
    """
    if references[0][0] == '<s>':
        references = [ref[1:-1] for ref in references]

    bleu_score = corpus_bleu([[ref] for ref in references],
                             [hyp.value for hyp in hypotheses])

    return bleu_score


def decode(args: Dict[str, str]):
    """
    performs decoding on a test set, and save the best-scoring decoding results.
    If the target gold-standard sentences are given, the function also computes
    corpus-level BLEU score.
    """

    src_lang = args['SRC_LANG']
    tgt_lang = args['TGT_LANG']
    src_lang_idx = LANG_INDICES[src_lang]
    tgt_lang_idx = LANG_INDICES[tgt_lang]

    model_path = args['MODEL_PATH']
    output_file = args['OUTPUT_FILE']

    test_data_src, _ = get_corpus_ids(src_lang_idx, tgt_lang_idx, data_type='test', is_tgt=False, is_train=False)
    # test_data_tgt = get_corpus_ids(src_lang_idx, tgt_lang_idx, data_type='test', is_tgt=True)

    print(f"load model from {model_path}")
    model = MultiNMT.load(model_path)

    # set model to evaluate mode
    model.eval()

    hypotheses = beam_search(model, test_data_src, src_lang_idx, tgt_lang_idx,
                             beam_size=int(args['--beam-size']),
                             max_decoding_time_step=int(args['--max-decoding-time-step']))

    top_hypotheses = [hyps[0].value for hyps in hypotheses]
    translated_text = decode_corpus_ids(lang_name=tgt_lang, sents=top_hypotheses)

    with open(output_file, 'w') as f:
        for sent in translated_text:
            f.write(sent + '\n')


def main():
    args = docopt(__doc__)

    # seed the random number generator (RNG), you may
    # also want to seed the RNG of tensorflow, pytorch, dynet, etc.
    seed = int(args['--seed'])
    np.random.seed(seed * 13 // 7)
    torch.manual_seed(seed * 13 // 7)

    if args['train']:
        train(args)
    elif args['decode']:
        decode(args)
    else:
        raise RuntimeError(f'invalid mode')


if __name__ == '__main__':
    main()
