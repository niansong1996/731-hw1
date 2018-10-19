# coding=utf-8

"""
A very basic implementation of neural machine translation

Usage:
    nmt.py train --train-src=<file> --train-tgt=<file> --dev-src=<file> --dev-tgt=<file> --vocab=<file> [options]
    nmt.py decode [options] MODEL_PATH TEST_SOURCE_FILE OUTPUT_FILE
    nmt.py decode [options] MODEL_PATH TEST_SOURCE_FILE TEST_TARGET_FILE OUTPUT_FILE

Options:
    -h --help                               show this screen.
    --cuda                                  use GPU
    --train-src=<file>                      train source file
    --train-tgt=<file>                      train target file
    --dev-src=<file>                        dev source file
    --dev-tgt=<file>                        dev target file
    --vocab=<file>                          vocab file
    --seed=<int>                            seed [default: 0]
    --batch-size=<int>                      batch size [default: 32]
    --embed-size=<int>                      embedding size [default: 256]
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
    --dropout=<float>                       dropout [default: 0.2]
    --max-decoding-time-step=<int>          maximum number of decoding time steps [default: 70]
"""

import math
import pickle
import sys
import time
from collections import namedtuple

import numpy as np
from typing import *
from docopt import docopt
from tqdm import tqdm
from nltk.translate.bleu_score import corpus_bleu, sentence_bleu, SmoothingFunction

from CPG import CPG
from Decoder import Decoder
from utils import read_corpus, batch_iter, load_matrix
from vocab import Vocab, VocabEntry
from embed import corpus_to_indices, indices_to_corpus

import torch
import torch.nn as nn
import torch.tensor as Tensor
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from torch.nn.utils.rnn import pack_padded_sequence
from torch.nn.utils.rnn import pad_packed_sequence


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
if torch.cuda.is_available():
    print(torch.cuda.get_device_name(torch.cuda.current_device()))

Hypothesis = namedtuple('Hypothesis', ['value', 'score'])


