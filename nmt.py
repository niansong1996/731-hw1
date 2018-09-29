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

from utils import read_corpus, batch_iter
from vocab import Vocab, VocabEntry
from embed import corpus_to_indices, indices_to_corpus

import torch
import torch.nn as nn
import torch.tensor as Tensor
import torch.nn.functional as F




Hypothesis = namedtuple('Hypothesis', ['value', 'score'])


class NMT(nn.Module):

    def __init__(self, embed_size, hidden_size, vocab, dropout_rate=0.2):
        super(NMT, self).__init__()

        self.embed_size = embed_size
        self.hidden_size = hidden_size
        self.dropout_rate = dropout_rate
        self.vocab = vocab
        input_size = len(vocab.src)
        self.output_size = len(vocab.tgt)

        # initialize neural network layers...
        # could add drop-out and bidirectional arguments
        # could also change the units to GRU
        self.encoder_embed = nn.Embedding(input_size, embed_size)
        self.encoder_lstm = nn.LSTM(embed_size, hidden_size)

        self.decoder_embed = nn.Embedding(self.output_size, embed_size)
        self.decoder_lstm = nn.LSTM(embed_size, hidden_size)
        self.decoder_out = nn.Linear(hidden_size, self.output_size)
        self.decoder_softmax = nn.LogSoftmax(dim=1)

        self.criterion = nn.NLLLoss()

    def forward(self, src_sents: List[List[str]], tgt_sents: List[List[str]]) -> Tensor:
        """
        take a mini-batch of source and target sentences, compute the log-likelihood of 
        target sentences.

        Args:
            src_sents: list of source sentence tokens
            tgt_sents: list of target sentence tokens, wrapped by `<s>` and `</s>`

        Returns:
            scores: a variable/tensor of shape (batch_size, ) representing the 
                log-likelihood of generating the gold-standard target sentence for 
                each example in the input batch
        """
        src_encodings, decoder_init_state = self.encode(src_sents)
        scores = self.decode(src_encodings, decoder_init_state, tgt_sents)

        return scores

    def encode(self, src_sents: List[List[str]]) -> Tuple[Tensor, Any]:
        """
        Use a GRU/LSTM to encode source sentences into hidden states

        Args:
            src_sents: list of source sentence tokens

        Returns:
            src_encodings: hidden states of tokens in source sentences, this could be a variable 
                with shape (batch_size, source_sentence_length, encoding_dim), or in orther formats
            decoder_init_state: decoder GRU/LSTM's initial state, computed from source encodings
        """

        batch_size = len(src_sents)

        # first the the vecotrized representation of the batch
        input = corpus_to_indices(self.vocab.src, src_sents)
        embedded = self.encoder_embed(input)
        output = embedded.transpose(0, 1)
        output, (hidden, _) = self.encoder_lstm(output)
        src_encodings = output
        decoder_init_state = hidden

        return src_encodings, decoder_init_state

    def decode(self, src_encodings: Tensor, decoder_init_state: Any, tgt_sents: List[List[str]]) -> Tensor:
        """
        Given source encodings, compute the log-likelihood of predicting the gold-standard target
        sentence tokens

        Args:
            src_encodings: hidden states of tokens in source sentences
            decoder_init_state: decoder GRU/LSTM's initial state
            tgt_sents: list of gold-standard target sentences, wrapped by `<s>` and `</s>`

        Returns:
            scores: could be a variable of shape (batch_size, ) representing the 
                log-likelihood of generating the gold-standard target sentence for 
                each example in the input batch
                (extra note) we need this to be in the shape of (batch_size, output_vocab_size) 
                for beam search
        """
        batch_size = len(tgt_sents)
        loss_mask = torch.ones(batch_size)  # the mask for calculating loss
        input = corpus_to_indices(self.vocab.tgt, [["<s>"] for i in range(batch_size)])
        # dim = (batch_size, 1 (single_word), embed_size)
        embeded = self.decoder_embed(input)
        # dim = (1 (single_word), batch_size, embed_size)
        decoder_input = embeded.transpose(0, 1)
        # decoder_input = F.relu(decoder_input)
        scores = torch.zeros(batch_size)
        h_t = decoder_init_state
        c_t = torch.zeros(decoder_init_state.shape)
        zero_mask = torch.zeros(batch_size)
        one_mask = torch.ones(batch_size)
        # convert the target sentences to indices, dim = (batch_size, max_sent_len)
        target_output = corpus_to_indices(self.vocab.tgt, tgt_sents)
        # skip the '<s>' in the tgt_sents since the output starts from the word after '<s>'
        for i in range(1, target_output.shape[1]):
            _, (h_t, c_t) = self.decoder_lstm(decoder_input, (h_t, c_t))
            vocab_size_output = self.decoder_out(h_t)
            # dim = (1, batch_size, vocab_size)
            top_v, top_i = torch.topk(vocab_size_output, 1, dim=2)  # pick the word with the top score for each batch
            input_indices = top_i.squeeze().detach() # dim = (batch_size) after squeeze
            # dim = (batch_size, vocab_size)
            softmax_output = self.decoder_softmax(vocab_size_output).squeeze()
            # dim = (batch_size)
            target_word_idices = target_output[:,i].reshape(batch_size)
            score_delta = self.criterion(softmax_output, target_word_idices)
            # mask 0 if eos is reached
            eos_mask = torch.where((input_indices != self.vocab.tgt.word2id['</s>']) * \
            (target_word_idices != self.vocab.tgt.word2id['</s>']), one_mask, zero_mask)
            loss_mask = loss_mask * eos_mask
            # update scores
            scores = scores + score_delta * loss_mask
            # get the input for the next layer from the embed of the target words
            decoder_input = self.decoder_embed(target_word_idices).view(-1, batch_size, self.embed_size)
        return scores

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
        hypotheses = []
        hypotheses_cand = [([["<s>"]], 0)]  # candidates for best hypotheses
        _, decoder_init_state = self.encode([src_sent])
        h_t = decoder_init_state
        c_t = torch.zeros(decoder_init_state.shape)
        for i in range(max_decoding_time_step):
            # get the new input words from the last word of every candidate
            input_words = [sent[-1] for sent in hypotheses_cand[0]]
            input = corpus_to_indices(self.vocab.tgt, input_words)
            # dim = (len(hypotheses_cand), 1 (single_word), embed_size)
            embeded = self.decoder_embed(input)
            # dim = (1 (single_word), len(hypotheses_cand), embed_size)
            decoder_input = embeded.transpose(0, 1)
            _, (h_t, c_t) = self.decoder_lstm(decoder_input, (h_t, c_t))
            # dim = (1 (single_word), len(hypotheses_cand), vocab_size)
            vocab_size_output = self.decoder_out(h_t)
            # dim = (1 (single_word), len(hypotheses_cand), beam_size)
            top_v, top_i = torch.topk(vocab_size_output, beam_size, dim=2)
            # dim = (len(hypotheses_cand), vocab_size)
            softmax_output = self.decoder_softmax(vocab_size_output).squeeze()
            new_hypotheses_cand = []
            for candidate_idx in range(len(hypotheses_cand)):
                sent, log_likelihood = hypotheses_cand[candidate_idx]
                for word_idx in top_i[0][candidate_idx]:
                    new_hypotheses_cand.append((sent + [self.vocab.tgt.id2word(word_idx)],
                                                log_likelihood + softmax_output[candidate_idx][word_idx]))
            # combine ending sentences with new candidates to form new hypotheses
            hypotheses = [x for x in hypotheses if x[0][-1] == '</s>'] + new_hypotheses_cand
            hypotheses = sorted(hypotheses, key=lambda x: x[1], reverse=True)[:beam_size]
            hypotheses_cand = [x for x in hypotheses if x[0][-1] != '</s>']
            if len(hypotheses_cand) == 0:
                break
        return hypotheses

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

        for src_sents, tgt_sents in batch_iter(dev_data, batch_size):
            loss = -model(src_sents, tgt_sents).sum()

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

    vocab = pickle.load(open(args['--vocab'], 'rb'))

    model = NMT(embed_size=int(args['--embed-size']),
                hidden_size=int(args['--hidden-size']),
                dropout_rate=float(args['--dropout']),
                vocab=vocab)

    num_trial = 0
    train_iter = patience = cum_loss = report_loss = cumulative_tgt_words = report_tgt_words = 0
    cumulative_examples = report_examples = epoch = valid_num = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin Maximum Likelihood training')

    # set the optimizers
    learning_rate = float(args['--lr'])
    model_params = model.parameters()
    for param in model_params:
        print(type(param.data), param.size())
    optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)

    while True:
        epoch += 1

        for src_sents, tgt_sents in batch_iter(train_data, batch_size=train_batch_size, shuffle=True):
            train_iter += 1
            print("#", end="")
            batch_size = len(src_sents)

            # (batch_size)
            # start training routine
            optimizer.zero_grad()
            loss_v = model(src_sents, tgt_sents)
            # _, loss_v = model.encode(src_sents)
            loss = torch.sum(loss_v)
            loss.backward()
            optimizer.step()

            report_loss += loss
            cum_loss += loss

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
                                                                                         time.time() - begin_time), file=sys.stderr)

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
                                                                                         cumulative_examples), file=sys.stderr)

                cum_loss = cumulative_examples = cumulative_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)

                # compute dev. ppl and bleu
                dev_ppl = model.evaluate_ppl(dev_data, batch_size=128)   # dev batch size can be a bit larger
                valid_metric = -dev_ppl

                print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl), file=sys.stderr)

                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                hist_valid_scores.append(valid_metric)

                if is_better:
                    patience = 0
                    print('save currently the best model to [%s]' % model_save_path, file=sys.stderr)
                    model.save(model_save_path)

                    # You may also save the optimizer's state
                elif patience < int(args['--patience']):
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)

                    if patience == int(args['--patience']):
                        num_trial += 1
                        print('hit #%d trial' % num_trial, file=sys.stderr)
                        if num_trial == int(args['--max-num-trial']):
                            print('early stop!', file=sys.stderr)
                            exit(0)

                        # decay learning rate, and restore from previously best checkpoint
                        lr = lr * float(args['--lr-decay'])
                        print('load previously best model and decay learning rate to %f' % lr, file=sys.stderr)

                        # load model
                        model_save_path

                        print('restore parameters of the optimizers', file=sys.stderr)
                        # You may also need to load the state of the optimizer saved before

                        # reset patience
                        patience = 0

                if epoch == int(args['--max-epoch']):
                    print('reached maximum number of epochs!', file=sys.stderr)
                    exit(0)


