import torch
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from modules.attention import AttentionPooling
from modules.recurrent import RNN, AttentionEncoder, AttentionEncoderCell, StackedCell
from utils.dataset import Documents, CharDocuments
from typing import Optional, Tuple


def pack_residual(x_pack, y_pack):
    x_tensor, x_lengths = pad_packed_sequence(x_pack)
    y_tensor, y_lengths = pad_packed_sequence(y_pack)

    if x_lengths != y_lengths:
        raise ValueError("different lengths")

    return pack_padded_sequence(x_tensor + y_tensor, x_lengths)


class PairEncoder(nn.Module):
    def __init__(self, question_embed_size, passage_embed_size, hidden_size,
                 bidirectional, mode, num_layers, dropout,
                 residual, gated, rnn_cell,
                 cell_factory=AttentionEncoderCell):
        super().__init__()

        attn_args = [question_embed_size, passage_embed_size, hidden_size]
        attn_kwargs = {"attn_size": 75, "batch_first": False}

        self.pair_encoder = AttentionEncoder(
            cell_factory,
            question_embed_size,
            passage_embed_size,
            hidden_size,
            AttentionPooling,
            attn_args,
            attn_kwargs,
            bidirectional=bidirectional,
            mode=mode,
            num_layers=num_layers,
            dropout=dropout,
            residual=residual,
            gated=gated,
            rnn_cell=rnn_cell,
            attn_mode="pair_encoding"
        )

    def forward(self, questions, question_mark, passage):
        inputs = (passage, questions, question_mark)
        result = self.pair_encoder(inputs)
        return result


class SelfMatchingEncoder(nn.Module):
    def __init__(self, passage_embed_size, hidden_size,
                 bidirectional, mode, num_layers,
                 dropout, residual, gated,
                 rnn_cell,
                 cell_factory=AttentionEncoderCell):
        super().__init__()
        attn_args = [passage_embed_size, passage_embed_size]
        attn_kwargs = {"attn_size": 75, "batch_first": False}

        self.pair_encoder = AttentionEncoder(
            cell_factory, passage_embed_size, passage_embed_size,
            hidden_size,
            AttentionPooling,
            attn_args,
            attn_kwargs,
            bidirectional=bidirectional,
            mode=mode,
            num_layers=num_layers,
            dropout=dropout,
            residual=residual,
            gated=gated,
            rnn_cell=rnn_cell,
            attn_mode="self_matching"
        )

    def forward(self, questions, question_mark, passage):
        inputs = (passage, questions, question_mark)
        return self.pair_encoder(inputs)


class Max(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, input):
        max_result, _ = input.max(dim=self.dim)
        return max_result


class Flatten(nn.Module):
    def forward(self, input):
        return input.view(input.size(0), -1)


class CharLevelWordEmbeddingCnn(nn.Module):
    def __init__(self,
                 char_embedding_size,
                 char_num,
                 num_filters: int,
                 ngram_filter_sizes=(5,),
                 output_dim=None,
                 activation=nn.ReLU,
                 embedding_weights=None,
                 padding_idx=1,
                 requires_grad=True
                 ):
        super().__init__()


        if embedding_weights is not None:
            char_num, char_embedding_size = embedding_weights.size()

        self.embed_char = nn.Embedding(char_num, char_embedding_size, padding_idx=padding_idx)
        if embedding_weights is not None:
            self.embed_char.weight = nn.Parameter(embedding_weights)
            self.embed_char.weight.requires_grad = requires_grad

        self._embedding_size = char_embedding_size

        self.cnn_layers = nn.ModuleList()
        for i, filter_size in enumerate(ngram_filter_sizes):
            self.cnn_layers.append(
                nn.Sequential(
                    nn.Conv1d(in_channels=char_embedding_size,
                              out_channels=num_filters,
                              kernel_size=filter_size),
                    activation(),
                    Max(2)
                )
            )

        conv_out_size = len(ngram_filter_sizes) * num_filters
        self.output_layer = nn.Linear(conv_out_size, output_dim) if output_dim else None
        self.output_dim = output_dim if output_dim else conv_out_size

    def forward(self, tensor, mask=None):
        """

        :param tensor:    batch x word_num x char_num
        :return:
        """
        if mask is not None:
            tensor = tensor * mask.float()

        batch_num, word_num, char_num = tensor.size()
        tensor = self.embed_char(tensor.view(-1, char_num)).transpose(1, 2)
        #import ipdb; ipdb.set_trace()
        conv_out = [layer(tensor) for layer in self.cnn_layers]
        output = torch.cat(conv_out, dim=1) if len(conv_out) > 1 else conv_out[0]

        if self.output_layer:
            output = self.output_layer(output)

        return output.view(batch_num, word_num, -1)