class NMT(nn.Module):

    def __init__(self, embed_size, hidden_size, vocab):
        super(NMT, self).__init__()

        self.vocab = vocab
        src_vocab_size = len(vocab.src)
        tgt_vocab_size = len(vocab.tgt)
        self.DECODER_PAD_IDX = self.vocab.tgt.word2id['<pad>']

        # initialize neural network layers...
        # could add drop-out and bidirectional arguments
        # could also change the units to GRU
        self.NUM_LAYER = 2
        self.NUM_DIR = 2
        self.BIDIR = self.NUM_DIR == 2

        decoder_hidden_size = self.NUM_DIR * hidden_size
        self.cpg = CPG()

    def forward(self, src_sents: List[List[str]], tgt_sents: List[List[str]]) -> Tensor:
        src_encodings, decoder_init_state = self.encode(src_sents)
        scores = self.decode(src_encodings, decoder_init_state, tgt_sents)

        return scores

    def encode(self, src_sents: List[List[str]]) -> Tuple[Tensor, Any]:
        # first the the vecotrized representation of the batch; dim = (batch_size, max_src_len)
        sent_length = torch.tensor([len(sent) for sent in src_sents]).to(device)
        sent_indices = self.vocab.src.words2indices(src_sents)
        sent_indices_padded = pad_sequence([torch.tensor(sent) for sent in sent_indices]).to(device)
        # embed padded seq

    def beam_search(self, src_sent: List[str], beam_size: int=5, max_decoding_time_step: int=70) -> List[Hypothesis]:
        """
        Given a single source sentence, perform beam search

        Args:
            src_sent: a single tokenized source sentence
            beam_size: beam size
            max_decoding_time_step: maximum number of time steps to unroll the decoding RNN

        Returns:
            hypotheses: a list of hypothesis, each hypothesis has two fields:
                value: List[str]: the decoded target sentence, represented as a list of words
                score: float: the log-likelihood of the target sentence
        """
        with torch.no_grad():
            # dim = (1, 1, embed_size)
            src_encodings, (h_n, c_n) = self.encode([src_sent])
            # dim = (1, 1, embed_size)
            h_t_0, c_t_0, attn_0 = Decoder.init_decoder_step_input(device, src_encodings)
            # candidates for best hypotheses
            hypotheses_cand = [(Hypothesis(['<s>'], 0), h_t_0, c_t_0, attn_0)]
            for i in range(max_decoding_time_step):
                new_hypotheses_cand = []
                for (sent, log_likelihood), h_t, c_t, attn in hypotheses_cand:
                    input_word = sent[-1]
                    # skip ended sentence
                    if input_word == '</s>':
                        # directly add ended sentence to new candidates
                        new_hypotheses_cand.append((Hypothesis(sent, log_likelihood), h_t, c_t, attn))
                        continue
                    # dim = (1, 1 (single_word), embed_size)
                    embeded = self.decoder_embed(corpus_to_indices(self.vocab.tgt, [[input_word]]).to(device))
                    # dim = (1 (single_word), 1, embed_size)
                    decoder_input = embeded.transpose(0, 1)
                    # softmax_output.shape = [vocab_size]
                    h_t, c_t, softmax_output, attn, a_t = self.decoder_step(src_encodings, decoder_input, h_t, c_t, attn)
                    # dim = (1, beam_size)
                    top_v, top_i = torch.topk(softmax_output.unsqueeze(0), beam_size, dim=1)
                    for word_idx_tensor in top_i[0]:
                        word_idx = word_idx_tensor.item()
                        hyp_word = self.vocab.tgt.id2word[word_idx]

                        # deal with unknown word
                        if hyp_word == '<unk>':
                            _, src_word_pos = torch.topk(a_t.squeeze(), 1)
                            src_word = src_sent[src_word_pos]
                            # use dictionary if possible, else use src_word
                            hyp_word = self.vocab.decoder_dict.get(src_word, src_word)

                        new_hyp = Hypothesis(sent + [hyp_word],
                                             log_likelihood + softmax_output[word_idx])
                        new_hypotheses_cand.append((new_hyp, h_t, c_t, attn))
                # combine ended sentences with new candidates to form new hypotheses
                hypotheses_cand = sorted(new_hypotheses_cand, key=lambda x: x[0].score, reverse=True)[:beam_size]
                # break if all sentences have ended
                if all(c[0].value[-1] == '</s>' for c in hypotheses_cand):
                    break
            return [c[0] for c in hypotheses_cand]

    def evaluate_ppl(self, dev_data: List[Any], batch_size: int=32):
        """
        Evaluate perplexity on dev sentences

        Args:
            dev_data: a list of dev sentences
            batch_size: batch size

        Returns:
            ppl: the perplexity on dev sentences
        """

        cum_loss = 0.
        cum_tgt_words = 0.

        # you may want to wrap the following code using a context manager provided
        # by the NN library to signal the backend to not to keep gradient information
        # e.g., `torch.no_grad()`
        with torch.no_grad():
            for src_sents, tgt_sents in batch_iter(dev_data, batch_size):
                loss = self(src_sents, tgt_sents).sum()

                cum_loss += loss
                tgt_word_num_to_predict = sum(len(s[1:]) for s in tgt_sents)  # omitting the leading `<s>`
                cum_tgt_words += tgt_word_num_to_predict

            ppl = np.exp(cum_loss / cum_tgt_words)

            return ppl

    @staticmethod
    def load(model_path: str):
        """
        Load a pre-trained model

        Returns:
            model: the loaded model
        """

        return torch.load(model_path)

    def save(self, path: str):
        """
        Save current model to file
        """
        torch.save(self, path)


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