def beam_search(model: NMT, test_data_src: List[List[str]], beam_size: int, max_decoding_time_step: int) -> List[List[Hypothesis]]:
    was_training = model.training

    hypotheses = []
    for src_sent in tqdm(test_data_src, desc='Decoding', file=sys.stdout):
        example_hyps = model.beam_search(src_sent, beam_size=beam_size, max_decoding_time_step=max_decoding_time_step)

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

    print(f"load model from {args['MODEL_PATH']}", file=sys.stderr)
    model = NMT.load(args['MODEL_PATH'])

    hypotheses = beam_search(model, test_data_src,
                             beam_size=int(args['--beam-size']),
                             max_decoding_time_step=int(args['--max-decoding-time-step']))

    if args['TEST_TARGET_FILE']:
        top_hypotheses = [hyps[0] for hyps in hypotheses]
        bleu_score = compute_corpus_level_bleu_score(test_data_tgt, top_hypotheses)
        print(f'Corpus BLEU: {bleu_score}', file=sys.stderr)

    with open(args['OUTPUT_FILE'], 'w') as f:
        for src_sent, hyps in zip(test_data_src, hypotheses):
            top_hyp = hyps[0]
            hyp_sent = ' '.join(top_hyp.value)
            f.write(hyp_sent + '\n')


def main():
    args = docopt(__doc__)

    # seed the random number generator (RNG), you may
    # also want to seed the RNG of tensorflow, pytorch, dynet, etc.
    seed = int(args['--seed'])
    np.random.seed(seed * 13 // 7)

    if args['train']:
        train(args)
    elif args['decode']:
        decode(args)
    else:
        raise RuntimeError(f'invalid mode')


if __name__ == '__main__':
    main()
