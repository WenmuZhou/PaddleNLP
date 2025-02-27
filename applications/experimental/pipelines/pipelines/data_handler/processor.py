# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2021 deepset GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import json
import logging
import os
import random
import tarfile
import tempfile
import uuid
import requests
from tqdm import tqdm
from abc import ABC, abstractmethod
from inspect import signature
from pathlib import Path
from io import StringIO
from typing import Optional, Dict, List, Union, Any, Iterable

import pandas as pd
import numpy as np
from pipelines.utils.tokenization import tokenize_batch_question_answering

from pipelines.data_handler.dataset import convert_features_to_dataset
from pipelines.data_handler.samples import (
    Sample,
    SampleBasket,
    get_passage_offsets,
    offset_to_token_idx_vecorized,
)
from pipelines.utils.logger import StdoutLogger

logger = logging.getLogger(__name__)


class Processor(ABC):
    """
    Base class for low level data processors to convert input text to PaddleNLP Datasets.
    """

    subclasses: dict = {}

    def __init__(
        self,
        tokenizer,
        max_seq_len: int,
        train_filename: Optional[Union[Path, str]],
        dev_filename: Optional[Union[Path, str]],
        test_filename: Optional[Union[Path, str]],
        dev_split: float,
        data_dir: Optional[Union[Path, str]],
        tasks: Dict = {},
        proxies: Optional[Dict] = None,
        multithreading_rust: Optional[bool] = True,
    ):
        """
        :param tokenizer: Used to split a sentence (str) into tokens.
        :param max_seq_len: Samples are truncated after this many tokens.
        :param train_filename: The name of the file containing training data.
        :param dev_filename: The name of the file containing the dev data. If None and 0.0 < dev_split < 1.0 the dev set
                             will be a slice of the train set.
        :param test_filename: The name of the file containing test data.
        :param dev_split: The proportion of the train set that will sliced. Only works if dev_filename is set to None
        :param data_dir: The directory in which the train, test and perhaps dev files can be found.
        :param tasks: Tasks for which the processor shall extract labels from the input data.
                      Usually this includes a single, default task, e.g. text classification.
                      In a multitask setting this includes multiple tasks, e.g. 2x text classification.
                      The task name will be used to connect with the related PredictionHead.
        :param proxies: proxy configuration to allow downloads of remote datasets.
                    Format as in  "requests" library: https://2.python-requests.org//en/latest/user/advanced/#proxies
        :param multithreading_rust: Whether to allow multithreading in Rust, e.g. for FastTokenizers.
                                    Note: Enabling multithreading in Rust AND multiprocessing in python might cause
                                    deadlocks.
        """
        if not multithreading_rust:
            os.environ["RAYON_RS_NUM_CPUS"] = "1"

        self.tokenizer = tokenizer
        self.max_seq_len = max_seq_len
        self.tasks = tasks
        self.proxies = proxies

        # data sets
        self.train_filename = train_filename
        self.dev_filename = dev_filename
        self.test_filename = test_filename
        self.dev_split = dev_split
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = None  # type: ignore
        self.baskets: List = []

        self._log_params()
        self.problematic_sample_ids: set = set()

    def __init_subclass__(cls, **kwargs):
        """This automatically keeps track of all available subclasses.
        Enables generic load() and load_from_dir() for all specific Processor implementation.
        """
        super().__init_subclass__(**kwargs)
        cls.subclasses[cls.__name__] = cls

    # TODO potentially remove tasks from code - multitask learning is not supported anyways
    def add_task(self,
                 name,
                 metric,
                 label_list,
                 label_column_name=None,
                 label_name=None,
                 task_type=None,
                 text_column_name=None):
        if type(label_list) is not list:
            raise ValueError(
                f"Argument `label_list` must be of type list. Got: f{type(label_list)}"
            )

        if label_name is None:
            label_name = f"{name}_label"
        label_tensor_name = label_name + "_ids"
        self.tasks[name] = {
            "label_list": label_list,
            "metric": metric,
            "label_tensor_name": label_tensor_name,
            "label_name": label_name,
            "label_column_name": label_column_name,
            "text_column_name": text_column_name,
            "task_type": task_type,
        }

    @abstractmethod
    def dataset_from_dicts(self,
                           dicts: List[dict],
                           indices: Optional[List[int]] = None,
                           return_baskets: bool = False):
        raise NotImplementedError()

    @abstractmethod
    def _create_dataset(self, baskets: List[SampleBasket]):
        raise NotImplementedError

    @staticmethod
    def log_problematic(problematic_sample_ids):
        if problematic_sample_ids:
            n_problematic = len(problematic_sample_ids)
            problematic_id_str = ", ".join(
                [str(i) for i in problematic_sample_ids])
            logger.error(
                f"Unable to convert {n_problematic} samples to features. Their ids are : {problematic_id_str}"
            )

    @staticmethod
    def _check_sample_features(basket: SampleBasket):
        """
        Check if all samples in the basket has computed its features.

        :param basket: the basket containing the samples

        :return: True if all the samples in the basket has computed its features, False otherwise
        """
        if basket.samples is None:
            return False
        elif len(basket.samples) == 0:
            return False
        if basket.samples is None:
            return False
        else:
            for sample in basket.samples:
                if sample.features is None:
                    return False
        return True

    def _log_samples(self, n_samples: int, baskets: List[SampleBasket]):
        logger.debug("*** Show {} random examples ***".format(n_samples))
        if len(baskets) == 0:
            logger.debug(
                "*** No samples to show because there are no baskets ***")
            return
        for i in range(n_samples):
            random_basket = random.choice(baskets)
            random_sample = random.choice(random_basket.samples)  # type: ignore
            logger.debug(random_sample)

    def _log_params(self):
        params = {
            "processor": self.__class__.__name__,
            "tokenizer": self.tokenizer.__class__.__name__,
        }
        names = ["max_seq_len", "dev_split"]
        for name in names:
            value = getattr(self, name)
            params.update({name: str(value)})
        StdoutLogger.log_params(params)