def train(args: Dict[str, str]):
    train_data_src = read_corpus(args['--train-src'], source='src')
    train_data_tgt = read_corpus(args['--train-tgt'], source='tgt')

    dev_data_src = read_corpus(args['--dev-src'], source='src')
    dev_data_tgt = read_corpus(args['--dev-tgt'], source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))
    dev_data = list(zip(dev_data_src, dev_data_tgt))

    train_batch_size = int(args['--batch-size'])
    clip_grad = float(args['--clip-grad'])
    valid_niter = int(args['--valid-niter'])
    log_every = int(args['--log-every'])
    model_save_path = args['--save-to']
    optimizer_save_path = args['--save-opt']

    vocab = pickle.load(open(args['--vocab'], 'rb'))

    model = NMT(embed_size=int(args['--embed-size']),
                hidden_size=int(args['--hidden-size']),
                dropout_rate=float(args['--dropout']),
                vocab=vocab).to(device)

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
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    while True:
        epoch += 1

        for src_sents, tgt_sents in batch_iter(train_data, batch_size=train_batch_size, shuffle=True):
            train_iter += 1
            batch_size = len(src_sents)

            if train_iter % 5 == 0:
                print("#", end="", flush=True)

            # start training routine
            optimizer.zero_grad()
            loss_v = model(src_sents, tgt_sents)
            loss = torch.sum(loss_v)
            loss.backward()
            torch.nn.utils.clip_grad_norm(model.parameters(), clip_grad)
            optimizer.step()

            report_loss += loss
            cum_loss += loss.detach()

            tgt_words_num_to_predict = sum(len(s[1:]) for s in tgt_sents)  # omitting leading `<s>`
            report_tgt_words += tgt_words_num_to_predict
            cumulative_tgt_words += tgt_words_num_to_predict
            report_examples += batch_size
            cumulative_examples += batch_size

            if train_iter % log_every == 0:
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         math.exp(report_loss / report_tgt_words),
                                                                                         cumulative_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), flush=True)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.

            # the following code performs validation on dev set, and controls the learning schedule
            # if the dev score is better than the last check point, then the current model is saved.
            # otherwise, we allow for that performance degeneration for up to `--patience` times;
            # if the dev score does not increase after `--patience` iterations, we reload the previously
            # saved best model (and the state of the optimizer), halve the learning rate and continue
            # training. This repeats for up to `--max-num-trial` times.
            if train_iter % valid_niter == 0:
                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cumulative_examples,
                                                                                         np.exp(cum_loss / cumulative_tgt_words),
                                                                                         cumulative_examples))

                cum_loss = cumulative_examples = cumulative_tgt_words = 0.
                valid_num += 1

                print('begin validation ... size %d %d' % (len(dev_data), len(dev_data_src)))

                # set model to evaluate mode
                model.eval()
                # compute dev. ppl and bleu
                dev_ppl = model.evaluate_ppl(dev_data, batch_size=128)   # dev batch size can be a bit larger
                # set model back to training mode
                model.train()
                '''
                print("dev. ppl %f" % dev_ppl)
                dev_hyps = []
                for dev_src_sent in dev_data_src:
                    print(".", end="", flush=True)
                    dev_hyp_sent = model.beam_search(dev_src_sent)
                    dev_hyps.append(dev_hyp_sent[0])
                dev_bleu = compute_corpus_level_bleu_score(dev_data_tgt, dev_hyps)
                '''
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


def beam_search(model: NMT, test_data_src: List[List[str]], beam_size: int, max_decoding_time_step: int) \
        -> List[List[Hypothesis]]:
    hypotheses = []
    for src_sent in tqdm(test_data_src, desc='Decoding', file=sys.stdout):
        example_hyps = model.beam_search(src_sent, beam_size=beam_size,
                                         max_decoding_time_step=max_decoding_time_step)

        hypotheses.append(example_hyps)

    return hypotheses


def decode(args: Dict[str, str]):
    """
    performs decoding on a test set, and save the best-scoring decoding results. 
    If the target gold-standard sentences are given, the function also computes
    corpus-level BLEU score.
    """
    test_data_src = read_corpus(args['TEST_SOURCE_FILE'], source='src')
    if args['TEST_TARGET_FILE']:
        test_data_tgt = read_corpus(args['TEST_TARGET_FILE'], source='tgt')

    print(f"load model from {args['MODEL_PATH']}")
    model = NMT.load(args['MODEL_PATH'])

    vocab = pickle.load(open('data/vocab.bin', 'rb'))
    model.vocab = vocab
    # set model to evaluate mode
    model.eval()

    hypotheses = beam_search(model, test_data_src,
                             beam_size=int(args['--beam-size']),
                             max_decoding_time_step=int(args['--max-decoding-time-step']))

    if args['TEST_TARGET_FILE']:
        top_hypotheses = [hyps[0] for hyps in hypotheses]
        bleu_score = compute_corpus_level_bleu_score(test_data_tgt, top_hypotheses)
        print(f'Corpus BLEU: {bleu_score}')

    with open(args['OUTPUT_FILE'], 'w') as f:
        for src_sent, hyps in zip(test_data_src, hypotheses):
            top_hyp = hyps[0]
            hyp_sent = ' '.join(top_hyp.value)
            f.write(hyp_sent + '\n')


def main():
    args = docopt(__doc__)

    # seed the random number generator (RNG) for numpy and pytorch
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
