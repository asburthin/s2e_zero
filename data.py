import json
import logging
import os
import pickle
from collections import namedtuple

import torch

from consts import SPEAKER_START, SPEAKER_END, NULL_ID_FOR_COREF
from utils import flatten_list_of_lists
from torch.utils.data import Dataset

CorefExample = namedtuple("CorefExample", ["token_ids", "clusters"])

logger = logging.getLogger(__name__)


class CorefDataset(Dataset):
    def __init__(self, file_path, tokenizer, max_seq_length=-1):
        self.tokenizer = tokenizer
        logger.info(f"Reading dataset from {file_path}")
        examples, self.max_mention_num, self.max_cluster_size, self.max_num_clusters = self._parse_jsonlines(file_path)
        self.max_seq_length = max_seq_length
        self.examples, self.lengths, self.num_examples_filtered = self._tokenize(examples)
        logger.info(
            f"Finished preprocessing Coref dataset. {len(self.examples)} examples were extracted, {self.num_examples_filtered} were filtered due to sequence length.")

    def _parse_jsonlines(self, file_path):
        """Parse the jsonlines file into a list of examples.
        Args:
            file_path: path to the jsonlines file
        Returns:
            examples: list of doc_key, input_words, clusters
            max_mention_num: maximum number of mentions in a single example
            max_cluster_size: maximum number of mentions in a single cluster
            max_num_clusters: maximum number of clusters in a single example
            """
        examples = []
        max_mention_num = -1
        max_cluster_size = -1
        max_num_clusters = -1
        with open(file_path, 'r') as f:
            for line in f:
                # Each line is a json object that represents a single example
                # The json object contains the following fields:
                #   doc_key: document key
                #   sentences: list of sentences, each sentence is a list of words
                #   clusters: list of clusters, each cluster is a list of mentions, each mention is a list of [sentence_idx, start_token_idx, end_token_idx]

                d = json.loads(line.strip())
                doc_key = d["doc_key"]
                input_words = flatten_list_of_lists(d["sentences"])
                clusters = d["clusters"]

                # Max mention num is the maximum number of mentions in a single example
                max_mention_num = max(max_mention_num, len(flatten_list_of_lists(clusters)))
                # Max cluster size is the maximum number of mentions in a single cluster
                max_cluster_size = max(max_cluster_size, max(len(cluster) for cluster in clusters) if clusters else 0)
                # Max num clusters is the maximum number of clusters in a single example
                max_num_clusters = max(max_num_clusters, len(clusters) if clusters else 0)
                examples.append((doc_key, input_words, clusters))
        return examples, max_mention_num, max_cluster_size, max_num_clusters

    def _tokenize(self, examples):
        """Tokenize the examples."""
        coref_examples = []
        lengths = []
        num_examples_filtered = 0
        for doc_key, words, clusters in examples:
            word_idx_to_start_token_idx = dict()
            word_idx_to_end_token_idx = dict()
            end_token_idx_to_word_idx = [0]  # for <s>

            token_ids = []
            for idx, word in enumerate(words):
                word_idx_to_start_token_idx[idx] = len(token_ids) + 1  # +1 for <s>
                tokenized = self.tokenizer.tokenize(word)
                for _ in range(len(tokenized)):
                    end_token_idx_to_word_idx.append(idx)
                token_ids.extend(tokenized)
                word_idx_to_end_token_idx[idx] = len(token_ids)  # old_seq_len + 1 (for <s>) + len(tokenized_word) - 1 (we start counting from zero) = len(token_ids)

            if 0 < self.max_seq_length < len(token_ids):
                num_examples_filtered += 1
                continue

            new_clusters = [
                [(word_idx_to_start_token_idx[start], word_idx_to_end_token_idx[end - 1]) for start, end in cluster] for
                cluster in clusters]
            lengths.append(len(token_ids))

            # CorefExample = namedtuple("CorefExample", ["token_ids", "clusters"])
            # Example: 
            # Text = "John Smith is a nice guy. He lives in London."
            # CorefExample = {
            #  token_ids: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
            #  clusters: [[(0, 1), (6, 6)]]
            coref_examples.append(((doc_key, end_token_idx_to_word_idx), CorefExample(token_ids=token_ids, clusters=new_clusters)))
        return coref_examples, lengths, num_examples_filtered

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, item):
        return self.examples[item]

    def pad_clusters_inside(self, clusters):
        return [cluster + [(NULL_ID_FOR_COREF, NULL_ID_FOR_COREF)] * (self.max_cluster_size - len(cluster)) for cluster
                in clusters]

    def pad_clusters_outside(self, clusters):
        return clusters + [[]] * (self.max_num_clusters - len(clusters))

    def pad_clusters(self, clusters):
        clusters = self.pad_clusters_outside(clusters)
        clusters = self.pad_clusters_inside(clusters)
        return clusters

    def pad_batch(self, batch, max_length):
        max_length += 2  # we have additional two special tokens <s>, </s>
        padded_batch = []
        for example in batch:
            encoded_dict = self.tokenizer.encode_plus(example[0],
                                                      add_special_tokens=True,
                                                      pad_to_max_length=True,
                                                      max_length=max_length,
                                                      return_attention_mask=True,
                                                      is_split_into_words=True,
                                                      return_tensors='pt')
            clusters = self.pad_clusters(example.clusters)
            example = (encoded_dict["input_ids"], encoded_dict["attention_mask"]) + (torch.tensor(clusters),)
            padded_batch.append(example)
        tensored_batch = tuple(torch.stack([example[i].squeeze() for example in padded_batch], dim=0) for i in range(len(example)))
        return tensored_batch


def get_dataset(args, tokenizer, evaluate=False):
    read_from_cache, file_path = False, ''
    if evaluate and os.path.exists(args.predict_file_cache):
        file_path = args.predict_file_cache
        read_from_cache = True
    elif (not evaluate) and os.path.exists(args.train_file_cache):
        file_path = args.train_file_cache
        read_from_cache = True

    if read_from_cache:
        logger.info(f"Reading dataset from {file_path}")
        with open(file_path, 'rb') as f:
            return pickle.load(f)

    file_path, cache_path = (args.predict_file, args.predict_file_cache) if evaluate else (args.train_file, args.train_file_cache)

    coref_dataset = CorefDataset(file_path, tokenizer, max_seq_length=args.max_seq_length)
    with open(cache_path, 'wb') as f:
        pickle.dump(coref_dataset, f)

    return coref_dataset
