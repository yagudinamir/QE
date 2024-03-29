import numpy as np
import onmt
import torch
import torch.nn as nn
import torch.nn.functional as F

from pytorch_pretrained_bert import BertModel

from .embedding import PAD_TOKEN


device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')


# Compute log sum exp in a numerically stable way for the forward algorithm
def log_sum_exp(matrix, dim=None):
  max_score = matrix.max()
  if dim is not None:
    return max_score + \
           torch.log(torch.sum(torch.exp(matrix - max_score), dim=dim))
  else:
    return max_score + \
           torch.log(torch.sum(torch.exp(matrix - max_score)))


class CRF(nn.Module):

  def __init__(self, input_size, num_tags):
    super(CRF, self).__init__()

    self._num_tags = num_tags

    self._input2tags = nn.Linear(input_size, num_tags)

    self._transitions_from_start = nn.Parameter(
      torch.randn(num_tags)
    )
    self._transitions = nn.Parameter(
      torch.randn(num_tags, num_tags)
    )
    self._transitions_to_end = nn.Parameter(
      torch.randn(num_tags)
    )

  def _log_score(self, seq, tags):
    emit_scores = self._input2tags(seq)

    score = self._transitions_from_start[tags[0]] \
            + emit_scores[0, tags[0]]

    for i, item in enumerate(seq[1:], 1):
      score += self._transitions[tags[i], tags[i - 1]]
      score += emit_scores[i, tags[i]]

    score += self._transitions_to_end[tags[-1]]

    return score

  def _partition(self, seq):
    emit_scores = self._input2tags(seq)

    # total log score of all paths ending at given label
    tag_scores_total = self._transitions_from_start \
                       + emit_scores[0]

    for i, item in enumerate(seq[1:], 1):
      # log score of transitions at current step
      transition_scores = self._transitions + tag_scores_total
      tag_scores_total = log_sum_exp(transition_scores, dim=1)
      tag_scores_total += emit_scores[i]

    end_transition_scores = self._transitions_to_end + tag_scores_total

    return log_sum_exp(end_transition_scores)

  def label(self, seq):
    if len(seq.shape) == 3:
      seq = seq[0]

    emit_scores = self._input2tags(seq)

    # maximum log score of a path ending at given label
    tag_scores_total = self._transitions_from_start \
                       + emit_scores[0]

    # previous tag on the best path
    parent = torch.full(
      (len(seq), self._num_tags),
      -1,
      dtype=torch.long
    )

    for i, item in enumerate(seq[1:], 1):
      # log score of transitions at current step
      transition_scores = self._transitions + tag_scores_total
      tag_scores_total, parent[i] = torch.max(transition_scores, dim=1)
      tag_scores_total += emit_scores[i]

    end_transition_scores = self._transitions_to_end + tag_scores_total
    # dim=0 is passed only to retreive the argmax
    path_score, cur_tag = torch.max(end_transition_scores, dim=0)

    path = []
    cur_idx = len(seq) - 1
    while cur_tag != -1:
      # sanity check
      assert cur_idx >= 0

      path.append(cur_tag)
      cur_tag = parent[cur_idx, cur_tag]
      cur_idx -= 1

    # sanity check
    assert len(path) == len(seq)

    path.reverse()
    return torch.tensor(path), path_score

  def log_likelihood(self, seq, tags):
    if len(seq.shape) == 3:
      seq = seq[0]
    if len(tags.shape) == 3:
      tags = tags[0]
    return self._log_score(seq, tags) - self._partition(seq)