class SquadProcessor(Processor):
    """
    Convert QA data (in SQuAD Format)
    """

    def __init__(
        self,
        tokenizer,  # type: ignore
        max_seq_len: int,
        data_dir: Optional[Union[Path, str]],
        label_list: Optional[List[str]] = None,
        metric="squad",  # type: ignore
        train_filename: Optional[Union[Path, str]] = Path("train-v2.0.json"),
        dev_filename: Optional[Union[Path, str]] = Path("dev-v2.0.json"),
        test_filename: Optional[Union[Path, str]] = None,
        dev_split: float = 0,
        doc_stride: int = 128,
        max_query_length: int = 64,
        proxies: Optional[dict] = None,
        max_answers: int = 6,
        **kwargs,
    ):
        """
        :param tokenizer: Used to split a sentence (str) into tokens.
        :param max_seq_len: Samples are truncated after this many tokens.
        :param data_dir: The directory in which the train and dev files can be found.
                         If not available the dataset will be loaded automaticaly
                         if the last directory has the same name as a predefined dataset.
                         These predefined datasets are defined as the keys in the dict at
                         `pipelines.basics.data_handler.utils.`_.
        :param label_list: list of labels to predict (strings). For most cases this should be: ["start_token", "end_token"]
        :param metric: name of metric that shall be used for evaluation, can be "squad" or "top_n_accuracy"
        :param train_filename: The name of the file containing training data.
        :param dev_filename: The name of the file containing the dev data. If None and 0.0 < dev_split < 1.0 the dev set
                             will be a slice of the train set.
        :param test_filename: None
        :param dev_split: The proportion of the train set that will sliced. Only works if dev_filename is set to None
        :param doc_stride: When the document containing the answer is too long it gets split into part, strided by doc_stride
        :param max_query_length: Maximum length of the question (in number of subword tokens)
        :param proxies: proxy configuration to allow downloads of remote datasets.
                        Format as in  "requests" library: https://2.python-requests.org//en/latest/user/advanced/#proxies
        :param max_answers: number of answers to be converted. QA dev or train sets can contain multi-way annotations, which are converted to arrays of max_answer length
        :param kwargs: placeholder for passing generic parameters
        """
        self.ph_output_type = "per_token_squad"

        assert doc_stride < (max_seq_len - max_query_length), (
            "doc_stride ({}) is longer than max_seq_len ({}) minus space reserved for query tokens ({}). \nThis means that there will be gaps "
            "as the passage windows slide, causing the model to skip over parts of the document.\n"
            "Please set a lower value for doc_stride (Suggestions: doc_stride=128, max_seq_len=384)\n "
            "Or decrease max_query_length".format(doc_stride, max_seq_len,
                                                  max_query_length))

        self.doc_stride = doc_stride
        self.max_query_length = max_query_length
        self.max_answers = max_answers
        super(SquadProcessor, self).__init__(
            tokenizer=tokenizer,
            max_seq_len=max_seq_len,
            train_filename=train_filename,
            dev_filename=dev_filename,
            test_filename=test_filename,
            dev_split=dev_split,
            data_dir=data_dir,
            tasks={},
            proxies=proxies,
        )
        self._initialize_special_tokens_count()
        if metric and label_list:
            self.add_task("question_answering", metric, label_list)
        else:
            logger.info(
                "Initialized processor without tasks. Supply `metric` and `label_list` to the constructor for "
                "using the default task or add a custom task later via processor.add_task()"
            )

    def dataset_from_dicts(self,
                           dicts: List[dict],
                           indices: Optional[List[int]] = None,
                           return_baskets: bool = False):
        """
        Convert input dictionaries into a paddlenlp dataset for Question Answering.
        For this we have an internal representation called "baskets".
        Each basket is a question-document pair.
        Each stage adds or transforms specific information to our baskets.

        :param dicts: dict, input dictionary with SQuAD style information present
        :param indices: list, indices used during multiprocessing so that IDs assigned to our baskets is unique
        :param return_baskets: boolean, whether to return the baskets or not (baskets are needed during inference)
        """
        # Convert to standard format
        # Have no effect on BasicQA tutorial
        pre_baskets = [self.convert_qa_input_dict(x)
                       for x in dicts]  # TODO move to input object conversion

        # Step1: Tokenize documents and questions
        baskets = tokenize_batch_question_answering(pre_baskets, self.tokenizer,
                                                    indices)

        # Split documents into smaller passages to fit max_seq_len
        baskets = self._split_docs_into_passages(baskets)

        # Convert answers from string to token space, skip this step for inference
        if not return_baskets:
            baskets = self._convert_answers(baskets)

        # Convert internal representation (nested baskets + samples with mixed types) to paddle features (arrays of numbers)
        baskets = self._passages_to_paddle_features(baskets, return_baskets)

        # Convert features into paddle dataset, this step also removes potential errors during preprocessing
        dataset, tensor_names, baskets = self._create_dataset(baskets)

        # Logging
        if indices:
            if 0 in indices:
                self._log_samples(n_samples=1, baskets=self.baskets)

        # During inference we need to keep the information contained in baskets.
        if return_baskets:
            return dataset, tensor_names, self.problematic_sample_ids, baskets
        else:
            return dataset, tensor_names, self.problematic_sample_ids

    # TODO use Input Objects instead of this function, remove Natural Questions (NQ) related code
    def convert_qa_input_dict(self, infer_dict: dict):
        """Input dictionaries in QA can either have ["context", "qas"] (internal format) as keys or
        ["text", "questions"] (api format). This function converts the latter into the former. It also converts the
        is_impossible field to answer_type so that NQ and SQuAD dicts have the same format.
        """
        # check again for doc stride vs max_seq_len when. Parameters can be changed for already initialized models (e.g. in pipelines)
        assert self.doc_stride < (self.max_seq_len - self.max_query_length), (
            "doc_stride ({}) is longer than max_seq_len ({}) minus space reserved for query tokens ({}). \nThis means that there will be gaps "
            "as the passage windows slide, causing the model to skip over parts of the document.\n"
            "Please set a lower value for doc_stride (Suggestions: doc_stride=128, max_seq_len=384)\n "
            "Or decrease max_query_length".format(self.doc_stride,
                                                  self.max_seq_len,
                                                  self.max_query_length))

        try:
            # Check if infer_dict is already in internal json format
            if "context" in infer_dict and "qas" in infer_dict:
                return infer_dict
            # converts dicts from inference mode to data structure used in pipelines
            questions = infer_dict["questions"]
            text = infer_dict["text"]
            uid = infer_dict.get("id", None)
            qas = [{
                "question": q,
                "id": uid,
                "answers": [],
                "answer_type": None
            } for i, q in enumerate(questions)]
            converted = {"qas": qas, "context": text}
            return converted
        except KeyError:
            raise Exception("Input does not have the expected format")

    def _initialize_special_tokens_count(self):
        vec = self.tokenizer.build_inputs_with_special_tokens(token_ids_0=["a"],
                                                              token_ids_1=["b"])
        self.sp_toks_start = vec.index("a")
        self.sp_toks_mid = vec.index("b") - self.sp_toks_start - 1
        self.sp_toks_end = len(vec) - vec.index("b") - 1

    def _split_docs_into_passages(self, baskets: List[SampleBasket]):
        """
        Because of the sequence length limitation of Language Models, the documents need to be divided into smaller
        parts that we call passages.
        """
        # n_special_tokens = 4
        n_special_tokens = self.tokenizer.num_special_tokens_to_add(pair=True)
        for basket in baskets:
            samples = []
            ########## perform some basic checking
            # TODO, eventually move checking into input validation functions
            # ignore samples with empty context
            if basket.raw["document_text"] == "":
                logger.warning("Ignoring sample with empty context")
                continue
            ########## end checking

            # Calculate the number of tokens that can be reserved for the passage. This is calculated by considering
            # the max_seq_len, the number of tokens in the question and the number of special tokens that will be added
            # when the question and passage are joined (e.g. [CLS] and [SEP])
            passage_len_t = (
                self.max_seq_len -
                len(basket.raw["question_tokens"][:self.max_query_length]) -
                n_special_tokens)

            # passage_spans is a list of dictionaries where each defines the start and end of each passage
            # on both token and character level
            try:
                passage_spans = get_passage_offsets(
                    basket.raw["document_offsets"], self.doc_stride,
                    passage_len_t, basket.raw["document_text"])
            except Exception as e:
                logger.warning(
                    f"Could not devide document into passages. Document: {basket.raw['document_text'][:200]}\n"
                    f"With error: {e}")
                passage_spans = []

            for passage_span in passage_spans:
                # Unpack each variable in the dictionary. The "_t" and "_c" indicate
                # whether the index is on the token or character level
                passage_start_t = passage_span["passage_start_t"]
                passage_end_t = passage_span["passage_end_t"]
                passage_start_c = passage_span["passage_start_c"]
                passage_end_c = passage_span["passage_end_c"]

                # Token 粒度标志: token 是否为 Words 的开头，如果为 0 则表示该 token 应该与之前的 token 连接起来.
                passage_start_of_word = basket.raw["document_start_of_word"][
                    passage_start_t:passage_end_t]
                passage_tokens = basket.raw["document_tokens"][
                    passage_start_t:passage_end_t]
                passage_text = basket.raw["document_text"][
                    passage_start_c:passage_end_c]

                clear_text = {
                    "passage_text": passage_text,
                    "question_text": basket.raw["question_text"],
                    "passage_id": passage_span["passage_id"],
                }
                tokenized = {
                    "passage_start_t":
                    passage_start_t,
                    "passage_start_c":
                    passage_start_c,
                    "passage_tokens":
                    passage_tokens,
                    "passage_start_of_word":
                    passage_start_of_word,
                    "question_tokens":
                    basket.raw["question_tokens"][:self.max_query_length],
                    "question_offsets":
                    basket.raw["question_offsets"][:self.max_query_length],
                    "question_start_of_word":
                    basket.raw["question_start_of_word"]
                    [:self.max_query_length],
                }
                # The sample ID consists of internal_id and a passage numbering
                # sample_id 最后一位表示 passage-id
                sample_id = f"{basket.id_internal}-{passage_span['passage_id']}"
                samples.append(
                    Sample(id=sample_id,
                           clear_text=clear_text,
                           tokenized=tokenized))

            basket.samples = samples

        return baskets

    def _convert_answers(self, baskets: List[SampleBasket]):
        """
        Converts answers that are pure strings into the token based representation with start and end token offset.
        Can handle multiple answers per question document pair as is common for development/text sets
        """
        for basket in baskets:
            error_in_answer = False
            for num, sample in enumerate(basket.samples):  # type: ignore
                # Dealing with potentially multiple answers (e.g. Squad dev set)
                # Initializing a numpy array of shape (max_answers, 2), filled with -1 for missing values
                label_idxs = np.full((self.max_answers, 2), fill_value=-1)

                if error_in_answer or (len(basket.raw["answers"]) == 0):
                    # If there are no answers we set
                    label_idxs[0, :] = 0
                else:
                    # For all other cases we use start and end token indices, that are relative to the passage
                    for i, answer in enumerate(basket.raw["answers"]):
                        # Calculate start and end relative to document
                        answer_len_c = len(answer["text"])
                        answer_start_c = answer["answer_start"]
                        answer_end_c = answer_start_c + answer_len_c - 1

                        # Convert character offsets to token offsets on document level
                        answer_start_t = offset_to_token_idx_vecorized(
                            basket.raw["document_offsets"], answer_start_c)
                        answer_end_t = offset_to_token_idx_vecorized(
                            basket.raw["document_offsets"], answer_end_c)

                        # Adjust token offsets to be relative to the passage
                        answer_start_t -= sample.tokenized[
                            "passage_start_t"]  # type: ignore
                        answer_end_t -= sample.tokenized[
                            "passage_start_t"]  # type: ignore

                        # Initialize some basic variables
                        question_len_t = len(
                            sample.tokenized["question_tokens"])  # type: ignore
                        passage_len_t = len(
                            sample.tokenized["passage_tokens"])  # type: ignore

                        # Check that start and end are contained within this passage
                        # answer_end_t is 0 if the first token is the answer
                        # answer_end_t is passage_len_t if the last token is the answer
                        if passage_len_t > answer_start_t >= 0 and passage_len_t >= answer_end_t >= 0:
                            # Then adjust the start and end offsets by adding question and special token
                            label_idxs[i][
                                0] = self.sp_toks_start + question_len_t + self.sp_toks_mid + answer_start_t
                            label_idxs[i][
                                1] = self.sp_toks_start + question_len_t + self.sp_toks_mid + answer_end_t
                        # If the start or end of the span answer is outside the passage, treat passage as no_answer
                        else:
                            label_idxs[i][0] = 0
                            label_idxs[i][1] = 0

                        ########## answer checking ##############################
                        # TODO, move this checking into input validation functions and delete wrong examples there
                        # Cases where the answer is not within the current passage will be turned into no answers by the featurization fn
                        if answer_start_t < 0 or answer_end_t >= passage_len_t:
                            pass
                        else:
                            doc_text = basket.raw["document_text"]
                            answer_indices = doc_text[
                                answer_start_c:answer_end_c + 1]
                            answer_text = answer["text"]
                            # check if answer string can be found in context
                            if answer_text not in doc_text:
                                logger.warning(
                                    f"Answer '{answer['text']}' not contained in context.\n"
                                    f"Example will not be converted for training/evaluation."
                                )
                                error_in_answer = True
                                label_idxs[i][
                                    0] = -100  # TODO remove this hack also from featurization
                                label_idxs[i][1] = -100
                                break  # Break loop around answers, so the error message is not shown multiple times
                            if answer_indices.strip() != answer_text.strip():
                                logger.warning(
                                    f"Answer using start/end indices is '{answer_indices}' while gold label text is '{answer_text}'.\n"
                                    f"Example will not be converted for training/evaluation."
                                )
                                error_in_answer = True
                                label_idxs[i][
                                    0] = -100  # TODO remove this hack also from featurization
                                label_idxs[i][1] = -100
                                break  # Break loop around answers, so the error message is not shown multiple times
                        ########## end of checking ####################

                sample.tokenized["labels"] = label_idxs  # type: ignore

        return baskets

    def _passages_to_paddle_features(self, baskets: List[SampleBasket],
                                     return_baskets: bool):
        """
        Convert internal representation (nested baskets + samples with mixed types) to python features (arrays of numbers).
        We first join question and passages into one large vector.
        Then we add vectors for: - input_ids (token ids)
                                 - segment_ids (does a token belong to question or document)
                                 - padding_mask
                                 - span_mask (valid answer tokens)
                                 - start_of_word
        """
        for basket in baskets:
            # Add features to samples
            for num, sample in enumerate(basket.samples):  # type: ignore
                # Initialize some basic variables
                if sample.tokenized is not None:
                    question_tokens = sample.tokenized["question_tokens"]
                    question_start_of_word = sample.tokenized[
                        "question_start_of_word"]
                    question_len_t = len(question_tokens)
                    passage_start_t = sample.tokenized["passage_start_t"]
                    passage_tokens = sample.tokenized["passage_tokens"]
                    passage_start_of_word = sample.tokenized[
                        "passage_start_of_word"]
                    passage_len_t = len(passage_tokens)
                    sample_id = [int(x) for x in sample.id.split("-")]

                    # - Combines question_tokens and passage_tokens into a single vector called input_ids
                    # - input_ids also contains special tokens (e.g. CLS or SEP tokens).
                    # - It will have length = question_len_t + passage_len_t + n_special_tokens. This may be less than
                    #   max_seq_len but never greater since truncation was already performed when the document was chunked into passages
                    question_input_ids = sample.tokenized["question_tokens"]
                    passage_input_ids = sample.tokenized["passage_tokens"]

                input_ids = self.tokenizer.build_inputs_with_special_tokens(
                    token_ids_0=question_input_ids,
                    token_ids_1=passage_input_ids)

                segment_ids = self.tokenizer.create_token_type_ids_from_sequences(
                    token_ids_0=question_input_ids,
                    token_ids_1=passage_input_ids)
                # To make the start index of passage tokens the start manually
                # self.sp_toks_start = 1
                # self.sp_toks_mid = 2
                # self.sp_toks_end = 1
                # [0, 'a', 2, 2, 'b', 2] = self.tokenizer.build_inputs_with_special_tokens(token_ids_0=["a"], token_ids_1=["b"])
                seq_2_start_t = self.sp_toks_start + question_len_t + self.sp_toks_mid

                start_of_word = ([0] * self.sp_toks_start +
                                 question_start_of_word +
                                 [0] * self.sp_toks_mid +
                                 passage_start_of_word + [0] * self.sp_toks_end)

                # The mask has 1 for real tokens and 0 for padding tokens. Only real
                # tokens are attended to.
                padding_mask = [1] * len(input_ids)

                # The span_mask has 1 for tokens that are valid start or end tokens for QA spans.
                # 0s are assigned to question tokens, mid special tokens, end special tokens, and padding
                # Note that start special tokens are assigned 1 since they can be chosen for a no_answer prediction
                span_mask = [1] * self.sp_toks_start
                span_mask += [0] * question_len_t
                span_mask += [0] * self.sp_toks_mid
                span_mask += [1] * passage_len_t
                span_mask += [0] * self.sp_toks_end

                # Pad up to the sequence length. For certain models, the pad token id is not 0 (e.g. Roberta where it is 1)
                pad_idx = self.tokenizer.pad_token_id
                padding = [pad_idx] * (self.max_seq_len - len(input_ids))
                zero_padding = [0] * (self.max_seq_len - len(input_ids))

                input_ids += padding
                padding_mask += zero_padding
                segment_ids += zero_padding
                start_of_word += zero_padding
                span_mask += zero_padding

                # TODO possibly remove these checks after input validation is in place
                len_check = (len(input_ids) == len(padding_mask) ==
                             len(segment_ids) == len(start_of_word) ==
                             len(span_mask))
                id_check = len(sample_id) == 3
                label_check = return_baskets or len(
                    sample.tokenized.get(
                        "labels", [])) == self.max_answers  # type: ignore
                # labels are set to -100 when answer cannot be found
                label_check2 = return_baskets or np.all(
                    sample.tokenized["labels"] > -99)  # type: ignore
                if len_check and id_check and label_check and label_check2:
                    # - The first of the labels will be used in train, and the full array will be used in eval.
                    # - start_of_word and spec_tok_mask are not actually needed by model.forward() but are needed for
                    #   model.formatted_preds() during inference for creating answer strings
                    # - passage_start_t is index of passage's first token relative to document
                    feature_dict = {
                        "input_ids": input_ids,
                        "padding_mask": padding_mask,
                        "segment_ids": segment_ids,
                        "passage_start_t":
                        passage_start_t,  # 相对于 document token 的起始位置.
                        "start_of_word": start_of_word,
                        "labels": sample.tokenized.get("labels",
                                                       []),  # type: ignore
                        "id": sample_id,
                        "seq_2_start_t":
                        seq_2_start_t,  # query、passage pair 对中的 token id 起始位置
                        "span_mask": span_mask,
                    }
                    # other processor's features can be lists
                    sample.features = [feature_dict]  # type: ignore
                else:
                    self.problematic_sample_ids.add(sample.id)
                    sample.features = None
        return baskets

    def _create_dataset(self, baskets: List[SampleBasket]):
        """
        Convert python features into paddle dataset.
        Also removes potential errors during preprocessing.
        Flattens nested basket structure to create a flat list of features
        """
        features_flat: List[dict] = []
        basket_to_remove = []
        for basket in baskets:
            if self._check_sample_features(basket):
                for sample in basket.samples:  # type: ignore
                    features_flat.extend(sample.features)  # type: ignore
            else:
                # remove the entire basket
                basket_to_remove.append(basket)
        if len(basket_to_remove) > 0:
            for basket in basket_to_remove:
                # if basket_to_remove is not empty remove the related baskets
                baskets.remove(basket)

        dataset, tensor_names = convert_features_to_dataset(
            features=features_flat)
        return dataset, tensor_names, baskets


