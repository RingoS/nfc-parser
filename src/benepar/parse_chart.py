import os

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

from transformers import AutoConfig, AutoModel

from . import char_lstm
from . import decode_chart
from . import nkutil
from .partitioned_transformer import (
    ConcatPositionalEncoding,
    FeatureDropout,
    PartitionedTransformerEncoder,
    PartitionedTransformerEncoderLayer,
)
from . import parse_base
from . import retokenization
from . import subbatching

from analysis.trees import InternalTreebankNode, LeafTreebankNode, load_trees_from_text

import time, random

import sklearn
import json
import copy
import nltk

class ChartParser(nn.Module, parse_base.BaseParser):
    def __init__(
        self,
        tag_vocab,
        label_vocab,
        char_vocab,
        hparams,
        pattern_vocab=None,
        pattern_children=None,
        pretrained_model_path=None,
    ):
        super().__init__()
        self.config = locals()
        self.config.pop("self")
        self.config.pop("__class__")
        self.config.pop("pretrained_model_path")
        self.config["hparams"] = hparams.to_dict()
        self.hparams = hparams

        self.tag_vocab = tag_vocab
        self.label_vocab = label_vocab
        self.char_vocab = char_vocab
        self.pattern_vocab = pattern_vocab
        self.label_from_index = {i: label for label, i in label_vocab.items()}

        self.d_model = hparams.d_model

        self.char_encoder = None
        self.pretrained_model = None
        if hparams.use_chars_lstm:
            assert (
                not hparams.use_pretrained
            ), "use_chars_lstm and use_pretrained are mutually exclusive"
            self.retokenizer = char_lstm.RetokenizerForCharLSTM(self.char_vocab)
            self.char_encoder = char_lstm.CharacterLSTM(
                max(self.char_vocab.values()) + 1,
                hparams.d_char_emb,
                hparams.d_model // 2,  # Half-size to leave room for
                # partitioned positional encoding
                char_dropout=hparams.char_lstm_input_dropout,
            )
        elif hparams.use_pretrained:
            if pretrained_model_path is None:
                self.retokenizer = retokenization.Retokenizer(
                    hparams.pretrained_model, retain_start_stop=True
                )
                self.pretrained_model = AutoModel.from_pretrained(
                    hparams.pretrained_model
                )
            else:
                self.retokenizer = retokenization.Retokenizer(
                    pretrained_model_path, retain_start_stop=True
                )
                self.pretrained_model = AutoModel.from_config(
                    AutoConfig.from_pretrained(pretrained_model_path)
                )
            d_pretrained = self.pretrained_model.config.hidden_size

            if hparams.use_encoder:
                self.project_pretrained = nn.Linear(
                    d_pretrained, hparams.d_model // 2, bias=False
                )
            else:
                self.project_pretrained = nn.Linear(
                    d_pretrained, hparams.d_model, bias=False
                )

        if hparams.use_encoder:
            self.morpho_emb_dropout = FeatureDropout(hparams.morpho_emb_dropout)
            self.add_timing = ConcatPositionalEncoding(
                d_model=hparams.d_model,
                max_len=hparams.encoder_max_len,
            )
            encoder_layer = PartitionedTransformerEncoderLayer(
                hparams.d_model,
                n_head=hparams.num_heads,
                d_qkv=hparams.d_kv,
                d_ff=hparams.d_ff,
                ff_dropout=hparams.relu_dropout,
                residual_dropout=hparams.residual_dropout,
                attention_dropout=hparams.attention_dropout,
            )
            self.encoder = PartitionedTransformerEncoder(
                encoder_layer, hparams.num_layers
            )
        else:
            self.morpho_emb_dropout = None
            self.add_timing = None
            self.encoder = None

        self.f_label = nn.Sequential(
            nn.Linear(hparams.d_model, hparams.d_label_hidden),
            nn.LayerNorm(hparams.d_label_hidden),
            nn.ReLU(),
            nn.Linear(hparams.d_label_hidden, max(label_vocab.values())),
        )
        # self.f_label = nn.Linear(hparams.d_model, max(label_vocab.values()))

        if hparams.use_pattern:
            self.f_pattern = nn.Sequential(
                nn.Linear(hparams.d_model, hparams.d_label_hidden),
                nn.LayerNorm(hparams.d_label_hidden),
                nn.ReLU(),
                nn.Linear(hparams.d_label_hidden, len(self.pattern_vocab)),
            )
            # self.f_pattern = nn.Linear(hparams.d_model, len(self.pattern_vocab))
            self.pattern_loss_scale = hparams.pattern_loss_scale
            self.pattern_from_index = {i: label for label, i in pattern_vocab.items()}
        else:
            self.f_pattern = None
            self.pattern_loss_scale = None
            self.pattern_from_index = None

        if hparams.predict_tags:
            self.f_tag = nn.Sequential(
                nn.Linear(hparams.d_model, hparams.d_tag_hidden),
                nn.LayerNorm(hparams.d_tag_hidden),
                nn.ReLU(),
                nn.Linear(hparams.d_tag_hidden, max(tag_vocab.values()) + 1),
            )
            self.tag_loss_scale = hparams.tag_loss_scale
            self.tag_from_index = {i: label for label, i in tag_vocab.items()}
        else:
            self.f_tag = None
            self.tag_from_index = None

        if hparams.use_compatible:
            if not self.pattern_from_index:
                self.pattern_from_index = {i: label for label, i in pattern_vocab.items()}
            self.pattern_children = pattern_children
            self.compatible_loss_scale = hparams.compatible_loss_scale
            self.biaffine_matrix = nn.Parameter(torch.zeros([self.hparams.d_label_hidden, self.hparams.d_label_hidden]), requires_grad=True)
            nn.init.xavier_uniform_(self.biaffine_matrix)

            self.compatible_labels = torch.full([len(self.pattern_vocab), len(self.label_vocab) - 1], -100, dtype=torch.long)
            for key, values in pattern_children.items():
                if key not in pattern_vocab:
                    continue
                for _ in values:
                    if _ not in label_vocab:
                        continue
                    self.compatible_labels[self.pattern_vocab[key], self.label_vocab[_] - 1] = 1
            self.num_positive_compatible = torch.sum(torch.where(self.compatible_labels > 0, self.compatible_labels, torch.tensor(0)))
            print("num_positive_compatible: {}, {}".format(self.num_positive_compatible, int(self.num_positive_compatible)/(len(pattern_vocab)*len(label_vocab))))
        else:
            self.pattern_children = None
            self.compatible_loss_scale = None
        
        if hparams.use_sibling:
            self.sibling_loss_scale = hparams.sibling_loss_scale
            self.f_left_sibling = nn.Sequential(
                nn.Linear(hparams.d_model, hparams.d_label_hidden),
                nn.LayerNorm(hparams.d_label_hidden),
                nn.ReLU(),
                nn.Linear(hparams.d_label_hidden, max(label_vocab.values()) + 1),
            )
            self.f_right_sibling = nn.Sequential(
                nn.Linear(hparams.d_model, hparams.d_label_hidden),
                nn.LayerNorm(hparams.d_label_hidden),
                nn.ReLU(),
                nn.Linear(hparams.d_label_hidden, max(label_vocab.values()) + 1),
            )
            if hparams.use_sibling_compatible:
                self.sibling_compatible_loss_scale = hparams.sibling_compatible_loss_scale
                self.biaffine_matrix_left_middle = nn.Parameter(torch.zeros([self.hparams.d_label_hidden, self.hparams.d_label_hidden]), requires_grad=True)
                self.biaffine_matrix_right_middle = nn.Parameter(torch.zeros([self.hparams.d_label_hidden, self.hparams.d_label_hidden]), requires_grad=True)
                self.biaffine_matrix_left_right = nn.Parameter(torch.zeros([self.hparams.d_label_hidden, self.hparams.d_label_hidden]), requires_grad=True)
                nn.init.xavier_uniform_(self.biaffine_matrix_left_middle)
                nn.init.xavier_uniform_(self.biaffine_matrix_right_middle)
                nn.init.xavier_uniform_(self.biaffine_matrix_left_right)
                
                assert os.path.exists(hparams.sibling_compatible_path)
                with open(hparams.sibling_compatible_path, 'r', encoding='utf-8') as f:
                    sibling_compatible_dict = json.load(f)
                
                self.left_sibling_compatible_dict = sibling_compatible_dict[0]
                self.right_sibling_compatible_dict = sibling_compatible_dict[1]

                self.left_sibling_compatible_labels = torch.full([len(self.label_vocab) - 1, len(self.label_vocab)], -100, dtype=torch.long) # [label, sibling]
                self.right_sibling_compatible_labels = torch.full([len(self.label_vocab) - 1, len(self.label_vocab)], -100, dtype=torch.long) # [label, sibling]

                for curr_label, sibling_dict in self.left_sibling_compatible_dict.items():
                    if curr_label not in label_vocab:
                        continue
                    if curr_label.strip() == "":
                        continue
                    for sibling, _num in sibling_dict.items():
                        if sibling not in label_vocab:
                            continue
                        if _num >= hparams.sibling_compatible_threshold:
                            self.left_sibling_compatible_labels[label_vocab[curr_label] - 1, label_vocab[sibling]] = 1
                for curr_label, sibling_dict in self.right_sibling_compatible_dict.items():
                    if curr_label not in label_vocab:
                        continue
                    if curr_label.strip() == "":
                        continue
                    for sibling, _num in sibling_dict.items():
                        if sibling not in label_vocab:
                            continue
                        if _num >= hparams.sibling_compatible_threshold:
                            self.right_sibling_compatible_labels[label_vocab[curr_label] - 1, label_vocab[sibling]] = 1
                self.num_positive_left_compatible = torch.sum(torch.where(self.left_sibling_compatible_labels > 0, self.left_sibling_compatible_labels, torch.tensor(0)))
                print("num_positive_left_compatible: {}, {}".format(self.num_positive_left_compatible, int(self.num_positive_left_compatible)/(len(label_vocab)*(len(label_vocab) - 1))))
                self.num_positive_right_compatible = torch.sum(torch.where(self.right_sibling_compatible_labels > 0, self.right_sibling_compatible_labels, torch.tensor(0)))
                print("num_positive_right_compatible: {}, {}".format(self.num_positive_right_compatible, int(self.num_positive_right_compatible)/(len(label_vocab)*(len(label_vocab) - 1))))

            else:
                self.sibling_compatible_loss_scale = None
                self.biaffine_matrix_left_middle = None
                self.biaffine_matrix_right_middle = None
                self.biaffine_matrix_left_right = None
        else:
            self.sibling_loss_scale = None
            self.f_left_sibling = None
            self.f_right_sibling = None
            self.sibling_compatible_loss_scale = None
            self.biaffine_matrix_left_middle = None
            self.biaffine_matrix_right_middle = None
            self.biaffine_matrix_left_right = None

        
        self.decoder = decode_chart.ChartDecoder(
            label_vocab=self.label_vocab,
            force_root_constituent=hparams.force_root_constituent,
        )
        self.criterion = decode_chart.SpanClassificationMarginLoss(
            reduction="sum", force_root_constituent=hparams.force_root_constituent
        )

        self.parallelized_devices = None

    @property
    def device(self):
        if self.parallelized_devices is not None:
            return self.parallelized_devices[0]
        else:
            return next(self.f_label.parameters()).device

    @property
    def output_device(self):
        if self.parallelized_devices is not None:
            return self.parallelized_devices[1]
        else:
            return next(self.f_label.parameters()).device

    def parallelize(self, *args, **kwargs):
        self.parallelized_devices = (torch.device("cuda", 0), torch.device("cuda", 1))
        for child in self.children():
            if child != self.pretrained_model:
                child.to(self.output_device)
        self.pretrained_model.parallelize(*args, **kwargs)

    @classmethod
    def from_trained(cls, model_path, input_pretrained_model_path=None):
        if os.path.isdir(model_path):
            # Multi-file format used when exporting models for release.
            # Unlike the checkpoints saved during training, these files include
            # all tokenizer parameters and a copy of the pre-trained model
            # config (rather than downloading these on-demand).
            config = AutoConfig.from_pretrained(model_path).benepar
            state_dict = torch.load(
                os.path.join(model_path, "benepar_model.bin"), map_location="cpu"
            )
            config["pretrained_model_path"] = model_path
        else:
            # Single-file format used for saving checkpoints during training.
            data = torch.load(model_path, map_location="cpu")
            config = data["config"]
            state_dict = data["state_dict"]

        hparams = config["hparams"]

        if "force_root_constituent" not in hparams:
            hparams["force_root_constituent"] = True

        if "use_pattern" not in hparams:
            hparams["use_pattern"] = False
        if "use_compatible" not in hparams:
            hparams["use_compatible"] = False
        if "use_sibling" not in hparams:
            hparams["use_sibling"] = False
        if "use_sibling_compatible" not in hparams:
            hparams["use_sibling_compatible"] = False
        if "use_regularization" not in hparams:
            hparams["use_regularization"] = False

        config["hparams"] = nkutil.HParams(**hparams)
        config["hparams"].pretrained_model = '/data/senyang/bert/bert-large-uncased/' if not input_pretrained_model_path else input_pretrained_model_path
        parser = cls(**config)
        parser.load_state_dict(state_dict)
        return parser

    def encode(self, example):
        if self.char_encoder is not None:
            encoded = self.retokenizer(example.words, return_tensors="np")
        else:
            encoded = self.retokenizer(example.words, example.space_after)

        if example.tree is not None:
            encoded["span_labels"], encoded["left_sib_span_labels"], encoded["right_sib_span_labels"] = torch.tensor(
                self.decoder.chart_from_tree(example.tree)
            )
            if self.f_tag is not None:
                encoded["tag_labels"] = torch.tensor(
                    [-100] + [self.tag_vocab[tag] for _, tag in example.pos()] + [-100]
                )
        return encoded

    def pad_encoded(self, encoded_batch):
        batch = self.retokenizer.pad(
            [
                {
                    k: v
                    for k, v in example.items()
                    if k not in (
                        "span_labels", 
                        "tag_labels", 
                        "pattern_labels", 
                        "compatible_labels", 
                        "left_sib_span_labels", 
                        "right_sib_span_labels", 
                        "left_sibling_compatible_labels",
                        "right_sibling_compatible_labels",
                        )
                }
                for example in encoded_batch
            ],
            return_tensors="pt",
        )
        if encoded_batch and "span_labels" in encoded_batch[0]:
            batch["span_labels"] = decode_chart.pad_charts(
                [example["span_labels"] for example in encoded_batch]
            )
        if encoded_batch and "pattern_labels" in encoded_batch[0]:
            batch["pattern_labels"] = decode_chart.pad_charts(
                [example["pattern_labels"] for example in encoded_batch],
                padding_value=-100
            )
        if encoded_batch and "compatible_labels" in encoded_batch[0]:
        #     batch["compatible_labels"] = torch.cat([_["compatible_labels"].unsqueeze(0) for _ in encoded_batch], dim=0)
            # batch["compatible_labels"] = nn.utils.rnn.pad_sequence(
            #     [example["compatible_labels"] for example in encoded_batch],
            #     batch_first=True,
            #     padding_value=-100,
            # )
            batch["compatible_labels"] = encoded_batch[0]["compatible_labels"]

        if encoded_batch and "right_sib_span_labels" in encoded_batch[0]:
            batch["right_sib_span_labels"] = decode_chart.pad_charts(
                [example["right_sib_span_labels"] for example in encoded_batch]
            )
        if encoded_batch and "left_sib_span_labels" in encoded_batch[0]:
            batch["left_sib_span_labels"] = decode_chart.pad_charts(
                [example["left_sib_span_labels"] for example in encoded_batch]
            )
        
        if encoded_batch and "left_sibling_compatible_labels" in encoded_batch[0]:
            batch["left_sibling_compatible_labels"] = encoded_batch[0]["left_sibling_compatible_labels"]
        if encoded_batch and "right_sibling_compatible_labels" in encoded_batch[0]:
            batch["right_sibling_compatible_labels"] = encoded_batch[0]["right_sibling_compatible_labels"]

        if encoded_batch and "tag_labels" in encoded_batch[0]:
            batch["tag_labels"] = nn.utils.rnn.pad_sequence(
                [example["tag_labels"] for example in encoded_batch],
                batch_first=True,
                padding_value=-100,
            )
        return batch

    def _get_lens(self, encoded_batch):
        if self.pretrained_model is not None:
            return [len(encoded["input_ids"]) for encoded in encoded_batch]
        return [len(encoded["valid_token_mask"]) for encoded in encoded_batch]

    def encode_and_collate_subbatches(self, examples, subbatch_max_tokens, get_pattern_function, strip_top):

        batch_size = len(examples)
        batch_num_tokens = sum(len(x.words) for x in examples)
        encoded = [self.encode(example) for example in examples]

        # new: pattern-classification
        if self.f_pattern is not None:
            # all_possible_spans = from_numpy(np.zeros([int(fencepost_annotations_start.shape[0]*(fencepost_annotations_start.shape[0] + 1)/2), fencepost_annotations_start.shape[0], fencepost_annotations_start.shape[1]], dtype=np.uint8))
            # all_possible_span_states = torch.matmul(all_possible_spans, fencepost_annotations_start)
            trees_text = [' '.join(str(_.tree).split()) for _ in examples]
            outdated_trees = load_trees_from_text('\n'.join(trees_text), strip_top=strip_top, strip_spmrl_features=False)
            batch_patterns = get_pattern_function([outdated_trees], n=self.config["hparams"]["num_ngram"], pattern_num_threshold=0)[1][0]
            pattern_labels_gold = [torch.tril(torch.full_like(encoded[i]["span_labels"], -100), diagonal=-1) for i in range(len(encoded))]
            for i in range(len(encoded)):
                positive_num = 0
                for j in range(len(batch_patterns[i])):
                    start, end, label = batch_patterns[i][j][0], batch_patterns[i][j][1], batch_patterns[i][j][2]
                    end -= 1
                    if label in self.pattern_vocab:
                        curr_pattern_label_index = self.pattern_vocab[label]
                        if curr_pattern_label_index != 0: positive_num += 1
                    else:
                        curr_pattern_label_index = self.pattern_vocab[" "]
                    assert curr_pattern_label_index <= len(self.pattern_vocab) - 1 and curr_pattern_label_index >= 0
                    pattern_labels_gold[i][start, end] = curr_pattern_label_index if curr_pattern_label_index != 0 else -100
                num_negative = min(int(positive_num/len(encoded[i]["span_labels"])) - 1, self.hparams.pattern_num_negative)
                if num_negative > 0:
                    indice_1 = random.sample(range(len(encoded[i]["span_labels"])), positive_num*num_negative)
                    indice_2 = random.sample(range(len(encoded[i]["span_labels"])), positive_num*num_negative)
                    for j in range(positive_num*num_negative):
                        curr_index = [min(indice_1[j], indice_2[j]), max(indice_1[j], indice_2[j])]
                        if pattern_labels_gold[i][curr_index[0], curr_index[1]] == -100:
                            pattern_labels_gold[i][curr_index[0], curr_index[1]] = 0
                else:
                    pattern_labels_gold[i] = torch.where(pattern_labels_gold[i] >= 0, pattern_labels_gold[i], torch.LongTensor([0]))
                encoded[i]["pattern_labels"] = pattern_labels_gold[i].to(self.device)

        # new: pattern-constituent compatibility
        if self.compatible_loss_scale is not None:
            tmp_compatible_labels = self.compatible_labels.clone()
            if (tmp_compatible_labels.shape[0]*tmp_compatible_labels.shape[1]) / int(self.num_positive_compatible) > self.hparams.compatible_num_negative:
                negative_sample_num = int(self.num_positive_compatible)*self.hparams.compatible_num_negative
            else:
                negative_sample_num = tmp_compatible_labels.shape[0]*tmp_compatible_labels.shape[1]
            indice = random.sample(range(tmp_compatible_labels.shape[0]*tmp_compatible_labels.shape[1]), negative_sample_num)
            for i in range(negative_sample_num):
                indice_label = indice[i]//len(self.pattern_vocab)
                indice_pattern = indice[i]%len(self.pattern_vocab)
                if tmp_compatible_labels[indice_pattern, indice_label] == -100:
                    tmp_compatible_labels[indice_pattern, indice_label] = 0 
            for index in range(len(encoded)):
                encoded[index]["compatible_labels"] = tmp_compatible_labels
            # encoded[index]["compatible_labels"] = self.compatible_labels
        
        if self.sibling_compatible_loss_scale is not None:
            # left
            tmp_left_compatible_labels = self.left_sibling_compatible_labels.clone()
            indice = random.sample(range(tmp_left_compatible_labels.shape[0]*tmp_left_compatible_labels.shape[1]), int(self.num_positive_left_compatible)*self.hparams.num_negative_sibling_compatible)
            for i in range(int(self.num_positive_left_compatible)*self.hparams.num_negative_sibling_compatible):
                indice_sibling = indice[i]//(len(self.label_vocab) - 1)
                indice_label = indice[i]%(len(self.label_vocab) - 1)
                if tmp_left_compatible_labels[indice_label, indice_sibling] == -100:
                    tmp_left_compatible_labels[indice_label, indice_sibling] = 0 
            for index in range(len(encoded)):
                encoded[index]["left_sibling_compatible_labels"] = tmp_left_compatible_labels
            # right
            tmp_right_compatible_labels = self.right_sibling_compatible_labels.clone()
            indice = random.sample(range(tmp_right_compatible_labels.shape[0]*tmp_right_compatible_labels.shape[1]), int(self.num_positive_right_compatible)*self.hparams.num_negative_sibling_compatible)
            for i in range(int(self.num_positive_right_compatible)*self.hparams.num_negative_sibling_compatible):
                indice_sibling = indice[i]//(len(self.label_vocab) - 1)
                indice_label = indice[i]%(len(self.label_vocab) - 1)
                if tmp_right_compatible_labels[indice_label, indice_sibling] == -100:
                    tmp_right_compatible_labels[indice_label, indice_sibling] = 0 
            for index in range(len(encoded)):
                encoded[index]["right_sibling_compatible_labels"] = tmp_right_compatible_labels

        res = []
        for ids, subbatch_encoded in subbatching.split(
            encoded, costs=self._get_lens(encoded), max_cost=subbatch_max_tokens,
        ):
            subbatch = self.pad_encoded(subbatch_encoded)
            subbatch["batch_size"] = batch_size
            subbatch["batch_num_tokens"] = batch_num_tokens
            res.append((len(ids), subbatch))
        return res

    def forward(self, batch):
        valid_token_mask = batch["valid_token_mask"].to(self.output_device)

        if (
            self.encoder is not None
            and valid_token_mask.shape[1] > self.add_timing.timing_table.shape[0]
        ):
            raise ValueError(
                "Sentence of length {} exceeds the maximum supported length of "
                "{}".format(
                    valid_token_mask.shape[1] - 2,
                    self.add_timing.timing_table.shape[0] - 2,
                )
            )

        if self.char_encoder is not None:
            assert isinstance(self.char_encoder, char_lstm.CharacterLSTM)
            char_ids = batch["char_ids"].to(self.device)
            extra_content_annotations = self.char_encoder(char_ids, valid_token_mask)
        elif self.pretrained_model is not None:
            input_ids = batch["input_ids"].to(self.device)
            # print(input_ids[:2])
            words_from_tokens = batch["words_from_tokens"].to(self.output_device)
            pretrained_attention_mask = batch["attention_mask"].to(self.device)

            extra_kwargs = {}
            if "token_type_ids" in batch:
                extra_kwargs["token_type_ids"] = batch["token_type_ids"].to(self.device)
            if "decoder_input_ids" in batch:
                extra_kwargs["decoder_input_ids"] = batch["decoder_input_ids"].to(
                    self.device
                )
                extra_kwargs["decoder_attention_mask"] = batch[
                    "decoder_attention_mask"
                ].to(self.device)

            pretrained_out = self.pretrained_model(
                input_ids, attention_mask=pretrained_attention_mask, **extra_kwargs
            )
            features = pretrained_out.last_hidden_state.to(self.output_device)
            features = features[
                torch.arange(features.shape[0])[:, None],
                # Note that words_from_tokens uses index -100 for invalid positions
                F.relu(words_from_tokens),
            ]
            features.masked_fill_(~valid_token_mask[:, :, None], 0)
            if self.encoder is not None:
                extra_content_annotations = self.project_pretrained(features)

        if self.encoder is not None:
            encoder_in = self.add_timing(
                self.morpho_emb_dropout(extra_content_annotations)
            )

            annotations = self.encoder(encoder_in, valid_token_mask)
            # Rearrange the annotations to ensure that the transition to
            # fenceposts captures an even split between position and content.
            # TODO(nikita): try alternatives, such as omitting position entirely
            annotations = torch.cat(
                [
                    annotations[..., 0::2],
                    annotations[..., 1::2],
                ],
                -1,
            )
        else:
            assert self.pretrained_model is not None
            annotations = self.project_pretrained(features)

        if self.f_tag is not None:
            tag_scores = self.f_tag(annotations)
        else:
            tag_scores = None

        fencepost_annotations = torch.cat(
            [
                annotations[:, :-1, : self.d_model // 2],
                annotations[:, 1:, self.d_model // 2 :],
            ],
            -1,
        )

        # Note that the bias added to the final layer norm is useless because
        # this subtraction gets rid of it
        span_features = (
            torch.unsqueeze(fencepost_annotations, 1)
            - torch.unsqueeze(fencepost_annotations, 2)
        )[:, :-1, 1:]
        # span_features: [batch_size, seq_len, seq_len, hidden_size]
        
        span_scores = self.f_label(span_features)
        # span_scores: [batch_size, seq_len, seq_len, label_vocab_size-1]

        span_scores = torch.cat(
            [span_scores.new_zeros(span_scores.shape[:-1] + (1,)), span_scores], -1
        )
        # span_scores: [batch_size, seq_len, seq_len, label_vocab_size], the first dimension is all zero

        if self.f_pattern is not None:
            pattern_scores = self.f_pattern(span_features)
        else:
            pattern_scores = None

        if self.compatible_loss_scale is None:
            compatible_scores = None
        else:
            curr_batch_size = span_features.shape[0]
            curr_seq_len = span_features.shape[1]
            if pattern_scores == None:
                pattern_scores = self.f_pattern(span_features)

            self.biaffine_matrix = self.biaffine_matrix.to(self.device)
            _compatible_scores = torch.mm(self.f_pattern[3].weight, self.biaffine_matrix)
            _compatible_scores = torch.mm(_compatible_scores, torch.transpose(self.f_label[3].weight, 0, 1))
            compatible_scores = torch.sigmoid(_compatible_scores).unsqueeze(-1)
            compatible_scores = torch.cat([1 - compatible_scores, compatible_scores], dim=-1)
            compatible_scores = torch.log(compatible_scores + 1e-20)
        
        if self.sibling_loss_scale is None:
            left_sibling_scores, right_sibling_scores, left_sibling_compatible_scores, right_sibling_compatible_scores = None, None, None, None
        else:
            left_sibling_scores = self.f_left_sibling(span_features)
            right_sibling_scores = self.f_right_sibling(span_features)
            if self.sibling_compatible_loss_scale is None:
                left_sibling_compatible_scores, right_sibling_compatible_scores = None, None
            else:
                curr_batch_size = span_features.shape[0]
                curr_seq_len = span_features.shape[1]
                self.biaffine_matrix_left_middle = self.biaffine_matrix_left_middle.to(self.device)
                self.biaffine_matrix_right_middle = self.biaffine_matrix_right_middle.to(self.device)
                _compatible_scores_left = torch.mm(
                                                    torch.mm(self.f_label[3].weight, self.biaffine_matrix_left_middle), 
                                                    torch.transpose(self.f_left_sibling[3].weight, 0, 1)
                                                    )
                left_sibling_compatible_scores = torch.sigmoid(_compatible_scores_left).unsqueeze(-1)
                left_sibling_compatible_scores = torch.log(
                    torch.cat([1 - left_sibling_compatible_scores, left_sibling_compatible_scores], dim=-1) + 1e-20
                    )
                _compatible_scores_right = torch.mm(
                                                    torch.mm(self.f_label[3].weight, self.biaffine_matrix_right_middle), 
                                                    torch.transpose(self.f_right_sibling[3].weight, 0, 1)
                                                    )
                right_sibling_compatible_scores = torch.sigmoid(_compatible_scores_right).unsqueeze(-1)
                right_sibling_compatible_scores = torch.log(
                    torch.cat([1 - right_sibling_compatible_scores, right_sibling_compatible_scores], dim=-1) + 1e-20
                    )
        
        return span_scores, tag_scores, pattern_scores, compatible_scores, left_sibling_scores, right_sibling_scores, left_sibling_compatible_scores, right_sibling_compatible_scores

    def compute_loss(self, batch, return_confusion_matrix=False):
        all_scores= self.forward(batch)
        span_scores, tag_scores = all_scores[0:2]
        pattern_scores, compatible_scores = all_scores[2:4]
        left_sibling_scores, right_sibling_scores, left_sibling_compatible_scores, right_sibling_compatible_scores = all_scores[4:8]

        span_labels = batch["span_labels"].to(span_scores.device)
        span_loss = self.criterion(span_scores, span_labels)
        # Divide by the total batch size, not by the subbatch size
        span_loss = span_loss / batch["batch_size"]
        total_loss = span_loss



        if tag_scores is not None:
            tag_labels = batch["tag_labels"].to(tag_scores.device)
            tag_loss = self.tag_loss_scale * F.cross_entropy(
                tag_scores.reshape((-1, tag_scores.shape[-1])),
                tag_labels.reshape((-1,)),
                reduction="sum",
                ignore_index=-100,
            )
            tag_loss = tag_loss / batch["batch_num_tokens"]
            total_loss = total_loss + tag_loss
        else:
            tag_loss = None
        
        if pattern_scores is not None:
            pattern_labels = batch["pattern_labels"].to(pattern_scores.device)
            # XXX ??
            _positive_loss_scale = 1
            negative_loss_scale = 1/(1+_positive_loss_scale)
            # for _ in pattern_labels.view(-1):
            #     assert _ <= len(self.pattern_vocab) - 1 or _ >= 0
            pattern_loss = self.pattern_loss_scale * F.cross_entropy(
                pattern_scores.reshape((-1, len(self.pattern_vocab))),
                pattern_labels.reshape((-1)),
                reduction="mean",
                ignore_index=-100,
                # weight=torch.tensor([negative_loss_scale, 1-negative_loss_scale], device=pattern_scores.device)
            )
            total_loss = total_loss + pattern_loss
        else:
            pattern_loss = None
        
        if compatible_scores is not None:
            compatible_labels = batch["compatible_labels"].to(compatible_scores.device)

            # confusion matrix
            if return_confusion_matrix:
                selected_indexes = torch.cat([(compatible_labels.reshape((-1)) == 0).nonzero(as_tuple=False)[:, 0], (compatible_labels.reshape((-1)) == 1).nonzero(as_tuple=False)[:, 0]])
                predicted = compatible_scores.reshape((-1, 2)).argmax(-1)[selected_indexes].tolist()
                gold = compatible_labels.reshape((-1))[selected_indexes].tolist()
                assert len(predicted) == len(gold)
                confusion_matrix = sklearn.metrics.confusion_matrix(gold, predicted)
            else:
                confusion_matrix = None
            
            _positive_loss_scale = 1
            negative_loss_scale = 1/(1+_positive_loss_scale)
            compatible_loss = self.compatible_loss_scale * F.nll_loss(
                compatible_scores.reshape((-1, 2)),
                compatible_labels.reshape((-1)),
                reduction="mean",
                ignore_index=-100,
                # weight=torch.tensor([negative_loss_scale, 1-negative_loss_scale], device=compatible_scores.device)
            )
            total_loss = total_loss + compatible_loss
        else:
            compatible_loss = None
            confusion_matrix = None
        
        if left_sibling_scores is not None:
            left_sibling_labels = batch["left_sib_span_labels"].to(left_sibling_scores.device)
            right_sibling_labels = batch["right_sib_span_labels"].to(right_sibling_scores.device)

            left_sibling_loss = self.sibling_loss_scale * F.cross_entropy(
                left_sibling_scores.reshape((-1, self.f_left_sibling[3].weight.shape[0])),
                left_sibling_labels.reshape((-1)),
                reduction="mean",
                ignore_index=-100,
            )
            right_sibling_loss = self.sibling_loss_scale * F.cross_entropy(
                right_sibling_scores.reshape((-1, self.f_right_sibling[3].weight.shape[0])),
                right_sibling_labels.reshape((-1)),
                reduction="mean",
                ignore_index=-100,
            )
            sibling_loss = left_sibling_loss + right_sibling_loss
            total_loss = total_loss + sibling_loss
        else:
            sibling_loss = None

        if left_sibling_compatible_scores is not None:
            left_sibling_compatible_labels = batch["left_sibling_compatible_labels"].to(left_sibling_compatible_scores.device)
            right_sibling_compatible_labels = batch["right_sibling_compatible_labels"].to(right_sibling_compatible_scores.device)
            left_sibling_compatible_loss = self.sibling_compatible_loss_scale * F.nll_loss(
                left_sibling_compatible_scores.reshape((-1, 2)),
                left_sibling_compatible_labels.reshape((-1)),
                reduction="mean",
                ignore_index=-100,
            )
            right_sibling_compatible_loss = self.sibling_compatible_loss_scale * F.nll_loss(
                right_sibling_compatible_scores.reshape((-1, 2)),
                right_sibling_compatible_labels.reshape((-1)),
                reduction="mean",
                ignore_index=-100,
            )
            sibling_compatible_loss = left_sibling_compatible_loss + right_sibling_compatible_loss
            total_loss = total_loss + sibling_compatible_loss
        else:
            sibling_compatible_loss = None

        detailed_loss = {
            "label_loss": span_loss, 
            "tag_loss": tag_loss, 
            "pattern_loss": pattern_loss, 
            "compatible_loss": compatible_loss,
            "sibling_loss": sibling_loss,
            "sibling_compatible_loss": sibling_compatible_loss,
            }
        return total_loss, detailed_loss, confusion_matrix

    def _parse_encoded(
        self, examples, encoded, return_compressed=False, return_scores=False
    ):
        with torch.no_grad():
            batch = self.pad_encoded(encoded)
            all_scores= self.forward(batch)
            span_scores, tag_scores = all_scores[0:2]
            pattern_scores, compatible_scores = all_scores[2:4]
            left_sibling_scores, right_sibling_scores, left_sibling_compatible_scores, right_sibling_compatible_scores = all_scores[4:8]

            if return_scores:
                span_scores_np = span_scores.cpu().numpy()
            else:
                # Start/stop tokens don't count, so subtract 2
                lengths = batch["valid_token_mask"].sum(-1) - 2
                charts_np = self.decoder.charts_from_pytorch_scores_batched(
                    span_scores, lengths.to(span_scores.device)
                )
            if tag_scores is not None:
                tag_ids_np = tag_scores.argmax(-1).cpu().numpy()
            else:
                tag_ids_np = None

        for i in range(len(encoded)):
            example_len = len(examples[i].words)
            if return_scores:
                yield span_scores_np[i, :example_len, :example_len]
            elif return_compressed:
                output = self.decoder.compressed_output_from_chart(charts_np[i])
                if tag_ids_np is not None:
                    output = output.with_tags(tag_ids_np[i, 1 : example_len + 1])
                yield output
            else:
                if tag_scores is None:
                    leaves = examples[i].pos()
                else:
                    predicted_tags = [
                        self.tag_from_index[i]
                        for i in tag_ids_np[i, 1 : example_len + 1]
                    ]
                    leaves = [
                        (word, predicted_tag)
                        for predicted_tag, (word, gold_tag) in zip(
                            predicted_tags, examples[i].pos()
                        )
                    ]
                yield self.decoder.tree_from_chart(charts_np[i], leaves=leaves)

    def parse(
        self,
        examples,
        return_compressed=False,
        return_scores=False,
        subbatch_max_tokens=None,
    ):
        training = self.training
        self.eval()
        encoded = [self.encode(example) for example in examples]
        if subbatch_max_tokens is not None:
            res = subbatching.map(
                self._parse_encoded,
                examples,
                encoded,
                costs=self._get_lens(encoded),
                max_cost=subbatch_max_tokens,
                return_compressed=return_compressed,
                return_scores=return_scores,
            )
        else:
            res = self._parse_encoded(
                examples,
                encoded,
                return_compressed=return_compressed,
                return_scores=return_scores,
            )
            res = list(res)
        self.train(training)
        return res
