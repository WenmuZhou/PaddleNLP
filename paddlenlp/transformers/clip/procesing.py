# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2021 The HuggingFace Inc. team.
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
"""
Image/Text processor class for CLIP
"""

from ..tokenizer_utils_base import BatchEncoding
from .tokenizer import CLIPTokenizer
from .feature_extraction import CLIPFeatureExtractor

__all__ = ["CLIPProcessor"]


class CLIPProcessor(object):
    r"""
    Constructs a CLIP processor which wraps a CLIP feature extractor and a CLIP tokenizer into a single processor.
    [`CLIPProcessor`] offers all the functionalities of [`CLIPFeatureExtractor`] and [`CLIPTokenizer`]. See the
    [`CLIPProcessor.__call__`] and [`CLIPProcessor.decode`] for more information.
    Args:
        feature_extractor ([`CLIPFeatureExtractor`]):
            The feature extractor is a required input.
        tokenizer ([`CLIPTokenizer`]):
            The tokenizer is a required input.
    """

    def __init__(self, feature_extractor, tokenizer):
        super().__init__()
        self.tokenizer = tokenizer
        self.feature_extractor = feature_extractor

    def __call__(self, text=None, images=None, return_tensors=None, **kwargs):
        """
        Main method to prepare for the model one or several sequences(s) and image(s). This method forwards the `text`
        and `kwargs` arguments to CLIPTokenizer's [`CLIPTokenizer.__call__`] if `text` is not `None` to encode
        the text. To prepare the image(s), this method forwards the `images` and `kwrags` arguments to
        CLIPFeatureExtractor's [`CLIPFeatureExtractor.__call__`] if `images` is not `None`. Please refer to the
        doctsring of the above two methods for more information.
        Args:
            text (`str`, `List[str]`, `List[List[str]]`):
                The sequence or batch of sequences to be encoded. Each sequence can be a string or a list of strings
                (pretokenized string). If the sequences are provided as list of strings (pretokenized), you must set
                `is_split_into_words=True` (to lift the ambiguity with a batch of sequences).
            images (`PIL.Image.Image`, `np.ndarray`, `paddle.Tensor`, `List[PIL.Image.Image]`, `List[np.ndarray]`, `List[paddle.Tensor]`):
                The image or batch of images to be prepared. Each image can be a PIL image, NumPy array or Paddle
                tensor. In case of a NumPy array/Paddle tensor, each image should be of shape (C, H, W), where C is a
                number of channels, H and W are image height and width.
            return_tensors (`str` or [`~utils.TensorType`], *optional*):
                If set, will return tensors of a particular framework. Acceptable values are:
                - `'np'`: Return NumPy `np.ndarray` objects.
                - `'pd'`: Return Paddle `paddle.Tensor` objects.
        Returns:
            [`BatchEncoding`]: A [`BatchEncoding`] with the following fields:
            - **input_ids** -- List of token ids to be fed to a model. Returned when `text` is not `None`.
            - **attention_mask** -- List of indices specifying which tokens should be attended to by the model (when
              `return_attention_mask=True` or if *"attention_mask"* is in `self.model_input_names` and if `text` is not
              `None`).
            - **pixel_values** -- Pixel values to be fed to a model. Returned when `images` is not `None`.
        """

        if text is None and images is None:
            raise ValueError(
                "You have to specify either text or images. Both cannot be none."
            )

        if text is not None:
            encoding = self.tokenizer(text,
                                      return_tensors=return_tensors,
                                      **kwargs)

        if images is not None:
            image_features = self.feature_extractor(
                images, return_tensors=return_tensors, **kwargs)

        if text is not None and images is not None:
            encoding["pixel_values"] = image_features.pixel_values
            return encoding
        elif text is not None:
            return encoding
        else:
            return BatchEncoding(data=dict(**image_features),
                                 tensor_type=return_tensors)

    def batch_decode(self, *args, **kwargs):
        """
        This method forwards all its arguments to CLIPTokenizer's [`~PreTrainedTokenizer.batch_decode`]. Please
        refer to the docstring of this method for more information.
        """
        return self.tokenizer.batch_decode(*args, **kwargs)

    def decode(self, *args, **kwargs):
        """
        This method forwards all its arguments to CLIPTokenizer's [`~PreTrainedTokenizer.decode`]. Please refer to
        the docstring of this method for more information.
        """
        return self.tokenizer.decode(*args, **kwargs)

    # TODO junnyu find a better way from_pretrained and save_pretrained
    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path, *args, **kwargs):
        tokenizer = CLIPTokenizer.from_pretrained(pretrained_model_name_or_path,
                                                  *args, **kwargs)
        feature_extractor = CLIPFeatureExtractor()
        return cls(feature_extractor, tokenizer)

    def save_pretrained(self, save_directory, filename_prefix=None, **kwargs):
        return self.tokenizer.save_pretrained(save_directory, filename_prefix,
                                              **kwargs)
