# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import argparse
import os

import paddle
import paddlenlp as ppnlp

from model import MultiLabelClassifier

# yapf: disable
parser = argparse.ArgumentParser()
parser.add_argument("--params_path", type=str, required=True, default='./checkpoint/model_800/model_state.pdparams', help="The path to model parameters to be loaded.")
parser.add_argument("--output_path", type=str, default='./static_graph_params', help="The path of model parameter in static graph to be saved.")
args = parser.parse_args()
# yapf: enable

if __name__ == "__main__":
    # The number of labels should be in accordance with the training dataset.
    label_info = [
        'toxic', 'severe_toxic', 'obscene', 'threat', 'insult', 'identity_hate'
    ]

    # Load pretrained model
    pretrained_model = ppnlp.transformers.BertModel.from_pretrained(
        "bert-base-uncased")

    model = MultiLabelClassifier(pretrained_model, num_labels=len(label_info))

    if args.params_path and os.path.isfile(args.params_path):
        state_dict = paddle.load(args.params_path)
        model.set_dict(state_dict)
        print("Loaded parameters from %s" % args.params_path)
    model.eval()

    # Convert to static graph with specific input description
    model = paddle.jit.to_static(
        model,
        input_spec=[
            paddle.static.InputSpec(shape=[None, None],
                                    dtype="int64"),  # input_ids
            paddle.static.InputSpec(shape=[None, None],
                                    dtype="int64")  # segment_ids
        ])
    # Save in static graph model.
    paddle.jit.save(model, args.output_path)