class WordEmbedding(nn.Module):
    """
    Embed word with word-level embedding and char-level embedding
    """

    def __init__(self, word_embedding, padding_idx=1, requires_grad=False):
        super().__init__()

        word_vocab_size, word_embedding_dim = word_embedding.size()
        self.word_embedding_word_level = nn.Embedding(word_vocab_size, word_embedding_dim,
                                                      padding_idx=padding_idx)
        self.word_embedding_word_level.weight = nn.Parameter(word_embedding,
                                                             requires_grad=requires_grad)
        # self.embedding_size = word_embedding_dim + char_embedding_config["output_dim"]
        self.output_dim = word_embedding_dim

    def forward(self, *tensors):
        """
        :param words: all distinct words (char level) in seqs. Tuple of word tensors (batch first, with lengths) and word idx
        :param tensors: lists of documents like: (contexts_tensor, contexts_tensor_new), context_lengths)
        :return:   embedded batch (batch first)
        """

        result = []
        for tensor in tensors:
            word_level_embed = self.word_embedding_word_level(tensor)
            result.append(word_level_embed)
        return result


class SentenceEncoding(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers, bidirectional, dropout):
        super().__init__()

        self.question_encoder = RNN(input_size,
                                    hidden_size=hidden_size,
                                    num_layers=num_layers,
                                    bidirectional=bidirectional,
                                    dropout=dropout)

        self.passage_encoder = RNN(input_size,
                                   hidden_size=hidden_size,
                                   num_layers=num_layers,
                                   bidirectional=bidirectional,
                                   dropout=dropout)

    def forward(self, question_pack, context_pack):
        question_outputs, _ = self.question_encoder(question_pack)
        passage_outputs, _ = self.passage_encoder(context_pack)
        return question_outputs, passage_outputs


class PointerNetwork(nn.Module):
    def __init__(self, question_size, passage_size, attn_size=None,
                 cell_type=nn.GRUCell, num_layers=1, dropout=0, residual=False, **kwargs):
        super().__init__()
        self.num_layers = num_layers
        if attn_size is None:
            attn_size = question_size

        # TODO: what is V_q? (section 3.4)
        v_q_size = question_size
        self.question_pooling = AttentionPooling(question_size,
                                                 v_q_size, attn_size=attn_size)
        self.passage_pooling = AttentionPooling(passage_size,
                                                question_size, attn_size=attn_size)

        self.V_q = nn.Parameter(torch.randn(1, 1, v_q_size), requires_grad=True)
        self.cell = StackedCell(question_size, question_size, num_layers=num_layers,
                                dropout=dropout, rnn_cell=cell_type, residual=residual, **kwargs)

    def forward(self, question_pad, question_mask, passage_pad, passage_mask):
        hidden = self.question_pooling(question_pad, self.V_q,
                                       key_mask=question_mask, broadcast_key=True)  # 1 x batch x n

        inputs, ans_begin = self.passage_pooling(passage_pad, hidden,
                                                 key_mask=passage_mask, return_key_scores=True)

        hidden = hidden.expand([self.num_layers, hidden.size(1), hidden.size(2)])

        output, hidden = self.cell(inputs.squeeze(0), hidden)
        _, ans_end = self.passage_pooling(passage_pad, output.unsqueeze(0),
                                          key_mask=passage_mask, return_key_scores=True)

        return ans_begin, ans_end