class BaselineFeatureConverter(nn.Module):
  '''
  Converts categorial baseline features to one-hot vectors.

  Receives a K baseline features and converts them to float vector of size L.
  Operates on minibatches of size N.
  '''

  def __init__(self, vocab_sizes):
    super(BaselineFeatureConverter, self).__init__()

    self._num_features = len(vocab_sizes)
    self._embeds = [None] * self._num_features
    self._vocab_sizes = vocab_sizes[:]
    self._features_size = 0
    for i, size in enumerate(vocab_sizes):
      if size == -1:
        self._features_size += 1
        continue

      self._features_size += size
      one_hot_embeds = torch.eye(size)
      unk_embed = torch.zeros(size)
      self._embeds[i] = nn.Embedding.from_pretrained(
        torch.cat([one_hot_embeds, unk_embed.unsqueeze(0)])
      ).to(device)

  def forward(self, features):
    '''
    Converts baseline features to one-hot.

    :param features: Batch of features of shape (N, K)
    :return: Batch of converted features of shape (N, L)
    '''
    N, K = features.shape
    features = features.view(-1, K)
    converted = []
    for i in range(self._num_features):
      column = features[:, i]
      if self._embeds[i] is not None:
        column[column < 0] = self._vocab_sizes[i]
        column = self._embeds[i](column.to(torch.long))
      else:
        column = column.unsqueeze(1)
      converted.append(column)
    converted = torch.cat(converted, dim=1)
    return converted.reshape(N, -1)


