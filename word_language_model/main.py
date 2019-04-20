# coding: utf-8
import argparse
import time
import math
import os
import torch
import torch.nn as nn
import torch.onnx
import numpy as np

import data
import model

parser = argparse.ArgumentParser(description='PyTorch Wikitext-2 RNN/LSTM Language Model')
parser.add_argument('--data', type=str, default='./data/wikitext-2',
                    help='location of the data corpus')
parser.add_argument('--model', type=str, default='LSTM',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU)')
parser.add_argument('--emsize', type=int, default=200,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=200,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=2,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=20,
                    help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=40,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=20, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=35,
                    help='sequence length')
parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')
parser.add_argument('--log-interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str, default='model.pt',
                    help='path to save the final model')
parser.add_argument('--onnx-export', type=str, default='',
                    help='path to export the final model in onnx format')
args = parser.parse_args()

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

device = torch.device("cuda" if args.cuda else "cpu")

###############################################################################
# Load data
###############################################################################

corpus = data.Corpus(args.data)

# Starting from sequential data, batchify arranges the dataset into columns.
# For instance, with the alphabet as the sequence and batch size 4, we'd get
# ┌ a g m s ┐
# │ b h n t │
# │ c i o u │
# │ d j p v │
# │ e k q w │
# └ f l r x ┘.
# These columns are treated as independent by the model, which means that the
# dependence of e. g. 'g' on 'f' can not be learned, but allows more efficient
# batch processing.

def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    return data.to(device)

eval_batch_size = 10
train_data = batchify(corpus.train, args.batch_size)
val_data = batchify(corpus.valid, eval_batch_size)
test_data = batchify(corpus.test, eval_batch_size)

###############################################################################
# Build the model
###############################################################################

ntokens = len(corpus.dictionary)
#print("ntokens...", ntokens)
#print("args.bptt...", args.bptt)

#model = model.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, args.dropout, args.tied).to(device)
from torch.nn import Transformer 
model = Transformer(ntokens, ntokens+1, nhead=4, d_model=args.emsize, num_encoder_layers=2, num_decoder_layers=2, d_ff=256)
#model.cuda()

### Change the generator in transformer to nn.Linear(d_model, vocab)
model.generator = nn.Linear(args.emsize, ntokens)

### Modify embedding in encoder and decoder
model.encoder.src_embed = nn.Embedding(ntokens, args.emsize)
model.decoder.tgt_embed = nn.Embedding(ntokens, args.emsize)
### Remove pos_encoder
model.encoder.pos_encoder = None

model.to(device)

#print("ntokens is ", ntokens)
#print("corpus.dictionary", corpus.dictionary.word2idx)

criterion = nn.CrossEntropyLoss()

###############################################################################
# Training code
###############################################################################

def repackage_hidden(h):
    """Wraps hidden states in new Tensors, to detach them from their history."""
    if isinstance(h, torch.Tensor):
        return h.detach()
    else:
        return tuple(repackage_hidden(v) for v in h)

def subsequent_mask(sz):
    attn_shape = (1, sz, sz)
    mask = np.triu(np.ones(attn_shape), k=1).astype('uint8')
    mask = torch.from_numpy(mask) == 0
    mask = mask[0].float().masked_fill(mask == 0, float('-inf')).masked_fill(mask == 1, float(0.0)).view(sz, -1)
    mask
    return mask

# get_batch subdivides the source data into chunks of length args.bptt.
# If source is equal to the example output of the batchify function, with
# a bptt-limit of 2, we'd get the following two Variables for i = 0:
# ┌ a g m s ┐ ┌ b h n t ┐
# └ b h n t ┘ └ c i o u ┘
# Note that despite the name of the function, the subdivison of data is not
# done along the batch dimension (i.e. dimension 1), since that was handled
# by the batchify function. The chunks are along dimension 0, corresponding
# to the seq_len dimension in the LSTM.

def get_batch(source, i):
    seq_len = min(args.bptt, len(source) - 1 - i)
    batch_size = source.size()[1]
    data = source[i:i+seq_len]
#    decoder_inp = source[i:i+seq_len-1]
#    decoder_inp = torch.cat((torch.ones(1, batch_size).long()*-1, decoder_inp), dim=0)
    target = source[i+1:i+1+seq_len].view(-1)
#    print("data...", data.size(), data)
#    print("target original...", source[i+1:i+1+seq_len])
#    print("decoder_inp...", decoder_inp.size(), decoder_inp)
#    print("target...", target.size(), target)
#    target = source[-1].view(-1)
#    return data, target, decoder_inp
    return data, target

#def evaluate_model(model, src_data):
#	tgt_input = src_data[0].unsqueeze(0)
#	for _i in range(0, src_data.size()[0]):
#		print("we are not evaluate the model on step ", _i)
#		print("src_data...",src_data.size(),src_data)
#		print("tgt_input...",tgt_input.size(),tgt_input)
#		tgt_input = model(src_data, tgt_input)
#	return tgt_input