class Model(nn.Module):
    def __init__(self,
                 args,
                 char_embedding_config,
                 word_embedding_config,
                 sentence_encoding_config,
                 pair_encoding_config,
                 self_matching_config,
                 pointer_config):
        super().__init__()
        self.word_embedding = WordEmbedding(
            word_embedding=word_embedding_config["embedding_weights"],
            padding_idx=word_embedding_config["padding_idx"],
            requires_grad=word_embedding_config["update"])

        self.char_embedding = CharLevelWordEmbeddingCnn(
            char_embedding_size=char_embedding_config["char_embedding_size"],
            char_num = char_embedding_config["char_num"],
            num_filters=char_embedding_config["num_filters"],
            ngram_filter_sizes=char_embedding_config["ngram_filter_sizes"],
            output_dim=char_embedding_config["output_dim"],
            activation=char_embedding_config["activation"],
            embedding_weights=char_embedding_config["embedding_weights"],
            padding_idx=char_embedding_config["padding_idx"],
            requires_grad=char_embedding_config["update"]
        )

        # we are going to concat the output of two embedding methods
        embedding_dim = self.word_embedding.output_dim + self.char_embedding.output_dim

        self.r_net = RNet(args,
                          embedding_dim,
                          sentence_encoding_config,
                          pair_encoding_config,
                          self_matching_config,
                          pointer_config)

        self.args = args

    def forward(self,
                question: Documents,
                question_char: CharDocuments,
                passage: Documents,
                passage_char: CharDocuments):

        # Embedding using Glove
        embedded_question, embedded_passage = self.word_embedding(question.tensor, passage.tensor)

        if torch.cuda.is_available():
            question_char = question_char.cuda(self.args.device_id)
            passage_char = passage_char.cuda(self.args.device_id)
            question = question.cuda(self.args.device_id)
            passage = passage.cuda(self.args.device_id)
            embedded_question = embedded_question.cuda(self.args.device_id)
            embedded_passage = embedded_passage.cuda(self.args.device_id)

        # char level embedding
        embedded_question_char = self.char_embedding(question_char.tensor)
        embedded_passage_char = self.char_embedding(passage_char.tensor)

        # concat word embedding and char level embedding
        embedded_passage_merged = torch.cat([embedded_passage, embedded_passage_char], dim=2)
        embedded_question_merged = torch.cat([embedded_question, embedded_question_char], dim=2)

        return self.r_net(question, passage, embedded_question_merged, embedded_passage_merged)

    def cuda(self, *args, **kwargs):
        self.r_net.cuda(*args, **kwargs)
        self.char_embedding.cuda(*args, **kwargs)
        return self