class QualityEstimator(nn.Module):

  def __init__(self, hidden_size, src_embeddings, mt_embeddings, dropout_p=0.1,
               baseline_vocab_sizes=None, bert_features_size=0,
               predict_gaps=False, transformer_encoder=False):
    super(QualityEstimator, self).__init__()

    self._dropout = nn.Dropout(dropout_p)
    self._features_size = 0

    self._use_rnn = hidden_size > 0
    self._transformer_encoder = transformer_encoder
    if self._use_rnn:
      self._emb_dim = src_embeddings.shape[1]
      assert mt_embeddings.shape[1] == self._emb_dim

      self._hidden_size = hidden_size
      if self._transformer_encoder:
        self._features_size += 2 * self._emb_dim # transformer output + attention
      else:
        self._features_size += 3 * hidden_size # rnn output + attention + self-attention

      src_emb = onmt.modules.Embeddings(
          self._emb_dim,
          len(src_embeddings),
          PAD_TOKEN,
      )
      src_emb.word_lut.weight.data.copy_(src_embeddings)
      src_emb.word_lut.weight.requires_grad = False

      if self._transformer_encoder:
        self._src_enc = onmt.encoders.TransformerEncoder(
            num_layers=2,
            d_model=self._emb_dim,
            heads=4,
            d_ff=hidden_size,
            dropout=dropout_p,
            embeddings=src_emb,
            max_relative_positions=200,
        )
      else:
        self._src_enc = onmt.encoders.RNNEncoder(
            hidden_size=hidden_size,
            num_layers=1,
            rnn_type='LSTM',
            bidirectional=True,
            embeddings=src_emb,
        )

      mt_emb = onmt.modules.Embeddings(
          self._emb_dim,
          len(mt_embeddings),
          PAD_TOKEN,
      )
      mt_emb.word_lut.weight.data.copy_(mt_embeddings)
      mt_emb.word_lut.weight.requires_grad = False

      if self._transformer_encoder:
        self._mt_enc = onmt.encoders.TransformerEncoder(
            num_layers=2,
            d_model=self._emb_dim,
            heads=4,
            d_ff=hidden_size,
            dropout=dropout_p,
            embeddings=mt_emb,
            max_relative_positions=200,
        )
      else:
        self._mt_enc = onmt.encoders.RNNEncoder(
            hidden_size=hidden_size,
            num_layers=1,
            rnn_type='LSTM',
            bidirectional=True,
            embeddings=mt_emb,
        )

      self._attn_dim = self._emb_dim if self._transformer_encoder else hidden_size
      self._attn = onmt.modules.MultiHeadedAttention(
          head_count=1,
          model_dim=self._attn_dim,
      )
      self._attn.linear_keys.weight.data.copy_(torch.eye(self._attn_dim))
      self._attn.linear_keys.weight.requires_grad = False
      self._attn.linear_values.weight.data.copy_(torch.eye(self._attn_dim))
      self._attn.linear_values.weight.requires_grad = False
      self._attn.linear_query.weight.data.copy_(torch.eye(self._attn_dim))
      self._attn.linear_query.weight.requires_grad = False
      self._attn.final_linear.weight.requires_grad = False
      self._attn.final_linear.weight.data.copy_(torch.eye(self._attn_dim))

    self._use_baseline = baseline_vocab_sizes is not None
    if self._use_baseline:
      self._baseline_converter = BaselineFeatureConverter(baseline_vocab_sizes)
      self._features_size += self._baseline_converter._features_size

    self._use_bert = bert_features_size > 0
    if self._use_bert:
      self._features_size += bert_features_size

    assert self._features_size > 0

    self._crf = CRF(self._features_size, 2)


  def _extract_rnn_features(self, src, mt):
    max_src_len, batch_len = src.shape
    max_mt_len = mt.shape[0]

    nsrc = src.clone().unsqueeze(2)
    nsrc[nsrc < 0] = 0
    src_feats = self._src_enc(nsrc)[1].transpose(0, 1)
    src_feats = self._dropout(src_feats)

    nmt = mt.clone().unsqueeze(2)
    nmt[nmt < 0] = 0
    mt_feats = self._mt_enc(nmt)[1].transpose(0, 1)
    mt_feats = self._dropout(mt_feats)

    attn_mask = torch.empty(batch_len, max_mt_len, max_src_len,
                            dtype=torch.uint8, device=device)
    attn_mask[:] = (src != PAD_TOKEN).t().unsqueeze(1)
    context, _ = self._attn(src_feats, src_feats, mt_feats, attn_mask)

    rnn_features = [mt_feats, context]

    if not self._transformer_encoder:
      self_attn_mask = torch.empty(batch_len, max_mt_len, max_mt_len,
                              dtype=torch.uint8, device=device)
      self_attn_mask[:] = (mt != PAD_TOKEN).t().unsqueeze(1)
      self_context, _ = self._attn(mt_feats, mt_feats, mt_feats, self_attn_mask)
      rnn_features.append(self_context)

    return torch.cat(rnn_features, dim=2)

  def _convert_baseline_features(self, baseline_features):
    N, M, K = baseline_features.shape
    baseline_features = baseline_features.reshape(-1, K)
    converted = self._baseline_converter(baseline_features).to(device)
    return converted.reshape(N, M, -1)

  def forward(self, src, mt, baseline_features=None, bert_features=None):
    features = []

    if self._use_rnn:
      features.append(self._extract_rnn_features(src, mt))
    if self._use_baseline:
      assert baseline_features is not None
      base_feats = self._convert_baseline_features(baseline_features)
      features.append(base_feats.transpose(0, 1))
    if self._use_bert:
      features.append(bert_features.transpose(0, 1))

    features = torch.cat(features, dim=2)

    return features.transpose(0, 1)


  def loss(self, src, mt, aligns, src_tags=None,
           word_tags=None, gap_tags=None, **kwargs):
    features = self(src, mt, **kwargs)

    loss = 0

    batch_len = mt.shape[1]
    for i in range(batch_len):
      mt_len = (mt[:,i] != PAD_TOKEN).sum()
      loss -= self._crf.log_likelihood(
          features[:mt_len,i],
          word_tags[:mt_len,i]
      )
    return loss / batch_len

  def predict(self, src, mt, aligns, **kwargs):
    with torch.no_grad():
      src_tags = torch.ones_like(src)
      word_tags = torch.ones_like(mt)
      features = self(src, mt, **kwargs)


      batch_len = src.shape[1]
      for i in range(batch_len):
        mt_len = (mt[:,i] != PAD_TOKEN).sum()
        word_tags[:mt_len, i], _ = self._crf.label(features[:mt_len,i])
        for j_src, j_mt in aligns[:, i]:
          if j_mt == -1:
            break
          if word_tags[j_mt, i] == 0:
            src_tags[j_src, i] = 0

      word_tags[mt == PAD_TOKEN] = -1

      return src_tags, word_tags