def evaluate(data_source):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0.
    ntokens = len(corpus.dictionary)
#    hidden = model.init_hidden(eval_batch_size)

    tgt_mask = subsequent_mask(args.bptt).to(device)

    with torch.no_grad():
        for i in range(0, data_source.size(0) - 1, args.bptt):
            data, targets = get_batch(data_source, i)

            if args.bptt > len(data_source) - 1 - i:
                tgt_mask = subsequent_mask(len(data_source) - 1 - i).to(device)

#            print("data...", data.size(), data)
#            print("tgt_mask...", tgt_mask.size(), tgt_mask)
#            print("targets...", targets.size(), targets)
#            output, hidden = model(data, hidden)
            output = model(data, data, tgt_mask=tgt_mask)
            output_flat = output.view(-1, ntokens)
#            output_flat = output[-1].view(-1, ntokens)
            total_loss += len(data) * criterion(output_flat, targets).item()
#            hidden = repackage_hidden(hidden)
    return total_loss / (len(data_source) - 1)


def train():
    # Turn on training mode which enables dropout.
    model.train()
    total_loss = 0.
    start_time = time.time()
    ntokens = len(corpus.dictionary)
#    hidden = model.init_hidden(args.batch_size)
  
    tgt_mask = subsequent_mask(args.bptt).to(device)

    for batch, i in enumerate(range(0, train_data.size(0) - 1, args.bptt)):
#        data, targets, decoder_trg = get_batch(train_data, i)
        data, targets = get_batch(train_data, i)

        if args.bptt > len(train_data) - 1 - i:
            tgt_mask = subsequent_mask(len(train_data) - 1 - i).to(device)
#        print("targets...", targets.size(), targets)
#        print("data...", data.size(), data)
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
#        hidden = repackage_hidden(hidden)
        model.zero_grad()
#        output, hidden = model(data, hidden)

#        print("data...",data.size())
#        print("tgt_mask...",tgt_mask.size(),tgt_mask)
#        print("decoder_trg...", decoder_trg.size())
#        print("data and decoder_trg is same...", data[:-1]==decoder_trg[1:])
        output = model(data, data, tgt_mask=tgt_mask)
#        print("ntokens...", ntokens)
#        print("output...", output.size(), output)
#        print("targets...", targets.size(), targets)
#        output = output[-1]

#        print("output.view(-1, ntokens)...", output.view(-1, ntokens).size(), output.view(-1, ntokens))
#        print("hidden...", hidden)
#        print("targets...", targets.size(), targets)


        loss = criterion(output.view(-1, ntokens), targets)

#        loss = criterion(output[-1].view(-1, ntokens), targets[-1])
        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
        for p in model.parameters():
            p.data.add_(-lr, p.grad.data)

        total_loss += loss.item()

        if batch % args.log_interval == 0 and batch > 0:
            cur_loss = total_loss / args.log_interval
            elapsed = time.time() - start_time
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                    'loss {:5.2f} | ppl {:8.2f}'.format(
                epoch, batch, len(train_data) // args.bptt, lr,
                elapsed * 1000 / args.log_interval, cur_loss, math.exp(cur_loss)))
            total_loss = 0
            start_time = time.time()


def export_onnx(path, batch_size, seq_len):
    print('The model is also exported in ONNX format at {}'.
          format(os.path.realpath(args.onnx_export)))
    model.eval()
    dummy_input = torch.LongTensor(seq_len * batch_size).zero_().view(-1, batch_size).to(device)
    hidden = model.init_hidden(batch_size)
    torch.onnx.export(model, (dummy_input, hidden), path)


# Loop over epochs.
lr = args.lr
best_val_loss = None

# At any point you can hit Ctrl + C to break out of training early.
try:
    for epoch in range(1, args.epochs+1):
        epoch_start_time = time.time()
        train()
        val_loss = evaluate(val_data)
        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | '
                'valid ppl {:8.2f}'.format(epoch, (time.time() - epoch_start_time),
                                           val_loss, math.exp(val_loss)))
        print('-' * 89)
        # Save the model if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            with open(args.save, 'wb') as f:
                torch.save(model, f)
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0
except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')

#
## Load the best saved model.
#with open(args.save, 'rb') as f:
#    model = torch.load(f)
#    # after load the rnn params are not a continuous chunk of memory
#    # this makes them a continuous chunk, and will speed up forward pass
#    model.rnn.flatten_parameters()
#

# Run on test data.
test_loss = evaluate(test_data)
print('=' * 89)
print('| End of training | test loss {:5.2f} | test ppl {:8.2f}'.format(
    test_loss, math.exp(test_loss)))
print('=' * 89)

if len(args.onnx_export) > 0:
    # Export the model in ONNX format.
    export_onnx(args.onnx_export, batch_size=1, seq_len=args.bptt)