class RNet(nn.Module):
    def __init__(self, args, embedding_size,
                 sentence_encoding_config,
                 pair_encoding_config, self_matching_config, pointer_config):
        super().__init__()
        self.current_score = 0
        self.sentence_encoding = SentenceEncoding(embedding_size,
                                                  sentence_encoding_config["hidden_size"],
                                                  sentence_encoding_config["num_layers"],
                                                  sentence_encoding_config["bidirectional"],
                                                  sentence_encoding_config["dropout"])

        sentence_encoding_direction = (2 if sentence_encoding_config["bidirectional"] else 1)
        sentence_encoding_size = (sentence_encoding_config["hidden_size"] * sentence_encoding_direction)
        self.pair_encoder = PairEncoder(sentence_encoding_size,
                                        sentence_encoding_size,
                                        hidden_size=pair_encoding_config["hidden_size"],
                                        bidirectional=pair_encoding_config["bidirectional"],
                                        mode=pair_encoding_config["mode"],
                                        num_layers=pair_encoding_config["num_layers"],
                                        dropout=pair_encoding_config["dropout"],
                                        residual=pair_encoding_config["residual"],
                                        gated=pair_encoding_config["gated"],
                                        rnn_cell=pair_encoding_config["rnn_cell"],
                                        cell_factory=AttentionEncoderCell)

        pair_encoding_num_direction = (2 if pair_encoding_config["bidirectional"] else 1)
        self.self_matching_encoder = SelfMatchingEncoder(passage_embed_size=self_matching_config["hidden_size"] * pair_encoding_num_direction,
                                                         hidden_size=self_matching_config["hidden_size"],
                                                         bidirectional=self_matching_config["bidirectional"],
                                                         mode=self_matching_config["mode"],
                                                         num_layers=self_matching_config["num_layers"],
                                                         dropout=self_matching_config["dropout"],
                                                         residual=self_matching_config["residual"],
                                                         gated=self_matching_config["gated"],
                                                         rnn_cell=self_matching_config["rnn_cell"],
                                                         )

        question_size = sentence_encoding_direction * sentence_encoding_config["hidden_size"]
        passage_size = pair_encoding_num_direction * pair_encoding_config["hidden_size"]
        self.pointer_output = PointerNetwork(question_size,
                                             passage_size,
                                             num_layers=pointer_config["num_layers"],
                                             dropout=pointer_config["dropout"],
                                             cell_type=pointer_config["rnn_cell"])
        self.residual = args.residual
        for name, weight in self.parameters():
            if weight.ndimension() >= 2:
                nn.init.orthogonal(weight)

    def forward(self,
                question: Documents,
                passage: Documents,
                embedded_question,
                embedded_passage):
        # embed words using char-level and word-level and concat them
        passage_pack, question_pack = self._sentence_encoding(embedded_passage,
                                                              embedded_question,
                                                              passage, question)
        question_encoded_padded_sorted, _ = pad_packed_sequence(question_pack)  # (seq, batch, encode_size), lengths
        question_encoded_padded_original = question.restore_original_order(question_encoded_padded_sorted,
                                                                           batch_dim=1)
        # question and context has same ordering
        question_pad_in_passage_sorted_order = passage.to_sorted_order(question_encoded_padded_original,
                                                                       batch_dim=1)
        question_mask_in_passage_sorted_order = passage.to_sorted_order(question.mask_original, batch_dim=0).transpose(
            0, 1)

        paired_passage_pack = self._pair_encode(passage_pack, question_pad_in_passage_sorted_order,
                                                question_mask_in_passage_sorted_order)

        if self.residual:
            paired_passage_pack = pack_residual(paired_passage_pack, passage_pack)

        self_matched_passage_pack = self._self_match_encode(paired_passage_pack, passage)

        if self.residual:
            self_matched_passage_pack = pack_residual(paired_passage_pack, self_matched_passage_pack)

        begin, end = self.pointer_output(question_pad_in_passage_sorted_order,
                                         question_mask_in_passage_sorted_order,
                                         pad_packed_sequence(self_matched_passage_pack)[0],
                                         passage.to_sorted_order(passage.mask_original, batch_dim=0).transpose(0, 1))

        return (passage.restore_original_order(begin.transpose(0, 1), 0),
                passage.restore_original_order(end.transpose(0, 1), 0))

    def _sentence_encoding(self,
                           embedded_passage,
                           embedded_question,
                           passage,
                           question):
        question_pack = pack_padded_sequence(embedded_question, question.lengths, batch_first=True)
        passage_pack = pack_padded_sequence(embedded_passage, passage.lengths, batch_first=True)
        question_encoded_pack, passage_encoded_pack = self.sentence_encoding(question_pack, passage_pack)
        return passage_encoded_pack, question_encoded_pack

    def _self_match_encode(self,
                           paired_passage_pack,
                           passage):
        passage_mask_sorted_order = passage.to_sorted_order(passage.mask_original, batch_dim=0).transpose(0, 1)
        self_matched_passage, _ = self.self_matching_encoder(pad_packed_sequence(paired_passage_pack)[0],
                                                             passage_mask_sorted_order, paired_passage_pack)
        return self_matched_passage

    def _pair_encode(self,
                     passage_encoded_pack,
                     question_encoded_padded_in_passage_sorted_order,
                     question_mask_in_passage_order):
        paired_passage_pack, _ = self.pair_encoder(question_encoded_padded_in_passage_sorted_order,
                                                   question_mask_in_passage_order,
                                                   passage_encoded_pack)
        return paired_passage_pack
