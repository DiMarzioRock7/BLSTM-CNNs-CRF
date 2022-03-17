import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from utils import init_embedding, init_lstm, init_linear


def log_sum_exp(vec):
    """
    This function calculates the score explained above for the forward algorithm
    vec 2D: 1 * size_tag
    """
    max_score = vec[0, argmax(vec)]
    max_score_broadcast = max_score.view(1, -1).expand(1, vec.size()[1])
    return max_score + torch.log(torch.sum(torch.exp(vec - max_score_broadcast)))


def argmax(vec):
    """ This function returns the max index in a vector """
    _, idx = torch.max(vec, 1)
    return idx.view(-1).data.tolist()[0]


class BiLSTM_CRF(nn.Module):
    def __init__(self, args, word2idx, char2idx, tag2idx, glove_word=None):
        """
        Input parameters from args:

                args = Dictionary that maps NER tags to indices
                word2idx = Dimension of word embeddings (int)
                tag2idx = hidden state dimension
                char2idx = Dictionary that maps characters to indices
                glove_word = Numpy array which provides mapping from word embeddings to word indices
        """
        super(BiLSTM_CRF, self).__init__()
        self.device = args.device
        self.START_TAG = args.START_TAG
        self.STOP_TAG = args.STOP_TAG
        self.word2idx = word2idx
        self.char2idx = char2idx
        self.tag2idx = tag2idx
        self.n_word = len(word2idx)
        self.n_char = len(char2idx)
        self.n_tag = len(tag2idx)

        self.max_len_word = args.max_len_word

        self.idx_pad_char = args.idx_pad_char
        self.idx_pad_word = args.idx_pad_word

        self.dim_emb_char = args.dim_emb_char
        self.dim_emb_word = args.dim_emb_word

        self.dim_out_char = args.dim_out_char  # dimension of the character embeddings
        self.dim_out_word = args.dim_out_word  # The hidden dimension of the LSTM layer (int)

        self.mode_char = args.mode_char
        self.mode_word = args.mode_word
        self.n_cnn_layer = args.n_cnn_layer

        # embedding layer
        self.embedding_char = nn.Embedding(self.n_char+1, self.dim_emb_char, padding_idx=self.idx_pad_char)
        init_embedding(self.embedding_char)

        if args.enable_pretrained:
            self.embedding_word = nn.Embedding.from_pretrained(torch.FloatTensor(glove_word), freeze=args.freeze_glove,
                                                               padding_idx=self.idx_pad_word)
        else:
            self.embedding_word = nn.Embedding(self.n_word+1, self.dim_emb_word)
            init_embedding(self.embedding_word)

        # character encoder
        if self.mode_char == 'lstm':
            self.lstm_char = nn.LSTM(self.dim_emb_char, self.dim_out_char, num_layers=1, batch_first=True,
                                     bidirectional=True)
            init_lstm(self.lstm_char)
        elif self.mode_char == 'cnn':
            self.conv_char = nn.Conv2d(in_channels=1, out_channels=self.dim_out_char * 2,
                                      kernel_size=(3, self.dim_emb_char), padding=(2, 0))
            init_linear(self.conv_char)
        else:
            raise Exception('Character encoder mode unknown...')
        self.dropout1 = nn.Dropout(args.dropout)

        # word encoder
        if self.mode_word == 'lstm':
            self.lstm_word = nn.LSTM(self.dim_emb_word + self.dim_out_char * 2, self.dim_out_word, batch_first=True,
                                     bidirectional=True)
            init_lstm(self.lstm_word)

        elif self.mode_word == 'cnn1':
            self.conv_word = nn.Sequential(
                nn.Conv2d(in_channels=1, out_channels=self.dim_out_char * 2, kernel_size=(3, self.dim_emb_char),
                          padding=(1, 1)),
                nn.MaxPool2d()
            )

        elif self.mode_word == 'cnn2':
            self.conv_word = nn.Sequential(
                nn.Conv2d(in_channels=1, out_channels=self.dim_out_char * 2, kernel_size=(3, self.dim_emb_char),
                          padding=(1, 1)),
                nn.Conv2d(in_channels=self.dim_out_char * 2, out_channels=self.dim_out_char * 2,
                          kernel_size=(3, self.dim_emb_char), padding=(1, 1)),
            )

        elif self.mode_word == 'cnn3':
            self.conv_word = nn.Sequential(
                nn.Conv2d(in_channels=1, out_channels=self.dim_out_char * 2, kernel_size=(3, self.dim_emb_char),
                          padding=(1, 1)),
                nn.Conv2d(in_channels=self.dim_out_char * 2, out_channels=self.dim_out_char * 2,
                          kernel_size=(3, self.dim_emb_char), padding=(1, 1)),
                nn.Conv2d(in_channels=self.dim_out_char * 2, out_channels=self.dim_out_char * 2,
                          kernel_size=(3, self.dim_emb_char), padding=(1, 1)),
            )

        elif self.mode_word == 'cnn_d':
            self.conv_word = nn.Sequential(
                nn.Conv2d(in_channels=1, out_channels=self.dim_out_char * 2, kernel_size=(3, self.dim_emb_char),
                          padding=(1, 1)),
                nn.Conv2d(in_channels=self.dim_out_char * 2, out_channels=self.dim_out_char * 2,
                          kernel_size=(3, self.dim_emb_char), padding=(1, 1)),
                nn.Conv2d(in_channels=self.dim_out_char * 2, out_channels=self.dim_out_char * 2,
                          kernel_size=(3, self.dim_emb_char), padding=(1, 1)),
            )

        else:
            raise Exception('Word encoder mode '+self.mode_char+' unknown...')

        # for l in self.conv_word:
        #     if l

        self.dropout2 = nn.Dropout(args.dropout)

        # predictor
        self.hidden2tag = nn.Linear(self.dim_out_word * 2, self.n_tag)
        init_linear(self.hidden2tag)
        if args.enable_crf:
            self.transitions = nn.Parameter(torch.zeros(self.n_tag, self.n_tag))
            self.transitions.data[self.tag2idx[self.START_TAG], :] = -10000
            self.transitions.data[:, self.tag2idx[self.STOP_TAG]] = -10000

    def forward(self, words_batch, chars_batch, tags_batch, lens_word):

        # character-level modelling
        emb_chars = self.embedding_char(chars_batch)
        if self.mode_char == 'lstm':
            # covered padded characters that have 0 length to 1
            lens_char = (chars_batch != self.idx_pad_char).sum(dim=2)
            lens_char_covered = torch.where(lens_char == 0, 1, lens_char)
            packed_char = pack_padded_sequence(emb_chars.view(-1, self.max_len_word, self.dim_emb_char),
                                               lens_char_covered.view(-1).cpu(), batch_first=True, enforce_sorted=False)
            out_lstm_char, _ = self.lstm_char(packed_char)

            # return to (len_batch x len_sent x len_char x dim_emb_char)
            output_char, _ = pad_packed_sequence(out_lstm_char, batch_first=True, total_length=emb_chars.shape[-2])
            output_char = output_char * lens_char.view(-1, 1, 1).bool()
            output_char = output_char.reshape(emb_chars.shape[0], emb_chars.shape[1], self.max_len_word,
                                              self.dim_emb_char*2)

            enc_char = torch.cat(
                (torch.stack(
                    [sample[torch.arange(emb_chars.shape[1]).long(), lens-1, :self.dim_out_char]
                     for sample, lens in zip(output_char, lens_char)]),
                 torch.stack(
                     [sample[torch.arange(emb_chars.shape[1]).long(), lens*0, self.dim_out_char:]
                      for sample, lens in zip(output_char, lens_char)]))
                , dim=-1)

        elif self.mode_char == 'cnn':
            output_char = self.conv_char(emb_chars.unsqueeze(2).view(-1, 1, self.max_len_word, self.dim_emb_char))
            enc_char = F.max_pool2d(output_char, kernel_size=(output_char.size(2), 1)).view(words_batch.shape[0],
                                                                                            words_batch.shape[1],
                                                                                            self.dim_out_char * 2)
        else:
            raise Exception('Unknown character encoder: '+self.mode_char+'...')

        # load word embeddings
        emb_words = self.embedding_word(words_batch)
        emb_words_chars = torch.cat((emb_words, enc_char), dim=-1)
        emb_words_chars = self.dropout1(emb_words_chars)

        # word lstm
        if self.mode_word == 'lstm':
            packed_word = pack_padded_sequence(emb_words_chars, lens_word.cpu(), batch_first=True)
            out_lstm_word, _ = self.lstm_word(packed_word)
            output_word, _ = pad_packed_sequence(out_lstm_word, batch_first=True)

        elif self.mode_word[:3] == 'cnn':
            enc_word = self.conv_word(emb_words_chars).view(words_batch.shape[0], words_batch.shape[1],
                                                            self.dim_out_char * 2)

        else:
            raise Exception('Unknown word encoder: '+self.mode_word+'...')

        outputs = self.dropout2(enc_word)
        outputs = self.hidden2tag(outputs)
        return outputs

    def get_nll(self, words_batch, chars_batch, tags_batch, lens_batch):
        # sentence, tags is a list of ints
        # features is a 2D tensor, len(sentence) * self.tag_size
        feats = self.forward(words_batch, chars_batch, tags_batch, lens_batch)

        if self.use_crf:
            forward_score = self.forward_alg(feats)
            gold_score = self.score_sentence(feats, tags_batch)
            return forward_score - gold_score
        else:
            scores = F.cross_entropy(feats, tags_batch)
            return scores

    def forward_optimize(self, words_batch, chars_batch, tags_batch, lens_batch):
        """
        The function calls viterbi decode and generates the
        most probable sequence of tags for the sentence
        """

        # Get the emission scores from the BiLSTM
        feats = self.forward(words_batch, chars_batch, tags_batch, lens_batch)
        # viterbi to get tag_seq

        # Find the best path, given the features.
        if self.use_crf:
            score, tag_seq = self.viterbi_decode(self, feats)
        else:
            score, tag_seq = torch.max(feats, 1)
            tag_seq = list(tag_seq.cpu().data)

        return score, tag_seq

    def forward_alg(self, feats):
        """
        This function performs the forward algorithm explained above
        """
        # calculate in log domain
        # feats is len(sentence) * size_tag
        # initialize alpha with a Tensor with values all equal to -10000.

        # Do the forward algorithm to compute the partition function
        init_alphas = torch.Tensor(1, self.size_tag).fill_(-10000.)

        # START_TAG has all score.
        init_alphas[0][self.tag2idx[self.START_TAG]] = 0.

        # Wrap in a variable so that we will get automatic backprop
        forward_var = init_alphas.clone().to(feats.device)
        forward_var.require_grad = True

        # Iterate through the sentence
        for feat in feats:
            # broadcast the emission score: it is the same regardless of
            # the previous tag
            emit_score = feat.view(-1, 1)

            # the ith entry of trans_score is the score of transitioning to
            # next_tag from i
            tag_var = forward_var + self.transitions + emit_score

            # The ith entry of next_tag_var is the value for the
            # edge (i -> next_tag) before we do log-sum-exp
            max_tag_var, _ = torch.max(tag_var, dim=1)

            # The forward variable for this tag is log-sum-exp of all the
            # scores.
            tag_var = tag_var - max_tag_var.view(-1, 1)

            # Compute log sum exp in a numerically stable way for the forward algorithm
            forward_var = max_tag_var + torch.log(torch.sum(torch.exp(tag_var), dim=1)).view(1, -1)  # ).view(1, -1)
        terminal_var = (forward_var + self.transitions[self.tag2idx[self.STOP_TAG]]).view(1, -1)
        alpha = log_sum_exp(terminal_var)
        # Z(x)
        return alpha

    def score_sentences(self, feats, tags):
        # tags is ground_truth, a list of ints, length is len(sentence)
        # feats is a 2D tensor, len(sentence) * tag_size
        r = torch.LongTensor(range(feats.size()[0])).to(feats.device)
        pad_start_tags = torch.cat([torch.LongTensor([self.tag2idx[self.START_TAG]]), tags])
        pad_stop_tags = torch.cat([tags, torch.LongTensor([self.tag2idx[self.STOP_TAG]])])

        score = torch.sum(self.transitions[pad_stop_tags, pad_start_tags]) + torch.sum(feats[r, tags])

        return score

    def viterbi_decode(self, feats):
        """
        In this function, we implement the viterbi algorithm explained above.
        A Dynamic programming based approach to find the best tag sequence
        """
        backpointers = []
        # analogous to forward

        # Initialize the viterbi variables in log space
        init_vvars = torch.Tensor(1, self.tagset_size).fill_(-10000.)
        init_vvars[0][self.tag_to_ix[self.START_TAG]] = 0

        # forward_var at step i holds the viterbi variables for step i-1
        forward_var = init_vvars.re
        if self.use_gpu:
            forward_var = forward_var.cuda()
        for feat in feats:
            next_tag_var = forward_var.view(1, -1).expand(self.tagset_size, self.tagset_size) + self.transitions
            _, bptrs_t = torch.max(next_tag_var, dim=1)
            bptrs_t = bptrs_t.squeeze().data.cpu().numpy()  # holds the backpointers for this step
            next_tag_var = next_tag_var.data.cpu().numpy()
            viterbivars_t = next_tag_var[range(len(bptrs_t)), bptrs_t]  # holds the viterbi variables for this step
            viterbivars_t = torch.FloatTensor(viterbivars_t, require_grad=True, device=feats.device)

            # Now add in the emission scores, and assign forward_var to the set
            # of viterbi variables we just computed
            forward_var = viterbivars_t + feat
            backpointers.append(bptrs_t)

        # Transition to STOP_TAG
        terminal_var = forward_var + self.transitions[self.tag2idx[self.STOP_TAG]]
        terminal_var.data[self.tag2idx[self.STOP_TAG]] = -10000.
        terminal_var.data[self.tag2idx[self.START_TAG]] = -10000.
        best_tag_id = argmax(terminal_var.unsqueeze(0))
        path_score = terminal_var[best_tag_id]

        # Follow the back pointers to decode the best path.
        best_path = [best_tag_id]
        for bptrs_t in reversed(backpointers):
            best_tag_id = bptrs_t[best_tag_id]
            best_path.append(best_tag_id)

        # Pop off the start tag (we dont want to return that to the caller)
        start = best_path.pop()
        assert start == self.tag2idx[self.START_TAG]  # Sanity check
        best_path.reverse()
        return path_score, best_path


