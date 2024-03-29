import os
import pickle

import numpy as np
import torch

from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset

from .embedding import UNK_TOKEN, PAD_TOKEN


OK_TOKEN = 'OK'
BAD_TOKEN = 'BAD'


class QEDataset(Dataset):

  def __init__(self, name, src_tokenizer, mt_tokenizer, use_tags=True,
               use_baseline=False, use_bert=False, data_dir=None):
    if data_dir is None:
      data_dir = name

    self._src = self._read_text(
      os.path.join(data_dir, f'{name}.src'),
      src_tokenizer
    )
    self._mt = self._read_text(
      os.path.join(data_dir, f'{name}.mt'),
      mt_tokenizer
    )

    self._use_tags = use_tags
    if self._use_tags:
      self._src_tags = self._read_tags(
        os.path.join(data_dir, f'{name}.source_tags'),
        False
      )
      self._word_tags, self._gap_tags = self._read_tags(
        os.path.join(data_dir, f'{name}.tags'),
        True
      )

    self._use_baseline = use_baseline
    if self._use_baseline:
      baseline_file = os.path.join(data_dir, f'{name}.baseline')
      print('Reading', baseline_file)
      with open(baseline_file, 'rb') as f:
        baseline_dict = pickle.load(f)
        self._baseline_features = baseline_dict['features']
        self._baseline_vocab_sizes = baseline_dict['vocab_sizes']

    self._use_bert = use_bert
    if self._use_bert:
      bert_file = os.path.join(data_dir, f'{name}.bert')
      print('Reading', bert_file)
      with open(bert_file, 'rb') as f:
        self._bert_features = pickle.load(f)

    self._aligns = self._read_alignments(
      os.path.join(data_dir, f'{name}.src-mt.alignments')
    )

    self._validate()

  def __len__(self):
    return len(self._src)

  def __getitem__(self, idx):
    item = {
      'src': self._src[idx],
      'mt': self._mt[idx],
      'aligns': self._aligns[idx],
    }

    if self._use_tags:
      item.update({
        'src_tags': self._src_tags[idx],
        'word_tags': self._word_tags[idx],
        'gap_tags': self._gap_tags[idx],
      })

    if self._use_baseline:
      item.update({
        'baseline_features': self._baseline_features[idx],
      })

    if self._use_bert:
      item.update({
        'bert_features': self._bert_features[idx],
      })

    return item

  def _validate(self):
    num_samples = len(self._src)

    assert len(self._mt) == num_samples
    if self._use_tags:
      assert len(self._src_tags) == num_samples
      assert len(self._word_tags) == num_samples
      assert len(self._gap_tags) == num_samples

    for i in range(num_samples):
      src_len = len(self._src[i])
      mt_len = len(self._mt[i])

      if self._use_tags:
        assert len(self._src_tags[i]) == src_len
        assert len(self._word_tags[i]) == mt_len
        assert len(self._gap_tags[i]) == mt_len + 1

      if self._use_baseline:
        assert len(self._baseline_features[i]) == mt_len

      if self._use_bert:
        assert len(self._bert_features[i]) == mt_len

  def _read_text(self, path, tokenizer):
    print('Reading', path)

    samples = []
    with open(path, 'r') as file:
      for line in file:
        sample = []
        for i, word in enumerate(line.split()):
          new_tokens = tokenizer.convert_tokens_to_ids([word])
          sample.extend(new_tokens)
        samples.append(sample)

    return samples

  def _read_tags(self, path, has_gaps):
    print('Reading', path)

    word_tags = []
    if has_gaps:
      gap_tags = []

    with open(path, 'r') as file:
      for i, line in enumerate(file):
        line_tags = []
        for tag in line.split():
          if tag == OK_TOKEN:
            line_tags.append(1)
          elif tag == BAD_TOKEN:
            line_tags.append(0)
          else:
            raise ValueError('Unknown tag')

        if has_gaps:
          word_tags.append(line_tags[1::2])
          gap_tags.append(line_tags[::2])
        else:
          word_tags.append(line_tags)

    if has_gaps:
      return word_tags, gap_tags

    return word_tags

  def _read_alignments(self, path):
    print('Reading', path)

    aligns = []
    with open(path, 'r') as file:
      for line in file:
        line_aligns = []
        for pair in line.split():
          src, mt = pair.split('-')
          line_aligns.append([int(src), int(mt)])
        aligns.append(line_aligns)

    return aligns


def qe_collate(data, device=torch.device('cpu')):
  merged = {}
  for key in data[0].keys():
    sequence = [torch.tensor(sample[key]) for sample in data]
    merged[key] = pad_sequence(sequence, padding_value=PAD_TOKEN).to(device)

  return merged