class TextSimilarityProcessor(Processor):
    """
    Used to handle the Dense Passage Retrieval datasets that come in json format, example: biencoder-nq-train.json, biencoder-nq-dev.json, trivia-train.json, trivia-dev.json

    dataset format: list of dictionaries with keys: 'dataset', 'question', 'answers', 'positive_ctxs', 'negative_ctxs', 'hard_negative_ctxs'
    Each sample is a dictionary of format:
    {"dataset": str,
    "question": str,
    "answers": list of str
    "positive_ctxs": list of dictionaries of format {'title': str, 'text': str, 'score': int, 'title_score': int, 'passage_id': str}
    "negative_ctxs": list of dictionaries of format {'title': str, 'text': str, 'score': int, 'title_score': int, 'passage_id': str}
    "hard_negative_ctxs": list of dictionaries of format {'title': str, 'text': str, 'score': int, 'title_score': int, 'passage_id': str}
    }

    """

    def __init__(
        self,
        query_tokenizer,  # type: ignore
        passage_tokenizer,  # type: ignore
        max_seq_len_query: int,
        max_seq_len_passage: int,
        data_dir: str = "",
        metric=None,  # type: ignore
        train_filename: str = "train.json",
        dev_filename: Optional[str] = None,
        test_filename: Optional[str] = "test.json",
        dev_split: float = 0.1,
        proxies: Optional[dict] = None,
        max_samples: Optional[int] = None,
        embed_title: bool = True,
        num_positives: int = 1,
        num_hard_negatives: int = 1,
        shuffle_negatives: bool = True,
        shuffle_positives: bool = False,
        label_list: Optional[List[str]] = None,
        **kwargs,
    ):
        """
        :param query_tokenizer: Used to split a question (str) into tokens
        :param passage_tokenizer: Used to split a passage (str) into tokens.
        :param max_seq_len_query: Query samples are truncated after this many tokens.
        :param max_seq_len_passage: Context/Passage Samples are truncated after this many tokens.
        :param data_dir: The directory in which the train and dev files can be found.
                         If not available the dataset will be loaded automaticaly
                         if the last directory has the same name as a predefined dataset.
                         These predefined datasets are defined as the keys in the dict at
                         `pipelines.basics.data_handler.utils`_.
        :param metric: name of metric that shall be used for evaluation, e.g. "acc" or "f1_macro".
                 Alternatively you can also supply a custom function, that takes preds and labels as args and returns a numerical value.
                 For using multiple metrics supply them as a list, e.g ["acc", my_custom_metric_fn].
        :param train_filename: The name of the file containing training data.
        :param dev_filename: The name of the file containing the dev data. If None and 0.0 < dev_split < 1.0 the dev set
                             will be a slice of the train set.
        :param test_filename: None
        :param dev_split: The proportion of the train set that will sliced. Only works if dev_filename is set to None
        :param proxies: proxy configuration to allow downloads of remote datasets.
                        Format as in  "requests" library: https://2.python-requests.org//en/latest/user/advanced/#proxies
        :param max_samples: maximum number of samples to use
        :param embed_title: Whether to embed title in passages during tensorization (bool),
        :param num_hard_negatives: maximum number to hard negative context passages in a sample
        :param num_positives: maximum number to positive context passages in a sample
        :param shuffle_negatives: Whether to shuffle all the hard_negative passages before selecting the num_hard_negative number of passages
        :param shuffle_positives: Whether to shuffle all the positive passages before selecting the num_positive number of passages
        :param label_list: list of labels to predict. Usually ["hard_negative", "positive"]
        :param kwargs: placeholder for passing generic parameters
        """
        # TODO If an arg is misspelt, e.g. metrics, it will be swallowed silently by kwargs

        # Custom processor attributes
        self.max_samples = max_samples
        self.query_tokenizer = query_tokenizer
        self.passage_tokenizer = passage_tokenizer
        self.embed_title = embed_title
        self.num_hard_negatives = num_hard_negatives
        self.num_positives = num_positives
        self.shuffle_negatives = shuffle_negatives
        self.shuffle_positives = shuffle_positives
        self.max_seq_len_query = max_seq_len_query
        self.max_seq_len_passage = max_seq_len_passage

        super(TextSimilarityProcessor, self).__init__(
            tokenizer=None,  # type: ignore
            max_seq_len=0,
            train_filename=train_filename,
            dev_filename=dev_filename,
            test_filename=test_filename,
            dev_split=dev_split,
            data_dir=data_dir,
            tasks={},
            proxies=proxies,
        )
        if metric:
            self.add_task(
                name="text_similarity",
                metric=metric,
                label_list=label_list,
                label_name="label",
                task_type="text_similarity",
            )
        else:
            logger.info(
                "Initialized processor without tasks. Supply `metric` and `label_list` to the constructor for "
                "using the default task or add a custom task later via processor.add_task()"
            )

    def dataset_from_dicts(self,
                           dicts: List[dict],
                           indices: Optional[List[int]] = None,
                           return_baskets: bool = False):
        """
        Convert input dictionaries into a paddle dataset for TextSimilarity.
        For conversion we have an internal representation called "baskets".
        Each basket is one query and related text passages (positive passages fitting to the query and negative
        passages that do not fit the query)
        Each stage adds or transforms specific information to our baskets.

        :param dicts: input dictionary with DPR-style content
                        {"query": str,
                         "passages": List[
                                        {'title': str,
                                        'text': str,
                                        'label': 'hard_negative',
                                        'external_id': str},
                                        ....
                                        ]
                         }
        :param indices: indices used during multiprocessing so that IDs assigned to our baskets is unique
        :param return_baskets: whether to return the baskets or not (baskets are needed during inference)
        :return: dataset, tensor_names, problematic_ids, [baskets]
        """
        # Take the dict and insert into our basket structure, this stages also adds an internal IDs
        baskets = self._fill_baskets(dicts, indices)

        # Separat conversion of query
        baskets = self._convert_queries(baskets=baskets)

        # and context passages. When converting the context the label is also assigned.
        baskets = self._convert_contexts(baskets=baskets)

        # Convert features into paddle dataset, this step also removes and logs potential errors during preprocessing
        dataset, tensor_names, problematic_ids, baskets = self._create_dataset(
            baskets)

        if problematic_ids:
            logger.error(
                f"There were {len(problematic_ids)} errors during preprocessing at positions: {problematic_ids}"
            )

        if return_baskets:
            return dataset, tensor_names, problematic_ids, baskets
        else:
            return dataset, tensor_names, problematic_ids

    def _fill_baskets(self, dicts: List[dict], indices: Optional[List[int]]):
        baskets = []
        if not indices:
            indices = list(range(len(dicts)))
        for d, id_internal in zip(dicts, indices):
            basket = SampleBasket(id_external=None,
                                  id_internal=id_internal,
                                  raw=d)
            baskets.append(basket)
        return baskets

    def _convert_queries(self, baskets: List[SampleBasket]):
        for basket in baskets:
            clear_text = {}
            tokenized = {}
            features = [{}]  # type: ignore
            # extract query, positive context passages and titles, hard-negative passages and titles
            if "query" in basket.raw:
                try:
                    query = self._normalize_question(basket.raw["query"])
                    query_inputs = self.query_tokenizer(query)
                    tokenized_query = self.query_tokenizer.convert_ids_to_tokens(
                        query_inputs["input_ids"])

                    if len(tokenized_query) == 0:
                        logger.warning(
                            f"The query could not be tokenized, likely because it contains a character that the query tokenizer does not recognize"
                        )
                        return None

                    clear_text["query_text"] = query
                    tokenized["query_tokens"] = tokenized_query
                    features[0]["query_input_ids"] = query_inputs["input_ids"]
                    features[0]["query_segment_ids"] = query_inputs[
                        "token_type_ids"]
                except Exception as e:
                    features = None  # type: ignore

            sample = Sample(id="",
                            clear_text=clear_text,
                            tokenized=tokenized,
                            features=features)  # type: ignore
            basket.samples = [sample]
        return baskets

    def _convert_contexts(self, baskets: List[SampleBasket]):
        for basket in baskets:
            if "passages" in basket.raw:
                try:
                    positive_context = list(
                        filter(lambda x: x["label"] == "positive",
                               basket.raw["passages"]))
                    if self.shuffle_positives:
                        random.shuffle(positive_context)
                    positive_context = positive_context[:self.num_positives]
                    hard_negative_context = list(
                        filter(lambda x: x["label"] == "hard_negative",
                               basket.raw["passages"]))
                    if self.shuffle_negatives:
                        random.shuffle(hard_negative_context)
                    hard_negative_context = hard_negative_context[:self.
                                                                  num_hard_negatives]

                    positive_ctx_titles = [
                        passage.get("title", None)
                        for passage in positive_context
                    ]
                    positive_ctx_texts = [
                        passage["text"] for passage in positive_context
                    ]
                    hard_negative_ctx_titles = [
                        passage.get("title", None)
                        for passage in hard_negative_context
                    ]
                    hard_negative_ctx_texts = [
                        passage["text"] for passage in hard_negative_context
                    ]

                    # all context passages and labels: 1 for positive context and 0 for hard-negative context
                    ctx_label = [1] * self.num_positives + [
                        0
                    ] * self.num_hard_negatives
                    # featurize context passages
                    if self.embed_title:
                        # concatenate title with positive context passages + negative context passages
                        all_ctx = self._combine_title_context(
                            positive_ctx_titles,
                            positive_ctx_texts) + self._combine_title_context(
                                hard_negative_ctx_titles,
                                hard_negative_ctx_texts)
                    else:
                        all_ctx = positive_ctx_texts + hard_negative_ctx_texts

                    # assign empty string tuples if hard_negative passages less than num_hard_negatives
                    all_ctx += [("", "")] * (
                        (self.num_positives + self.num_hard_negatives) -
                        len(all_ctx))

                    # [text] -> tokenize -> id
                    ctx_inputs = self.passage_tokenizer(all_ctx[0])

                    # get tokens in string format
                    tokenized_passage = [
                        self.passage_tokenizer.convert_ids_to_tokens(ctx)
                        for ctx in ctx_inputs["input_ids"]
                    ]
                    # we only have one sample containing query and corresponding (multiple) context features
                    sample = basket.samples[0]  # type: ignore
                    sample.clear_text[
                        "passages"] = positive_context + hard_negative_context
                    sample.tokenized[
                        "passages_tokens"] = tokenized_passage  # type: ignore
                    sample.features[0]["passage_input_ids"] = ctx_inputs[
                        "input_ids"]  # type: ignore
                    sample.features[0]["passage_segment_ids"] = ctx_inputs[
                        "token_type_ids"]  # type: ignore
                except Exception as e:
                    basket.samples[0].features = None  # type: ignore

        return baskets

    def _create_dataset(self, baskets: List[SampleBasket]):
        """
        Convert python features into paddle dataset.
        Also removes potential errors during preprocessing.
        Flattens nested basket structure to create a flat list of features
        """
        features_flat: List[dict] = []
        basket_to_remove = []
        problematic_ids: set = set()
        for basket in baskets:
            if self._check_sample_features(basket):
                for sample in basket.samples:  # type: ignore
                    features_flat.extend(sample.features)  # type: ignore
            else:
                # remove the entire basket
                basket_to_remove.append(basket)
        if len(basket_to_remove) > 0:
            for basket in basket_to_remove:
                # if basket_to_remove is not empty remove the related baskets
                problematic_ids.add(basket.id_internal)
                baskets.remove(basket)

        dataset, tensor_names = convert_features_to_dataset(
            features=features_flat)
        return dataset, tensor_names, problematic_ids, baskets

    @staticmethod
    def _normalize_question(question: str) -> str:
        """Removes '?' from queries/questions"""
        if question[-1] == "?":
            question = question[:-1]
        return question

    @staticmethod
    def _combine_title_context(titles: List[str], texts: List[str]):
        res = []
        for title, ctx in zip(titles, texts):
            if title is None:
                title = ""
                logger.warning(
                    f"Couldn't find title although `embed_title` is set to True. Using title='' now. Related passage text: '{ctx}' "
                )
            res.append(tuple((title, ctx)))
        return res


def _is_json(x):
    if issubclass(type(x), Path):
        return True
    try:
        json.dumps(x)
        return True
    except:
        return False
