from typing import List

import torch
import torch.tensor as Tensor
import torch.nn as nn

from utils import assert_tensor_size


def unpack_weight(weight, input_size, hidden_size):
    assert (weight.shape == (4 * hidden_size * (input_size + hidden_size + 1 + 1), 1))

    W_x_shape = (input_size, 4 * hidden_size)
    W_h_shape = (hidden_size, 4 * hidden_size)
    b_x_shape = (1, 4 * hidden_size)
    b_h_shape = (1, 4 * hidden_size)

    W_x_0 = 0
    W_x_1 = W_x_shape[0] * W_x_shape[1]
    W_h_1 = W_x_1 + W_h_shape[0] * W_h_shape[1]
    b_x_1 = W_h_1 + b_x_shape[0] * b_x_shape[1]
    b_h_1 = b_x_1 + b_h_shape[0] * b_h_shape[1]

    W_x = weight[W_x_0:W_x_1].reshape(W_x_shape)
    W_h = weight[W_x_1:W_h_1].reshape(W_h_shape)
    b_x = weight[W_h_1:b_x_1].reshape(b_x_shape)
    b_h = weight[b_x_1:b_h_1].reshape(b_h_shape)

    return W_x, W_h, b_x, b_h


class Stack_FLSTMCell:
    def __init__(self, input_size, hidden_size, weights: List[List[Tensor]], num_layers=1):
        assert (len(weights) == num_layers)

        # init the size constants
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        # init the cells for each layer with weight references
        self.cells = []
        for i in range(num_layers):
            # only the input size of the first layer is the input size of the decoder
            cell_input_size = self.input_size if i == 0 else self.hidden_size
            cell = FLSTMCell(cell_input_size, self.hidden_size, weights[i])
            self.cells.append(cell)

    def __call__(self, X: Tensor, h_0: List[Tensor], c_0: List[Tensor]) \
            -> (List[Tensor], List[Tensor]):
        """
        Performs a step of stacked LSTM

        :param X: input
        :param h_0: a list of hidden layers of size num of layers, with the first layer at 0
        :param c_0: a list of memory cells of size num of layers, with the first cell at 0
        :return:
        """
        assert (len(h_0) == self.num_layers)
        assert (len(c_0) == self.num_layers)
        assert (X.shape[1] == self.input_size)

        # input and output for each layer
        input_x = X
        h_1 = []
        c_1 = []

        # do a forward pass sequentially with each layer
        for i in range(self.num_layers):
            cell = self.cells[i]
            h, c = cell(input_x, h_0[i], c_0[i])

            # get the results
            input_x = h
            h_1.append(h)
            c_1.append(c)

        assert (len(h_1) == self.num_layers)
        assert (len(c_1) == self.num_layers)

        return h_1, c_1


class FLSTMCell:
    def __init__(self, input_size, hidden_size, weights):
        # init the size constants
        self.input_size = input_size
        self.hidden_size = hidden_size
        W_x_shape = [input_size, 4 * hidden_size]
        W_h_shape = [hidden_size, 4 * hidden_size]
        b_x_shape = [1, 4 * hidden_size]
        b_h_shape = [1, 4 * hidden_size]
        # init the weights BY REFRENCE
        W_x, W_h, b_x, b_h = weights
        assert_tensor_size(W_x, W_x_shape)
        assert_tensor_size(W_h, W_h_shape)
        assert_tensor_size(b_x, b_x_shape)
        assert_tensor_size(b_h, b_h_shape)
        self.W_x = W_x
        self.W_h = W_h
        self.b_x = b_x
        self.b_h = b_h

    def __call__(self, X: Tensor, h_0: Tensor, c_0: Tensor) -> (Tensor, Tensor):
        """
        Performs a step of LSTM

        :param X: input embedding shape = [batch_size, input_size]
        :param c_0: cell state shape = [batch_size, hidden_size]
        :param h_0: hidden state shape = [batch_size, hidden_size]
        :param weights: W_x, W_h, b_x, b_h

        :return: (h_1, c_1) next cell state and hidden state shape = [batch_size, hidden_size]
        """
        hidden_size = h_0.shape[1]

        # [batch_size, 4*hidden_size]  =  [batch_size, input_size] * [input_size, 4*hidden_size]
        #                                  + [batch_size, hidden_size] * [hidden_size, 4*hidden_size]
        W_x_h_b = torch.mm(X, self.W_x) + torch.mm(h_0, self.W_h) + self.b_x + self.b_h

        # (batch_size, hidden_size)
        i = torch.sigmoid(W_x_h_b[:, 0:hidden_size])
        f = torch.sigmoid(W_x_h_b[:, hidden_size:2 * hidden_size])
        g = torch.tanh(W_x_h_b[:, 2 * hidden_size:3 * hidden_size])
        o = torch.sigmoid(W_x_h_b[:, 3 * hidden_size:4 * hidden_size])

        # (batch_size, hidden_size)
        c_1 = f * c_0 + i * g
        h_1 = o * torch.tanh(c_1)

        return h_1, c_1


if __name__ == '__main__':
    input_size_ = 256
    hidden_size_ = 512
    batch_size_ = 32

    c_0_ = torch.randn((batch_size_, hidden_size_))
    h_0_ = torch.randn((batch_size_, hidden_size_))
    X_ = torch.randn((batch_size_, input_size_))

    lstm = nn.LSTMCell(input_size_, hidden_size_)

    weights = []
    for param in lstm.parameters():
        weights.append(param.data)
    weights[2] = weights[2].unsqueeze(1)
    weights[3] = weights[3].unsqueeze(1)

    flstm = FLSTMCell(input_size_, hidden_size_, weights)

    lstm_output = lstm(X_, (h_0_, c_0_))
    flstm_output = flstm(X_, h_0_, c_0_)

    print(lstm_output)
    print(flstm_output)
